"""PhotoActions: styled product photos from the admin's own photos (Meta's
Muse Image, imageModel.py).

The admin uploads plain photos of an item to their own users/<uid>/ folder
in the Firebase bucket; generateProductPhotos reads them there by bucket
and path (photoStorage, never by fetching a URL), asks the image model for
PHOTOS_PER_CALL photos (photoPrompts: the first in the global prompt, with
the store's brand colors, the others in that plus the store's photoStyle,
StoreActions.updateMyStoreTheme; a store with no photo style gets the first
only), saves each under stores/<storeId>/photos/ and returns their download
URLs. Admins only, at
the store they own (requireOwnerStore); the call runs inside the request.

Each store makes at most STORE_DAILY_PHOTO_CAP photos a day (UTC+8), and
all stores together at most PHOTO_DAILY_CAP_ALL (every Google sign-in
becomes an admin with a store, so the store cap alone bounds nothing): two
PhotoUsage counters (usageCounters, deleted by a TTL index) count the
photos asked for, and give back those the model never returned (all of
them when it failed). A photo returned but not saved stays counted: Meta
billed it. At most PHOTO_CALLS_AT_ONCE calls run at once per process: a
call past that is a 503 before any quota is taken.

One deadline, taken when the call starts, bounds the Storage reads and the
model call; the model is skipped (a 503) with under MODEL_MIN_SECONDS
left. Each photo is recorded (a GeneratedPhotos document) right after its
upload: when a later one fails, the saved ones are still returned.

makeProductPhotoChoices is the photo step of adding a product with a
photo: it copies the admin's original into the store's photos (no model, so
no photo quota) and makes PHOTOS_PER_CALL photos from it as
generateProductPhotos does, so the admin picks one of them all (the chat's
ProductPhotoChoiceCard). When fewer can be made (no photo style, a daily cap
reached, the model unavailable or declining), the original and those made
are still offered, with the reason.

findProductsInPhoto is the first step of adding several products from one
group photo the admin attached: productFinder.py names each product (Muse
Spark) and finds it (SAM, samModel.py), cuts each one out and saves it in
the admin's own users/<uid>/found/ folder, and returns each name with its
cut-out's URL, which the new products form then takes as that row's photo
(and makeProductPhotoChoices as its original). Each store looks through at
most STORE_DAILY_FIND_CAP group photos a day, and all stores together at
most PRODUCT_FIND_DAILY_CAP_ALL (PhotoUsage counters, given back when the
find fails).

Every photo the model made keeps its prompt (GeneratedPhoto.prompt), and
both photo tools return it on each photo: the admin sees what the model was
asked.

findColorsInPhoto reads the main colors of a photo the admin attached
(colorFinder.py, Pillow), for the store's brand colors: the chat model
cannot read exact colors from an image. With a focus ("the logo"), SAM finds
that part first and only it is read. Only that SAM call costs: each store
asks for at most STORE_DAILY_COLOR_FIND_CAP a day, all stores together
COLOR_FIND_DAILY_CAP_ALL (given back when it fails); without a focus it
takes no quota and no photo slot. It answers before the store is set up
too, so a new admin's brand colors can come from their logo.

setProductPhoto shows one of the store's photos on one of its products
(Product.photoUrl, the photo's download URL): the shop shows it. Only a
photo the store holds can be set (made by the model, or an original
makeProductPhotoChoices copied), never a URL the client or the AI sends.

The clock: every read of now is _now(), which reads this module's
`datetime`, so a test freezes it by replacing PhotoActions.datetime.
"""

import datetime
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import colorFinder
import imageModel
import photoStorage
import productFinder
import samModel
from actions.callSlots import slot
from actions.ProductActions import PRODUCT_NOT_FOUND, PRODUCTS
from actions.StoreActions import (_validText, requireAnyOwnerStore,
                                  requireOwnerStore)
# ALL_STORES is imported for the tests, which read it here.
from actions.usageCounters import (ALL_STORES, appDay, envCap,
                                   giveBackQuotas, takeDailyQuotas)
from mcpTools import mcp_tool
from mongoDb import db
from services.__request import ServiceUnavailable
from objects import (PHOTO_NOTES_MAX_LENGTH, FoundProductsResponse,
                     PHOTO_COLOR_FOCUS_MAX_LENGTH, PhotoColorsResponse,
                     PHOTO_REFERENCES_MAX, PHOTOS_PER_CALL, PHOTOS_THEMED,
                     PRODUCT_NAME_MAX_LENGTH,
                     AdminProductRow, GeneratedPhoto, GeneratedPhotosResponse,
                     ProductPhotoChoicesResponse, projectedCopy,
                     projection_of)
from route_config import route_config
from utils import generateRandomString

GENERATED_PHOTOS = 'GeneratedPhotos'
PHOTO_USAGE = 'PhotoUsage'
# The kind of the photo counters, the store's and all stores'.
PHOTOS_KIND = 'photos'
# The kind of the group photo counters (findProductsInPhoto).
FINDS_KIND = 'productFinds'
# The kind of the focused color read counters (findColorsInPhoto).
COLOR_FINDS_KIND = 'colorFinds'
# What a store reset carries over to the new store (StoreActions).
STORE_USAGE_KINDS = (PHOTO_USAGE, (PHOTOS_KIND, FINDS_KIND, COLOR_FINDS_KIND))
# Read at import, so it stays the real timezone when a test replaces this
# module's `datetime`.
UTC = datetime.timezone.utc
STORE_DAILY_PHOTO_CAP = 100
# All stores together: PHOTO_DAILY_CAP_ALL (a whole number, 0 turns photos
# off), else DEFAULT_PHOTO_DAILY_CAP_ALL.
PHOTO_DAILY_CAP_ALL_VARIABLE = 'PHOTO_DAILY_CAP_ALL'
DEFAULT_PHOTO_DAILY_CAP_ALL = 1000
# Group photos a store, and all stores together, look through a day:
# PRODUCT_FIND_DAILY_CAP_ALL (0 turns it off), else the default.
STORE_DAILY_FIND_CAP = 20
FIND_DAILY_CAP_ALL_VARIABLE = 'PRODUCT_FIND_DAILY_CAP_ALL'
DEFAULT_FIND_DAILY_CAP_ALL = 200
FIND_CAP_REACHED = ("Your store has reached today's limit of group photos, "
                    'try again tomorrow')
FIND_ALL_CAP_REACHED = 'Finding products is busy today, try again tomorrow'
FINDS_TURNED_OFF = 'Finding products in a photo is turned off right now'
# Focused color reads (a SAM call) a store, and all stores together, make a
# day: COLOR_FIND_DAILY_CAP_ALL (0 turns them off), else the default.
STORE_DAILY_COLOR_FIND_CAP = 50
COLOR_FIND_DAILY_CAP_ALL_VARIABLE = 'COLOR_FIND_DAILY_CAP_ALL'
DEFAULT_COLOR_FIND_DAILY_CAP_ALL = 500
COLOR_FIND_CAP_REACHED = ("Your store has reached today's limit of reading "
                          'part of a photo: try again tomorrow, or ask for '
                          "the whole photo's colors")
COLOR_FIND_ALL_CAP_REACHED = ('Reading part of a photo is busy today: ask '
                              "for the whole photo's colors")
COLOR_FINDS_TURNED_OFF = ('Reading part of a photo is turned off right '
                          "now: ask for the whole photo's colors")
COLOR_FOCUS_LABEL = 'The part of the photo'
# The photo calls one process runs at once: each holds one of gunicorn's 8
# threads for up to PHOTO_DEADLINE_SECONDS. With the chat's 4, 2 stay free
# for the rest of the app.
PHOTO_CALLS_AT_ONCE = 2
_photoSlots = threading.BoundedSemaphore(PHOTO_CALLS_AT_ONCE)
# The call's reads and model call end by this deadline, and the saves get
# SAVE_SECONDS after the model answered. Cloud Run's request timeout is
# what caps a request (gunicorn's gthread --timeout only watches the
# worker), and it is above both together.
PHOTO_DEADLINE_SECONDS = 90
SAVE_SECONDS = 20
# The least time the model call may start with: less, and it is skipped.
MODEL_MIN_SECONDS = 15
PHOTO_CAP_REACHED = ("Your store has reached today's photo limit, try again "
                     'tomorrow')
PHOTO_ALL_CAP_REACHED = 'Photo generation is busy today, try again tomorrow'
PHOTOS_TURNED_OFF = 'Photo generation is turned off right now'
IMAGE_URLS_COUNT = f'Send 1 to {PHOTO_REFERENCES_MAX} imageUrls'
IMAGE_URLS_TWICE = 'imageUrls lists the same photo twice'
IMAGE_URL_MISSING = 'Send the imageUrl of the photo you uploaded'
NOTES_LABEL = 'The notes'
PRODUCT_NAME_LABEL = "The product's name"
PHOTO_NOT_FOUND = 'Photo not found'
# Why a photo call made fewer than PHOTOS_PER_CALL photos (with a cap's own
# message, or the model's refusal).
NO_STYLE_GLOBAL_ONLY = (f'Your store has no photo style yet, so only 1 of the '
                        f'{PHOTOS_PER_CALL} photos was made: set one to get '
                        'the others')
UNAVAILABLE_ORIGINAL_ONLY = ('Photo generation is unavailable right now, so '
                             'no styled photos were made')
SOME_NOT_MADE = (f'Some of the {PHOTOS_PER_CALL} photos could not be made '
                 'this time')
# The cap refusals, as makeProductPhotoChoices tells them.
CAPS_ORIGINAL_ONLY = {
    PHOTO_CAP_REACHED: ("Your store has reached today's photo limit, so no "
                        'styled photos were made: try again tomorrow'),
    PHOTO_ALL_CAP_REACHED: ('Photo generation is busy today, so no styled '
                            'photos were made: try again tomorrow'),
    PHOTOS_TURNED_OFF: ('Photo generation is turned off right now, so no '
                        'styled photos were made'),
}
PHOTO_INSTRUCTION = (
    'Create a professional e-commerce product photo of the item shown in '
    'the reference photos. Keep the product exactly as it is: its shape, '
    'colors, labels and printed text. Show only the product: leave out '
    'anything else in the reference photos, such as items placed beside it '
    'for scale. Add no text, logos, watermarks or people unless the style '
    'asks for them.')


def _now() -> datetime.datetime:
    """Now, timezone aware, in UTC: the one clock of the photo route."""
    return datetime.datetime.now(UTC)


def _validImageUrls(userId: str, imageUrls) -> list[str]:
    """The object paths of the admin's reference photos: 1 to
    PHOTO_REFERENCES_MAX download URLs of our bucket, each under the
    admin's own users/<uid>/ folder, no photo twice. A refusal names the
    URL's place (imageUrls[0]), never the URL."""
    if (not isinstance(imageUrls, list)
            or not 1 <= len(imageUrls) <= PHOTO_REFERENCES_MAX):
        raise ValueError(IMAGE_URLS_COUNT)
    return [path for _, path in photoStorage.ownPathsOfUrls(
        userId, imageUrls, 'imageUrls', IMAGE_URLS_TWICE)]


def brandColorsLine(primaryColor: str | None,
                    secondaryColor: str | None) -> str | None:
    """The prompt's line for the store's brand colors, or None when it has
    neither."""
    colors = [f'{name} {color}' for name, color in
              (('primary', primaryColor), ('secondary', secondaryColor))
              if color]
    if not colors:
        return None
    return (f'Brand colors: {", ".join(colors)} — use them as accents in '
            'the background and props, never on the product itself.')


def globalPhotoPrompt(store: dict, notes: str | None,
                      productName: str | None = None) -> str:
    """The global prompt, every photo call's first photo: the fixed
    instruction, the admin's notes as its special request (when sent), the
    product's name (when known, so the model knows which item is the
    product) and the store's brand colors (when set)."""
    lines = [PHOTO_INSTRUCTION]
    if notes is not None:
        lines.append(f'Special request: {notes}')
    if productName is not None:
        lines.append(f'Product: {productName}')
    colors = brandColorsLine(store.get('primaryColor'),
                             store.get('secondaryColor'))
    if colors is not None:
        lines.append(colors)
    return '\n'.join(lines)


def photoPrompt(store: dict, notes: str | None,
                productName: str | None = None) -> str:
    """The themed prompt, every photo call's other photos: the global
    prompt and the store's photo style."""
    return (f'{globalPhotoPrompt(store, notes, productName)}\n'
            f'Style: {store["photoStyle"]}')


def photoPrompts(store: dict, notes: str | None,
                 productName: str | None = None) -> list[tuple[str, int]]:
    """The prompts of a photo call, each with how many photos it makes:
    1 in the global prompt, then PHOTOS_THEMED in the themed one, which a
    store with no photo style does not get."""
    prompts = [(globalPhotoPrompt(store, notes, productName), 1)]
    if store.get('photoStyle'):
        prompts.append((photoPrompt(store, notes, productName),
                        PHOTOS_THEMED))
    return prompts


def _notGenerated(store: dict, made: int) -> str | None:
    """Why a photo call that made `made` photos made fewer than
    PHOTOS_PER_CALL: the store has no photo style, or a model call failed;
    null when it made them all."""
    if made >= PHOTOS_PER_CALL:
        return None
    if not store.get('photoStyle'):
        return NO_STYLE_GLOBAL_ONLY
    return SOME_NOT_MADE


def dailyCapAll() -> int:
    """PHOTO_DAILY_CAP_ALL, read at each call (usageCounters.envCap): unset
    or invalid, DEFAULT_PHOTO_DAILY_CAP_ALL."""
    return envCap(PHOTO_DAILY_CAP_ALL_VARIABLE, DEFAULT_PHOTO_DAILY_CAP_ALL,
                  'photoDailyCapAllInvalid')


def _takePhotoQuota(storeId: str, count: int) -> list[str]:
    """Count `count` photos against the store's daily cap and all stores'
    (dailyCapAll), refused when either would pass (and then counted against
    neither: usageCounters.takeQuotas). A cap of 0 is photos turned off.
    Returns the two counters' keys."""
    return takeDailyQuotas(
        PHOTO_USAGE,
        [(PHOTOS_KIND, storeId, STORE_DAILY_PHOTO_CAP, PHOTO_CAP_REACHED)],
        (PHOTOS_KIND, PHOTO_ALL_CAP_REACHED), dailyCapAll(),
        PHOTOS_TURNED_OFF, appDay(_now()), amount=count)


def _takeFindQuota(storeId: str) -> list[str]:
    """Count one group photo against the store's daily cap and all
    stores' (PRODUCT_FIND_DAILY_CAP_ALL), as _takePhotoQuota does."""
    capAll = envCap(FIND_DAILY_CAP_ALL_VARIABLE, DEFAULT_FIND_DAILY_CAP_ALL,
                    'productFindDailyCapAllInvalid')
    return takeDailyQuotas(
        PHOTO_USAGE,
        [(FINDS_KIND, storeId, STORE_DAILY_FIND_CAP, FIND_CAP_REACHED)],
        (FINDS_KIND, FIND_ALL_CAP_REACHED), capAll, FINDS_TURNED_OFF,
        appDay(_now()))


def _takeColorFindQuota(storeId: str) -> list[str]:
    """Count one focused color read against the store's daily cap and all
    stores' (COLOR_FIND_DAILY_CAP_ALL), as _takePhotoQuota does."""
    capAll = envCap(COLOR_FIND_DAILY_CAP_ALL_VARIABLE,
                    DEFAULT_COLOR_FIND_DAILY_CAP_ALL,
                    'colorFindDailyCapAllInvalid')
    return takeDailyQuotas(
        PHOTO_USAGE,
        [(COLOR_FINDS_KIND, storeId, STORE_DAILY_COLOR_FIND_CAP,
          COLOR_FIND_CAP_REACHED)],
        (COLOR_FINDS_KIND, COLOR_FIND_ALL_CAP_REACHED), capAll,
        COLOR_FINDS_TURNED_OFF, appDay(_now()))


def _readFocusedColors(storeId: str, data: bytes, focus: str,
                       deadline: float) -> colorFinder.ColorReading:
    """The colors of the photo's `focus`, SAM finding it: one photo slot
    and one color find quota, given back when the read fails."""
    with slot(_photoSlots, samModel.unavailable):
        keys = _takeColorFindQuota(storeId)
        try:
            return colorFinder.readColors(data, focus, deadline)
        except Exception:
            giveBackQuotas(PHOTO_USAGE, keys)
            raise


def _references(read: list[tuple[bytes, str]]
                ) -> list[tuple[str, bytes, str]]:
    """Photos read (bytes, content type) as the image model takes them:
    (file name, bytes, content type)."""
    return [(f'reference{i}.{"png" if contentType == "image/png" else "jpg"}',
             data, contentType)
            for i, (data, contentType) in enumerate(read)]


def _makeImages(userId: str, store: dict, paths: list[str],
                prompts: list[tuple[str, int]], deadline: float,
                references: list[tuple[str, bytes, str]] | None = None
                ) -> list[tuple[bytes, str]]:
    """The photos of the store from the reference photos at `paths` (or
    `references`, already read), each with its prompt, in the order of
    `prompts` (photoPrompts), whose model calls run at once. They are
    counted against the daily caps: those the model did not return are
    given back (all of them when this fails). When one call fails and
    another returns photos, those are returned (the failure logged, class
    only); when none returns any, the first call's failure is raised."""
    count = sum(n for _, n in prompts)
    keys = _takePhotoQuota(store['_id'], count)
    images = []
    try:
        if references is None:
            references = _references(photoStorage.readOwnImages(
                userId,
                [(f'imageUrls[{i}]', path) for i, path in enumerate(paths)],
                deadline))
        if deadline - time.monotonic() < MODEL_MIN_SECONDS:
            raise imageModel.unavailable()
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            calls = [pool.submit(imageModel.editImages, references, prompt, n,
                                 deadline)
                     for prompt, n in prompts]
        failures = []
        for (prompt, _), call in zip(prompts, calls):
            try:
                images += [(image, prompt) for image in call.result()]
            except Exception as error:
                failures.append(error)
        if failures and not images:
            raise failures[0]
        for error in failures:
            logging.warning({'event': 'productPhotoCallFailed',
                             'errorClass': type(error).__name__})
    finally:
        # Only the photos returned are billed: the rest are not counted.
        giveBackQuotas(PHOTO_USAGE, keys, count - len(images))
    if not images:
        raise imageModel.unavailable()
    return images


def _savePhoto(userId: str, store: dict, paths: list[str], data: bytes,
               deadline: float, *, notes: str | None = None,
               prompt: str | None = None,
               contentType: str = photoStorage.STORE_PHOTO_CONTENT_TYPE
               ) -> dict:
    """Upload one store photo and record it (a GeneratedPhotos document):
    one the model made (with its prompt), or, with no prompt, the admin's
    original. Returns its _id and url."""
    photoId = generateRandomString()
    url = photoStorage.saveStorePhoto(store['_id'], photoId, data, deadline,
                                      contentType)
    original = prompt is None
    db.create(GeneratedPhoto(
        _id=photoId,
        storeId=store['_id'],
        createdBy=userId,
        path=photoStorage.storePhotoPath(store['_id'], photoId, contentType),
        url=url,
        sourcePaths=paths,
        notes=notes,
        model=None if original else imageModel.IMAGE_MODEL,
        source='original' if original else 'generated',
        prompt=prompt,
        createdAt=_now(),
        _version=0).model_dump(by_alias=True), GENERATED_PHOTOS)
    return {'_id': photoId, 'url': url}


def _saveImages(userId: str, store: dict, paths: list[str],
                notes: str | None,
                images: list[tuple[bytes, str]]) -> list[dict]:
    """Upload each image and record it (a GeneratedPhotos document, with
    its prompt) right after, within SAVE_SECONDS. Returns the response's
    rows. When one fails, the saved ones are returned (the failure logged,
    class only); with none saved, it is raised."""
    deadline = time.monotonic() + SAVE_SECONDS
    saved = []
    try:
        for image, prompt in images:
            saved.append({**_savePhoto(userId, store, paths, image, deadline,
                                       notes=notes, prompt=prompt),
                          'prompt': prompt})
    except Exception as error:
        if not saved:
            raise
        logging.warning({'event': 'productPhotoSaveFailed',
                         'errorClass': type(error).__name__,
                         'saved': len(saved),
                         'returned': len(images)})
    return saved


class PhotoActions():

    @mcp_tool('admin',
              thinkingText=['Making your product photos',
                            'Styling your photos',
                            'Creating new photos'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Photos created',
                  responseModel=GeneratedPhotosResponse)
    def generateProductPhotos(self,
                              userId: str,
                              imageUrls: list[str],
                              notes: str | None = None,
                              productName: str | None = None) -> dict:
        """Turn the admin's plain photos of one item into 3 polished
        product photos: the 1st in the store's brand colors, the 2nd and
        3rd in its photo style too (both set with updateMyStoreTheme; a
        store with no photo style gets the 1st only). imageUrls are 1 to 10
        photos of the same item that the admin uploaded in the app (their
        download URLs, each once): never invent or change one. A store
        makes at most 100 photos a day, and the app has a daily limit
        across all stores too. notes is optional extra direction from the
        admin for these photos (up to 300 characters), e.g. "show it from
        above". productName is optional: the product's name (up to 80
        characters), when the photos are for one of the store's products,
        so the model knows which item to photograph. Returns each photo's
        _id, url and the prompt the model was given for it, and
        notGenerated, why fewer than 3 were made (null when all were): tell
        the admin. To show one in the shop, put it on its product with
        setProductPhoto. If this tool refuses, relay its message to the
        admin."""
        deadline = time.monotonic() + PHOTO_DEADLINE_SECONDS
        store = requireOwnerStore(userId, {'_id': 1, 'photoStyle': 1,
                                           'primaryColor': 1,
                                           'secondaryColor': 1})
        paths = _validImageUrls(userId, imageUrls)
        notes = _validText(notes, NOTES_LABEL, PHOTO_NOTES_MAX_LENGTH,
                           required=False)
        productName = _validText(productName, PRODUCT_NAME_LABEL,
                                 PRODUCT_NAME_MAX_LENGTH, required=False)
        prompts = photoPrompts(store, notes, productName)
        started = time.monotonic()
        with slot(_photoSlots, imageModel.unavailable):
            images = _makeImages(userId, store, paths, prompts, deadline)
            photos = _saveImages(userId, store, paths, notes, images)
        # Never the URLs, the paths, the prompts, the notes or the key.
        logging.info({
            'event': 'productPhotos',
            'storeId': store['_id'],
            'count': sum(n for _, n in prompts),
            'returned': len(images),
            'saved': len(photos),
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'photos': photos,
                'notGenerated': _notGenerated(store, len(photos))}

    @mcp_tool('admin',
              thinkingText=['Styling your product photo',
                            'Making photo choices for your product',
                            'Preparing your photo options'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Photo choices ready',
                  responseModel=ProductPhotoChoicesResponse)
    def makeProductPhotoChoices(self,
                                userId: str,
                                productId: str,
                                imageUrl: str,
                                notes: str | None = None) -> dict:
        """Make photo choices for one product of the admin's store (its
        productId from addStoreProducts or listMyStoreProducts) from the one
        photo the admin uploaded for it (imageUrl, its download URL from
        the new products form: never invent or change one). Keeps the
        original as one of the store's photos and makes 3 photos from it:
        the 1st in the store's brand colors, the 2nd and 3rd in its photo
        style too (a store with no photo style gets the 1st only); they
        count against the daily photo limits. When fewer are made (no photo
        style, today's limit reached, generation unavailable), the
        original and those made are still returned and notGenerated says
        why: tell the admin. notes is optional extra direction from the
        admin for these photos (up to 300 characters), only when they gave
        one. Returns the photos (original first), each made
        one with the prompt the model was given for it; the chat shows them
        as a card the admin picks from, numbered in this order (1 is the
        original). Call it for one product per turn; when the admin picks a
        photo on the card, or names one by its number ("use the 2nd"), call
        setProductPhoto with it, then call
        this for the next product with a photo. If this tool refuses, relay
        its message to the admin."""
        deadline = time.monotonic() + PHOTO_DEADLINE_SECONDS
        store = requireOwnerStore(userId, {'_id': 1, 'photoStyle': 1,
                                           'primaryColor': 1,
                                           'secondaryColor': 1})
        product = None
        if isinstance(productId, str) and productId:
            product = db.read({'_id': productId, 'storeId': store['_id']},
                              PRODUCTS, projection={'name': 1}, findOne=True)
        if product is None:
            raise ValueError(PRODUCT_NOT_FOUND)
        if not isinstance(imageUrl, str) or not imageUrl.strip():
            raise ValueError(IMAGE_URL_MISSING)
        path = photoStorage.ownImagePath(
            userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
            'imageUrl')
        paths = [path]
        notes = _validText(notes, NOTES_LABEL, PHOTO_NOTES_MAX_LENGTH,
                           required=False)
        started = time.monotonic()
        notGenerated = None
        images: list[tuple[bytes, str]] = []
        with slot(_photoSlots, imageModel.unavailable):
            read = photoStorage.readOwnImages(userId, [('imageUrl', path)],
                                              deadline)
            [(data, contentType)] = read
            original = _savePhoto(userId, store, paths, data,
                                  time.monotonic() + SAVE_SECONDS,
                                  contentType=contentType)
            photos = [{**original, 'source': 'original', 'prompt': None}]
            generated = []
            try:
                images = _makeImages(userId, store, paths,
                                     photoPrompts(store, notes,
                                                  product['name']),
                                     deadline, _references(read))
                generated = _saveImages(userId, store, paths, notes, images)
            except ValueError as refusal:
                # A cap reached, photos turned off, or the model declining:
                # the admin's to read.
                notGenerated = CAPS_ORIGINAL_ONLY.get(str(refusal),
                                                      str(refusal))
            except ServiceUnavailable:
                notGenerated = UNAVAILABLE_ORIGINAL_ONLY
            photos += [{**photo, 'source': 'generated'}
                       for photo in generated]
            if generated:
                notGenerated = _notGenerated(store, len(generated))
        # Never the URL, the path, the prompts or the refusal's text.
        logging.info({
            'event': 'productPhotoChoices',
            'storeId': store['_id'],
            'returned': len(images),
            'saved': len(photos) - 1,
            'generated': notGenerated is None,
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'productId': productId, 'productName': product['name'],
                'photos': photos, 'notGenerated': notGenerated}

    @mcp_tool('admin',
              thinkingText=['Looking for products in your photo',
                            'Naming your products',
                            'Cutting out each product'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Products found',
                  responseModel=FoundProductsResponse)
    def findProductsInPhoto(self,
                            userId: str,
                            imageUrl: str) -> dict:
        """Find the products in one photo of several products side by side
        that the admin attached (imageUrl, the attached image's url: never
        invent or change one): names each product and cuts its photo out
        (up to 12; things there for scale, such as a ball, are left out).
        Returns the products found, left to right, each a name and the
        imageUrl of its cut-out photo, and notFound, the names of products
        seen but not located. Then open showNewProductsForm with one row per
        product found, its name and that imageUrl (prices and stock only
        as the admin said them), and tell the admin to check the names and
        fill in the rest, and which products were not found (they can add
        those rows themselves). A store looks through at most 20 group
        photos a day. If this tool refuses, relay its message to the
        admin."""
        deadline = time.monotonic() + PHOTO_DEADLINE_SECONDS
        store = requireOwnerStore(userId, {'_id': 1})
        if not isinstance(imageUrl, str) or not imageUrl.strip():
            raise ValueError(IMAGE_URL_MISSING)
        path = photoStorage.ownImagePath(
            userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
            'imageUrl')
        started = time.monotonic()
        with slot(_photoSlots, samModel.unavailable):
            keys = _takeFindQuota(store['_id'])
            try:
                [(data, _)] = photoStorage.readOwnImages(
                    userId, [('imageUrl', path)], deadline)
                finding = productFinder.findProducts(data, deadline)
            except Exception:
                # A find that failed is not counted.
                giveBackQuotas(PHOTO_USAGE, keys)
                raise
            saveBy = time.monotonic() + SAVE_SECONDS
            products = []
            for product in finding.found:
                saved = photoStorage.saveFoundPhoto(
                    userId, generateRandomString(), product.photo, saveBy)
                products.append({'name': product.name,
                                 'imageUrl': photoStorage.publicPathUrl(
                                     saved)})
        # Never the URL, the path or the names.
        logging.info({
            'event': 'productsInPhoto',
            'storeId': store['_id'],
            'found': len(products),
            'notFound': len(finding.notFound),
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'products': products, 'notFound': finding.notFound}

    @mcp_tool('admin',
              thinkingText=['Reading the colors in your photo',
                            'Matching your colors',
                            'Picking out the main colors'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Colors read',
                  responseModel=PhotoColorsResponse)
    def findColorsInPhoto(self,
                          userId: str,
                          imageUrl: str,
                          focus: str | None = None) -> dict:
        """Read the exact main colors, as hex codes, of an image the admin
        attached (imageUrl, the attached image's url: never invent or
        change one). Call it whenever the admin wants a color from an
        image (their logo, a photo, a screenshot): you cannot read exact
        colors yourself, so never guess a hex code from an image and never
        ask the admin for one they say they do not know. focus is a short
        phrase (2 to 6 words) naming the part of the image the admin
        means, as it looks ("green square logo", "red shop sign"); leave it
        out for the whole image. Returns up to 6 colors, biggest share
        first, each with its share of the part read and whether it is a
        neutral (white, black or grey); focusFound false means the focus
        was not found and the colors are the whole image's (say so). The
        chat shows them as swatches the admin picks the primary and
        secondary color from; suggest the biggest non-neutral color for
        the primary. When the admin picks on the card, or names a color,
        save it with updateMyStoreTheme (createUserStore while setting up
        the store). If this tool refuses, relay its message to the
        admin."""
        deadline = time.monotonic() + PHOTO_DEADLINE_SECONDS
        # A store not set up yet reads colors too (its brand colors come
        # from createUserStore).
        store = requireAnyOwnerStore(userId, {'_id': 1})
        if not isinstance(imageUrl, str) or not imageUrl.strip():
            raise ValueError(IMAGE_URL_MISSING)
        path = photoStorage.ownImagePath(
            userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
            'imageUrl')
        focus = _validText(focus, COLOR_FOCUS_LABEL,
                           PHOTO_COLOR_FOCUS_MAX_LENGTH, required=False)
        started = time.monotonic()
        [(data, _)] = photoStorage.readOwnImages(
            userId, [('imageUrl', path)], deadline)
        if focus is None:
            reading = colorFinder.readColors(data, None, deadline)
        else:
            reading = _readFocusedColors(store['_id'], data, focus,
                                         deadline)
        # Never the URL, the path, the focus or the colors.
        logging.info({
            'event': 'photoColors',
            'storeId': store['_id'],
            'colors': len(reading.colors),
            'focused': focus is not None,
            'focusFound': reading.focusFound,
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'colors': [{'hex': color.hex, 'share': color.share,
                            'neutral': color.neutral}
                           for color in reading.colors],
                'focus': focus, 'focusFound': reading.focusFound}

    @mcp_tool('admin',
              thinkingText=['Setting the product photo',
                            'Updating the product photo',
                            'Putting the photo on the product'])
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Product photo set',
                  responseModel=AdminProductRow)
    def setProductPhoto(self,
                        userId: str,
                        productId: str,
                        photoId: str | None = None) -> dict:
        """Show one of the store's product photos (its _id from
        generateProductPhotos or makeProductPhotoChoices) on a product of
        the admin's store (its productId from listMyStoreProducts): customers
        see it in the shop. Leave photoId out to take the product's photo
        off. Confirm the product and the photo with the admin before
        calling, unless they picked it on a photo choice card or named its
        number there. If it
        refuses, relay its message to the admin."""
        store = requireOwnerStore(userId, {'_id': 1})
        photoUrl = None
        # An empty photoId is none: the photo comes off.
        if photoId:
            photo = db.read({'_id': photoId, 'storeId': store['_id']},
                            GENERATED_PHOTOS, projection={'url': 1},
                            findOne=True)
            if photo is None:
                raise ValueError(PHOTO_NOT_FOUND)
            photoUrl = photo['url']
        updated = None
        if productId:
            # The _version bump of every product write (Product).
            updated = db.findOneAndUpdate(
                {'_id': productId, 'storeId': store['_id']},
                {'$set': {'photoUrl': photoUrl}, '$inc': {'_version': 1}},
                PRODUCTS, upsert=False,
                projection=projection_of(AdminProductRow))
        if updated is None:
            raise ValueError(PRODUCT_NOT_FOUND)
        return projectedCopy(AdminProductRow, updated)

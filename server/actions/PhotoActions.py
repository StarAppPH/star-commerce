"""PhotoActions: styled product photos from the admin's own photos (Meta's
Muse Image, imageModel.py).

The admin uploads plain photos of an item to their own users/<uid>/ folder
in the Firebase bucket; generateProductPhotos reads them there by bucket
and path (photoStorage, never by fetching a URL), asks the image model for
1 to PHOTOS_PER_CALL_MAX photos in the store's look (its photoStyle and
brand colors, StoreActions.updateMyStoreTheme), saves each under
stores/<storeId>/photos/ and returns their download URLs. Admins only, at
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
no photo quota) and makes PHOTO_CHOICES_GENERATED styled photos from it, so
the admin picks one of them all (the chat's ProductPhotoChoiceCard). When no
styled photo can be made (no photo style, a daily cap reached, the model
unavailable or declining), the original is still offered, with the reason.

Every photo the model made keeps its prompt (GeneratedPhoto.prompt), and
both photo tools return it: the admin sees what the model was asked.

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

import imageModel
import photoStorage
from actions.callSlots import slot
from actions.ProductActions import PRODUCT_NOT_FOUND, PRODUCTS
from actions.StoreActions import _validText, requireOwnerStore
# ALL_STORES is imported for the tests, which read it here.
from actions.usageCounters import (ALL_STORES, appDay, envCap,
                                   giveBackQuotas, takeDailyQuotas)
from mcpTools import mcp_tool
from mongoDb import db
from services.__request import ServiceUnavailable
from objects import (PHOTO_CHOICES_GENERATED, PHOTO_NOTES_MAX_LENGTH,
                     PHOTO_REFERENCES_MAX, PHOTOS_PER_CALL_MAX,
                     PRODUCT_NAME_MAX_LENGTH,
                     AdminProductRow, GeneratedPhoto, GeneratedPhotosResponse,
                     ProductPhotoChoicesResponse, projectedCopy,
                     projection_of)
from route_config import route_config
from utils import generateRandomString, wholeNumber

GENERATED_PHOTOS = 'GeneratedPhotos'
PHOTO_USAGE = 'PhotoUsage'
# The kind of the photo counters, the store's and all stores'.
PHOTOS_KIND = 'photos'
# What a store reset carries over to the new store (StoreActions).
STORE_USAGE_KINDS = (PHOTO_USAGE, (PHOTOS_KIND, ))
# Read at import, so it stays the real timezone when a test replaces this
# module's `datetime`.
UTC = datetime.timezone.utc
STORE_DAILY_PHOTO_CAP = 100
# All stores together: PHOTO_DAILY_CAP_ALL (a whole number, 0 turns photos
# off), else DEFAULT_PHOTO_DAILY_CAP_ALL.
PHOTO_DAILY_CAP_ALL_VARIABLE = 'PHOTO_DAILY_CAP_ALL'
DEFAULT_PHOTO_DAILY_CAP_ALL = 1000
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
NO_PHOTO_STYLE = "Set your store's photo style first"
PHOTO_CAP_REACHED = ("Your store has reached today's photo limit, try again "
                     'tomorrow')
PHOTO_ALL_CAP_REACHED = 'Photo generation is busy today, try again tomorrow'
PHOTOS_TURNED_OFF = 'Photo generation is turned off right now'
IMAGE_URLS_COUNT = f'Send 1 to {PHOTO_REFERENCES_MAX} imageUrls'
IMAGE_URLS_TWICE = 'imageUrls lists the same photo twice'
IMAGE_URL_MISSING = 'Send the imageUrl of the photo you uploaded'
PHOTO_COUNT = f'count is a whole number from 1 to {PHOTOS_PER_CALL_MAX}'
NOTES_LABEL = 'The notes'
PRODUCT_NAME_LABEL = "The product's name"
PHOTO_NOT_FOUND = 'Photo not found'
# Why makeProductPhotoChoices offers the original only (with a cap's own
# message, or the model's refusal).
NO_STYLE_ORIGINAL_ONLY = ("Your store has no photo style yet, so no styled "
                          'photos were made: set one to get them')
UNAVAILABLE_ORIGINAL_ONLY = ('Photo generation is unavailable right now, so '
                             'no styled photos were made')
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


def _validCount(count) -> int:
    """How many photos to make: 1 to PHOTOS_PER_CALL_MAX (a bool is no
    number). The generated HTTP handler passes a missing body key as None,
    which is 1."""
    if count is None:
        return 1
    return wholeNumber(count, 1, PHOTOS_PER_CALL_MAX, PHOTO_COUNT)


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


def photoPrompt(store: dict, notes: str | None,
                productName: str | None = None) -> str:
    """The prompt of a photo call: the fixed instruction, the product's
    name (when known, so the model knows which item is the product), the
    store's style, its brand colors (when set) and the admin's notes (when
    sent)."""
    lines = [PHOTO_INSTRUCTION]
    if productName is not None:
        lines.append(f'Product: {productName}')
    lines.append(f'Style: {store["photoStyle"]}')
    colors = brandColorsLine(store.get('primaryColor'),
                             store.get('secondaryColor'))
    if colors is not None:
        lines.append(colors)
    if notes is not None:
        lines.append(f'Notes: {notes}')
    return '\n'.join(lines)


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


def _references(read: list[tuple[bytes, str]]
                ) -> list[tuple[str, bytes, str]]:
    """Photos read (bytes, content type) as the image model takes them:
    (file name, bytes, content type)."""
    return [(f'reference{i}.{"png" if contentType == "image/png" else "jpg"}',
             data, contentType)
            for i, (data, contentType) in enumerate(read)]


def _makeImages(userId: str, store: dict, paths: list[str], count: int,
                prompt: str, deadline: float,
                references: list[tuple[str, bytes, str]] | None = None
                ) -> list[bytes]:
    """`count` photos of the store from the reference photos at `paths`
    (or `references`, already read), counted against the daily caps: those
    the model did not return are given back (all of them when this
    fails)."""
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
        images = imageModel.editImages(references, prompt, count, deadline)
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
                notes: str | None, prompt: str,
                images: list[bytes]) -> list[dict]:
    """Upload each image and record it (a GeneratedPhotos document) right
    after, within SAVE_SECONDS. Returns the response's rows. When one
    fails, the saved ones are returned (the failure logged, class only);
    with none saved, it is raised."""
    deadline = time.monotonic() + SAVE_SECONDS
    saved = []
    try:
        for image in images:
            saved.append(_savePhoto(userId, store, paths, image, deadline,
                                    notes=notes, prompt=prompt))
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
                              count: int = 1,
                              notes: str | None = None,
                              productName: str | None = None) -> dict:
        """Turn the admin's plain photos of one item into polished product
        photos in the store's look (its photo style and brand colors, set
        with updateMyStoreTheme: a store with no photo style is refused).
        imageUrls are 1 to 10 photos of the same item that the admin
        uploaded in the app (their download URLs, each once): never invent
        or change one. count is how many photos to make (1 to 4, default
        1); a store makes at most 100 a day, and the app has a daily limit
        across all stores too. notes is optional extra
        direction from the admin for these photos (up to 300 characters),
        e.g. "show it from above". productName is optional: the product's
        name (up to 80 characters), when the photos are for one of the
        store's products, so the model knows which item to photograph.
        Confirm the count with the admin before calling. Returns each photo's _id and url, and the prompt the model
        was given: to show one in the shop, put it on its product with
        setProductPhoto. If this tool refuses, relay its message to the
        admin."""
        deadline = time.monotonic() + PHOTO_DEADLINE_SECONDS
        store = requireOwnerStore(userId, {'_id': 1, 'photoStyle': 1,
                                           'primaryColor': 1,
                                           'secondaryColor': 1})
        if not store.get('photoStyle'):
            raise ValueError(NO_PHOTO_STYLE)
        paths = _validImageUrls(userId, imageUrls)
        count = _validCount(count)
        notes = _validText(notes, NOTES_LABEL, PHOTO_NOTES_MAX_LENGTH,
                           required=False)
        productName = _validText(productName, PRODUCT_NAME_LABEL,
                                 PRODUCT_NAME_MAX_LENGTH, required=False)
        prompt = photoPrompt(store, notes, productName)
        started = time.monotonic()
        with slot(_photoSlots, imageModel.unavailable):
            images = _makeImages(userId, store, paths, count, prompt,
                                 deadline)
            photos = _saveImages(userId, store, paths, notes, prompt,
                                 images)
        # Never the URLs, the paths, the prompt, the notes or the key.
        logging.info({
            'event': 'productPhotos',
            'storeId': store['_id'],
            'count': count,
            'returned': len(images),
            'saved': len(photos),
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'photos': photos, 'prompt': prompt}

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
                                imageUrl: str) -> dict:
        """Make photo choices for one product of the admin's store (its
        productId from addStoreProducts or listMyStoreProducts) from the one
        photo the admin uploaded for it (imageUrl, its download URL from
        the new products form: never invent or change one). Keeps the
        original as one of the store's photos and makes 3 styled photos
        from it in the store's look (photo style and brand colors); they
        count against the daily photo limits. When no styled photo can be
        made (no photo style, today's limit reached, generation
        unavailable), the original is still returned and notGenerated says
        why: tell the admin. Returns the photos (original first) and the
        prompt the styled ones were made with; the chat shows them as a
        card the admin picks from. Call it for one product per turn; when
        the admin picks a photo, call setProductPhoto with it, then call
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
        started = time.monotonic()
        prompt = None
        notGenerated = None
        images: list[bytes] = []
        with slot(_photoSlots, imageModel.unavailable):
            read = photoStorage.readOwnImages(userId, [('imageUrl', path)],
                                              deadline)
            [(data, contentType)] = read
            original = _savePhoto(userId, store, paths, data,
                                  time.monotonic() + SAVE_SECONDS,
                                  contentType=contentType)
            photos = [{**original, 'source': 'original'}]
            if not store.get('photoStyle'):
                notGenerated = NO_STYLE_ORIGINAL_ONLY
            else:
                prompt = photoPrompt(store, None, product['name'])
                generated = []
                try:
                    images = _makeImages(userId, store, paths,
                                         PHOTO_CHOICES_GENERATED, prompt,
                                         deadline, _references(read))
                    generated = _saveImages(userId, store, paths, None,
                                            prompt, images)
                except ValueError as refusal:
                    # A cap reached, photos turned off, or the model
                    # declining: the admin's to read.
                    notGenerated = CAPS_ORIGINAL_ONLY.get(str(refusal),
                                                          str(refusal))
                except ServiceUnavailable:
                    notGenerated = UNAVAILABLE_ORIGINAL_ONLY
                photos += [{**photo, 'source': 'generated'}
                           for photo in generated]
                if not generated:
                    prompt = None
                    notGenerated = notGenerated or UNAVAILABLE_ORIGINAL_ONLY
        # Never the URL, the path, the prompt or the refusal's text.
        logging.info({
            'event': 'productPhotoChoices',
            'storeId': store['_id'],
            'returned': len(images),
            'saved': len(photos) - 1,
            'generated': notGenerated is None,
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'productId': productId, 'productName': product['name'],
                'photos': photos, 'prompt': prompt,
                'notGenerated': notGenerated}

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
        calling, unless they picked it on a photo choice card. If it
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

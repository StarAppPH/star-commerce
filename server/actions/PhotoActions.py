"""PhotoActions: styled product photos from the admin's own photos (Meta's
Muse Image, imageModel.py).

The admin uploads plain photos of an item to their own users/<uid>/ folder
in the Firebase bucket; generateProductPhotos reads them there by bucket
and path (photoStorage, never by fetching a URL), asks the image model for
PHOTOS_PER_CALL photos, one call each, all at once: each from one base
prompt (the admin's own, else defaultPhotoPrompt: the fixed instructions,
the product's name and the store's brand colors) and its own fixed shot
(PHOTO_SHOTS: a catalog shot, the product in use, a hero shot, each line
written from the store's colors by shotLines), the store's photo style
(StoreActions.updateMyStoreTheme) on the 2nd and 3rd, saves each under
stores/<storeId>/photos/ and returns their download URLs. Admins only, at the store they own
(requireOwnerStore); the call runs inside the request.

Each store makes at most STORE_DAILY_PHOTO_CAP photos a day (UTC+8), and
all stores together at most PHOTO_DAILY_CAP_ALL (every Google sign-in
becomes an admin with a store, so the store cap alone bounds nothing): two
PhotoUsage counters (usageCounters, deleted by a TTL index) count the
photos asked for, and give back those the model never returned (all of
them when it failed). A photo returned but not saved stays counted: Meta
billed it. At most PHOTO_CALLS_AT_ONCE calls run at once per process: a
call past that is a 503 before any quota is taken. Each call sends its
PHOTOS_PER_CALL requests to Meta at once, so a process has at most
PHOTO_CALLS_AT_ONCE x PHOTOS_PER_CALL (2 x 3 = 6) image requests in
flight.

Memory (Cloud Run gives the service 512 MiB): a call reads its references
(at most 25 MB together, photoStorage), scales each down to
MODEL_REFERENCE_MAX_SIDE one at a time (photoStorage.modelReference, a
JPEG decoded at a reduced scale), and encodes them for the request body
once (imageModel.imageInputs), which its PHOTOS_PER_CALL requests share,
read only: each request then holds only its own serialized body. So a call
peaks at its references, one shared encoding and three bodies of a few
scaled-down photos each, not three encodings of the full uploads.

One deadline, taken when the call starts, bounds the Storage reads and the
model calls; the model is skipped (a 503) with under MODEL_MIN_SECONDS
left. One model call failing keeps the others' photos (notGenerated says
some were not made). Each photo is recorded (a GeneratedPhotos document) right after its
upload: when a later one fails, the saved ones are still returned.

A product's uploaded photo is simply used: setUploadedProductPhotos,
called once after addStoreProducts for every product the new products form
sent with a photo (or for an existing product and a photo the admin
attached in chat), copies each upload into the store's photos as an
original, re-encoded with no metadata (photoStorage.storeOriginal: a
phone's EXIF location never goes public), and puts it on the product. No
model, so no photo quota: its own daily ORIGINALS_KIND counters bound the
copies instead, and its own slots (_copySlots) keep it off the generation
slots. An upload already copied for that product (its latest original's
sourcePaths) is put back on it, not copied again. The GeneratedPhotos
document keeps the productId. Its card offers to make better photos of
each product, with an edit box starting with defaultPhotoPrompt.
makeProductPhotoChoices makes them, only when the admin asks (on that card
or in chat): PHOTOS_PER_CALL photos from the product's original (its latest
original, else the photo on it) and one base prompt, so the admin picks one of
them all, that photo first (the chat's ProductPhotoChoiceCard). When fewer
can be made (a daily cap reached, the model unavailable or declining), that
photo and those made are still offered, with the reason.

The base prompt is the admin's own only from a form (the photo request
form, the photo offer card, whose edit box starts with defaultPhotoPrompt
and shows the shot lines read-only), else defaultPhotoPrompt (the store's
current one); what the admin types in chat is notes, added to every photo's
prompt as its last line ("Special request: ...").

findProductsInPhoto is the first step of adding several products from one
group photo the admin attached: productFinder.py names each product (Muse
Spark) and finds it (SAM, samModel.py), cuts each one out and saves it in
the admin's own users/<uid>/found/ folder, and returns each name with its
cut-out's URL, which the new products form then takes as that row's photo
(and setUploadedProductPhotos its original). Each store looks through at
most STORE_DAILY_FIND_CAP group photos a day, and all stores together at
most PRODUCT_FIND_DAILY_CAP_ALL (PhotoUsage counters, given back when the
find fails). A process looks through PRODUCT_FINDS_AT_ONCE photo at a time
(a 503 past it, before the quota).

Every photo tool's deadline (PHOTO_DEADLINE_SECONDS) and its saves'
(SAVE_SECONDS) are cut, inside a chat turn, to what the turn leaves
(turnDeadline.py), so the turn's model can still reply after it.

Every photo the model made keeps its full prompt (GeneratedPhoto.prompt:
the base, its style and shot lines, the notes), and
both photo tools return it on each photo: the admin sees what the model was
asked. No log carries a prompt.

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
setUploadedProductPhotos copied), never a URL the client or the AI sends.

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
import turnDeadline
from actions.callSlots import slot
from actions.ProductActions import (PRODUCT_NOT_FOUND, PRODUCTS,
                                    adminProductRow, validBatch)
from actions.StoreActions import (_validText, requireAnyOwnerStore,
                                  requireOwnerStore)
# ALL_STORES is imported for the tests, which read it here.
from actions.usageCounters import (ALL_STORES, appDay, envCap,
                                   giveBackQuotas, takeDailyQuotas)
from mcpTools import mcp_tool
from mongoDb import db
from services.__request import ServiceUnavailable
from objects import (PHOTO_NOTES_MAX_LENGTH, PHOTO_PROMPT_MAX_LENGTH,
                     FoundProductsResponse,
                     PHOTO_COLOR_FOCUS_MAX_LENGTH, PhotoColorsResponse,
                     PHOTO_REFERENCES_MAX, PHOTOS_PER_CALL,
                     PRODUCT_BATCH_MAX, PRODUCT_NAME_MAX_LENGTH,
                     AdminProductRow, GeneratedPhoto, GeneratedPhotosResponse,
                     ProductPhotoChoicesResponse, UploadedProductPhoto,
                     UploadedProductPhotosResponse, projectedCopy,
                     projection_of)
from utils import generateRandomString

GENERATED_PHOTOS = 'GeneratedPhotos'
PHOTO_USAGE = 'PhotoUsage'
# The kind of the photo counters, the store's and all stores'.
PHOTOS_KIND = 'photos'
# The kind of the group photo counters (findProductsInPhoto).
FINDS_KIND = 'productFinds'
# The kind of the focused color read counters (findColorsInPhoto).
COLOR_FINDS_KIND = 'colorFinds'
# The kind of the counters of originals copied (setUploadedProductPhotos).
ORIGINALS_KIND = 'originals'
# What a store reset carries over to the new store (StoreActions).
STORE_USAGE_KINDS = (PHOTO_USAGE, (PHOTOS_KIND, FINDS_KIND, COLOR_FINDS_KIND,
                                   ORIGINALS_KIND))
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
# Originals a store, and all stores together, copy a day
# (setUploadedProductPhotos: Storage reads, re-encodes and writes, no
# model): ORIGINAL_DAILY_CAP_ALL (0 turns it off), else the default. A
# store's cap is a day of busy listing (several full new products forms).
STORE_DAILY_ORIGINAL_CAP = 300
ORIGINAL_DAILY_CAP_ALL_VARIABLE = 'ORIGINAL_DAILY_CAP_ALL'
DEFAULT_ORIGINAL_DAILY_CAP_ALL = 5000
ORIGINAL_CAP_REACHED = ("Your store has reached today's limit of product "
                        'photos saved, try again tomorrow')
ORIGINAL_ALL_CAP_REACHED = 'Saving product photos is busy today, try again tomorrow'
ORIGINALS_TURNED_OFF = 'Saving product photos is turned off right now'
# The copies one process runs at once, apart from the generation slots:
# each holds a thread (inside a chat turn's, or an MCP call's) and decodes
# one photo at a time (up to MODEL_IMAGE_MAX_PIXELS), so this bounds the
# memory the copies take; a call past it is a 503 before any quota.
COPY_CALLS_AT_ONCE = 2
_copySlots = threading.BoundedSemaphore(COPY_CALLS_AT_ONCE)
# The photo calls one process runs at once: each holds one of gunicorn's 8
# threads for up to PHOTO_DEADLINE_SECONDS. With the chat's 4, 2 stay free
# for the rest of the app.
PHOTO_CALLS_AT_ONCE = 2
_photoSlots = threading.BoundedSemaphore(PHOTO_CALLS_AT_ONCE)
# The group photos one process looks through at once (findProductsInPhoto,
# inside a photo slot too): each holds its decoded photo (up to
# photoStorage.DECODE_MAX_PIXELS) for its whole naming and SAM calls, and
# two at once once took the service past its 512 MiB. A find past it is a
# 503 the admin can retry, before any quota is taken.
PRODUCT_FINDS_AT_ONCE = 1
_findSlots = threading.BoundedSemaphore(PRODUCT_FINDS_AT_ONCE)
# The call's reads and model call end by this deadline, and the saves get
# SAVE_SECONDS after the model answered; inside a chat turn both are cut to
# the turn's time (turnDeadline.toolDeadline / saveDeadline), so its model
# can still reply. Cloud Run's request timeout is what caps a request
# (gunicorn's gthread --timeout only watches the worker), and it is above
# both together.
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
PROMPT_LABEL = 'The prompt'
NOTES_LABEL = 'The notes'
PRODUCT_NAME_LABEL = "The product's name"
PHOTO_NOT_FOUND = 'Photo not found'
# setUploadedProductPhotos' refusals (before any write) and per-product
# failures (after).
UPLOADED_PHOTOS_COUNT = f'Send 1 to {PRODUCT_BATCH_MAX} products at a time'
PRODUCT_TWICE = 'this product is already in the list'
PHOTO_NOT_SAVED = 'Its photo could not be saved right now: try again'
# makeProductPhotoChoices for a product with no photo of the store's.
NO_PRODUCT_PHOTO = ('This product has no photo yet: photos are made from '
                    'the photo uploaded for it')
# What the AI reads of a store to write its default prompt.
PROMPT_STORE_FIELDS = {'_id': 1, 'photoStyle': 1, 'primaryColor': 1,
                       'secondaryColor': 1}
# Why a photo call made fewer than PHOTOS_PER_CALL photos (with a cap's own
# message, or the model's refusal).
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
# The lines every default base prompt starts with (defaultPhotoPrompt).
PHOTO_BASE_LINES = (
    'Create a professional e-commerce product photo of the item in the '
    'reference photo.',
    'Keep the product exactly as it is: its shape, colors, labels and '
    'printed text.',
    'Improve it only as a photographer would: clean, even light, sharp '
    'focus, true-to-life color, no dust, smudges or creases.',
    'Show only the product: leave out anything placed beside it, such as '
    'items for scale.',
    'No added text, logos, watermarks or people unless the photo style '
    'asks for them.',
)
# The fixed shot of each photo a call makes, in order: its label (the
# client shows it) and its line, whose {background}, {props} and
# {gradient} shotLines writes from the store's brand colors. The store's
# photo style goes on every shot but the first (STYLED_SHOTS_FROM).
PHOTO_SHOTS = (
    ('Catalog',
     'Shot: a three-quarter front view on {background}, soft studio light, '
     'a gentle shadow under the product, the product filling about 70% of '
     'the frame.'),
    ('In use',
     'Shot: the product where it is actually used, inferred from the '
     'product, with {props}, warm natural window light, a softly blurred '
     'background, the product in sharp focus.'),
    ('Hero',
     'Shot: a low or slightly top-down angle on {gradient}, rim lighting '
     "that brings out the product's edges, material and texture, a closer "
     'crop.'),
)
STYLED_SHOTS_FROM = 1
assert len(PHOTO_SHOTS) == PHOTOS_PER_CALL


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
    """The base prompt's line for the store's brand colors, or None when it
    has neither."""
    colors = [f'{name} {color}' for name, color in
              (('primary', primaryColor), ('secondary', secondaryColor))
              if color]
    if not colors:
        return None
    return (f'Brand colors: {", ".join(colors)} — for backgrounds and props '
            'only, never on the product.')


def shotLines(primaryColor: str | None,
              secondaryColor: str | None) -> list[dict]:
    """Each shot of PHOTO_SHOTS as {label, line}, its line naming the brand
    colors the store has (by role: their hex is in brandColorsLine, which
    shotPrompts adds when the base lacks it) and a neutral one for a color
    it lacks. Pure."""
    if primaryColor and secondaryColor:
        gradient = ('a smooth gradient from the primary to the secondary '
                    'brand color')
    elif primaryColor or secondaryColor:
        which = 'primary' if primaryColor else 'secondary'
        gradient = (f'a smooth gradient from the {which} brand color to a '
                    'soft neutral light grey')
    else:
        gradient = 'a soft neutral gradient'
    words = {
        'background': ('a seamless background in the primary brand color'
                       if primaryColor
                       else 'a seamless neutral light-grey background'),
        'props': ('a few simple props in the secondary brand color'
                  if secondaryColor else 'a few simple neutral props'),
        'gradient': gradient,
    }
    return [{'label': label, 'line': line.format(**words)}
            for label, line in PHOTO_SHOTS]


def storeShots(store: dict) -> list[dict]:
    """The store's shot lines (shotLines), as the prompts take them.
    `store` holds PROMPT_STORE_FIELDS."""
    return shotLines(store.get('primaryColor'), store.get('secondaryColor'))


# What the shot list the admin sees adds to a shot the photo style goes on
# (display only: shotPrompts adds the style itself).
STYLE_SHOT_SUFFIX = ' Plus your photo style.'


def cardShots(store: dict) -> list[dict]:
    """The store's shots as the photo offer card and the photo request
    form show them, read only: storeShots, each shot the store's photo
    style goes on (shotPrompts) ending with STYLE_SHOT_SUFFIX."""
    shots = storeShots(store)
    if store.get('photoStyle'):
        for shot in shots[STYLED_SHOTS_FROM:]:
            shot['line'] += STYLE_SHOT_SUFFIX
    return shots


def defaultPhotoPrompt(store: dict, productName: str | None = None) -> str:
    """The base prompt a photo call makes its photos from when the admin
    sends none (and the one their edit box starts with): PHOTO_BASE_LINES,
    the product's name (when known, so the model knows which item is the
    product) and the store's brand colors (when it has one). `store` holds
    PROMPT_STORE_FIELDS."""
    lines = list(PHOTO_BASE_LINES)
    if productName is not None:
        lines.append(f'Product: {productName}')
    colors = brandColorsLine(store.get('primaryColor'),
                             store.get('secondaryColor'))
    if colors is not None:
        lines.append(colors)
    return '\n'.join(lines)


def shotPrompts(store: dict, base: str, notes: str | None) -> list[str]:
    """The full prompt of each photo a call makes, in PHOTO_SHOTS' order:
    the base, the store's brand colors line (brandColorsLine, with the hex
    the shot lines name by role) when the store has a color and the base
    does not hold that line already (an admin's base may have dropped it),
    the store's photo style (from the STYLED_SHOTS_FROM-th shot on, when it
    has one), the shot's line, then the notes as the last line, the special
    request."""
    style = store.get('photoStyle')
    colors = brandColorsLine(store.get('primaryColor'),
                             store.get('secondaryColor'))
    prompts = []
    for i, shot in enumerate(storeShots(store)):
        lines = [base]
        if colors is not None and colors not in base:
            lines.append(colors)
        if style and i >= STYLED_SHOTS_FROM:
            lines.append(f'Style: {style}')
        lines.append(shot['line'])
        if notes is not None:
            lines.append(f'Special request: {notes}')
        prompts.append('\n'.join(lines))
    return prompts


def _prompts(store: dict, prompt, notes,
             productName: str | None) -> tuple[list[str], str | None]:
    """The prompts of a photo call (shotPrompts) and its notes: the base is
    the admin's own prompt from a form (stripped, at most
    PHOTO_PROMPT_MAX_LENGTH), else, null or blank, the default one
    (defaultPhotoPrompt); the notes typed in chat are stripped, at most
    PHOTO_NOTES_MAX_LENGTH, blank is none. A refusal names neither's
    text."""
    prompt = _validText(prompt, PROMPT_LABEL, PHOTO_PROMPT_MAX_LENGTH,
                        required=False)
    notes = _validText(notes, NOTES_LABEL, PHOTO_NOTES_MAX_LENGTH,
                       required=False)
    if prompt is None:
        prompt = defaultPhotoPrompt(store, productName)
    return shotPrompts(store, prompt, notes), notes


def _notGenerated(made: int) -> str | None:
    """Why a photo call that made `made` photos made fewer than
    PHOTOS_PER_CALL (the model returned fewer); null when it made them
    all."""
    return None if made >= PHOTOS_PER_CALL else SOME_NOT_MADE


def dailyCapAll() -> int:
    """PHOTO_DAILY_CAP_ALL, read at each call (usageCounters.envCap): unset
    or invalid, DEFAULT_PHOTO_DAILY_CAP_ALL."""
    return envCap(PHOTO_DAILY_CAP_ALL_VARIABLE, DEFAULT_PHOTO_DAILY_CAP_ALL,
                  'photoDailyCapAllInvalid')


def _takeStoreQuota(kind: str, storeId: str, storeCap: int, capAll: int,
                    refusals: tuple[str, str, str],
                    amount: int = 1) -> list[str]:
    """Count `amount` of `kind` (a PhotoUsage counter) on today's app day
    against the store's daily cap and all stores' (`capAll`, read by the
    caller: dailyCapAll or envCap), refused when either would pass (and
    then counted against neither: usageCounters.takeQuotas). `refusals`
    are the store cap's, all stores' and the turned-off (capAll 0)
    messages. Returns the two counters' keys."""
    storeRefusal, allRefusal, turnedOff = refusals
    return takeDailyQuotas(
        PHOTO_USAGE, [(kind, storeId, storeCap, storeRefusal)],
        (kind, allRefusal), capAll, turnedOff, appDay(_now()),
        amount=amount)


def _takePhotoQuota(storeId: str, count: int) -> list[str]:
    """Count `count` photos against the store's daily cap and all stores'
    (dailyCapAll). A cap of 0 is photos turned off."""
    return _takeStoreQuota(
        PHOTOS_KIND, storeId, STORE_DAILY_PHOTO_CAP, dailyCapAll(),
        (PHOTO_CAP_REACHED, PHOTO_ALL_CAP_REACHED, PHOTOS_TURNED_OFF),
        amount=count)


def _takeOriginalQuota(storeId: str, count: int) -> list[str]:
    """Count `count` originals copied against the store's daily cap and all
    stores' (ORIGINAL_DAILY_CAP_ALL)."""
    return _takeStoreQuota(
        ORIGINALS_KIND, storeId, STORE_DAILY_ORIGINAL_CAP,
        envCap(ORIGINAL_DAILY_CAP_ALL_VARIABLE,
               DEFAULT_ORIGINAL_DAILY_CAP_ALL, 'originalDailyCapAllInvalid'),
        (ORIGINAL_CAP_REACHED, ORIGINAL_ALL_CAP_REACHED,
         ORIGINALS_TURNED_OFF),
        amount=count)


def _takeFindQuota(storeId: str) -> list[str]:
    """Count one group photo against the store's daily cap and all
    stores' (PRODUCT_FIND_DAILY_CAP_ALL)."""
    return _takeStoreQuota(
        FINDS_KIND, storeId, STORE_DAILY_FIND_CAP,
        envCap(FIND_DAILY_CAP_ALL_VARIABLE, DEFAULT_FIND_DAILY_CAP_ALL,
               'productFindDailyCapAllInvalid'),
        (FIND_CAP_REACHED, FIND_ALL_CAP_REACHED, FINDS_TURNED_OFF))


def _takeColorFindQuota(storeId: str) -> list[str]:
    """Count one focused color read against the store's daily cap and all
    stores' (COLOR_FIND_DAILY_CAP_ALL)."""
    return _takeStoreQuota(
        COLOR_FINDS_KIND, storeId, STORE_DAILY_COLOR_FIND_CAP,
        envCap(COLOR_FIND_DAILY_CAP_ALL_VARIABLE,
               DEFAULT_COLOR_FIND_DAILY_CAP_ALL,
               'colorFindDailyCapAllInvalid'),
        (COLOR_FIND_CAP_REACHED, COLOR_FIND_ALL_CAP_REACHED,
         COLOR_FINDS_TURNED_OFF))


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
    (file name, bytes, content type), a WebP turned into a PNG or JPEG and
    one larger than MODEL_REFERENCE_MAX_SIDE scaled down
    (photoStorage.modelReference), one at a time."""
    references = []
    for i, (data, contentType) in enumerate(read):
        data, contentType = photoStorage.modelReference(data, contentType,
                                                        f'imageUrls[{i}]')
        extension = 'png' if contentType == 'image/png' else 'jpg'
        references.append((f'reference{i}.{extension}', data, contentType))
    return references


def _makeImages(userId: str, store: dict, paths: list[str],
                prompts: list[str], deadline: float,
                references: list[tuple[str, bytes, str]] | None = None
                ) -> list[tuple[bytes, str]]:
    """One photo of the store per prompt, from the reference photos at
    `paths` (or `references`, already read), each with its prompt, in the
    order of `prompts`: one model call each (n=1), all at once. They are
    counted against the daily caps: those the model did not return are
    given back (all of them when this fails). When one call fails and
    another returns a photo, those returned are kept (the failure logged,
    class only); when none returns any, the first call's failure is
    raised."""
    keys = _takePhotoQuota(store['_id'], len(prompts))
    images: list[tuple[bytes, str]] = []
    try:
        if references is None:
            references = _references(photoStorage.readOwnImages(
                userId,
                [(f'imageUrls[{i}]', path) for i, path in enumerate(paths)],
                deadline))
        if deadline - time.monotonic() < MODEL_MIN_SECONDS:
            raise imageModel.unavailable()
        # Encoded once, shared read only by the calls (the module
        # docstring's memory note).
        inputs = imageModel.imageInputs(references)
        with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            calls = [pool.submit(imageModel.editImages, references, prompt,
                                 1, deadline, inputs)
                     for prompt in prompts]
        failures = []
        for prompt, call in zip(prompts, calls):
            try:
                images += [(image, prompt) for image in call.result()[:1]]
            except Exception as error:
                failures.append(error)
        if failures and not images:
            raise failures[0]
        for error in failures:
            logging.warning({'event': 'productPhotoCallFailed',
                             'errorClass': type(error).__name__})
    finally:
        # Only the photos returned are billed: the rest are not counted.
        giveBackQuotas(PHOTO_USAGE, keys, len(prompts) - len(images))
    if not images:
        raise imageModel.unavailable()
    return images


def _savePhoto(userId: str, store: dict, paths: list[str], data: bytes,
               deadline: float, *, prompt: str | None = None,
               notes: str | None = None, productId: str | None = None,
               contentType: str = photoStorage.STORE_PHOTO_CONTENT_TYPE
               ) -> dict:
    """Upload one store photo and record it (a GeneratedPhotos document,
    with the product it is for when there is one): one the model made
    (with its prompt), or, with no prompt, the admin's original. Returns
    its _id and url."""
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
        productId=productId,
        model=None if original else imageModel.IMAGE_MODEL,
        source='original' if original else 'generated',
        prompt=prompt,
        createdAt=_now(),
        _version=0).model_dump(by_alias=True), GENERATED_PHOTOS)
    return {'_id': photoId, 'url': url}


def _saveImages(userId: str, store: dict, paths: list[str],
                images: list[tuple[bytes, str]], *, notes: str | None = None,
                productId: str | None = None) -> list[dict]:
    """Upload each image and record it (a GeneratedPhotos document, with
    its own prompt) right after, within SAVE_SECONDS. Returns the
    response's rows. When one fails, the saved ones are returned (the
    failure logged, class only); with none saved, it is raised."""
    deadline = turnDeadline.saveDeadline(SAVE_SECONDS)
    saved = []
    try:
        for image, prompt in images:
            saved.append({**_savePhoto(userId, store, paths, image, deadline,
                                       prompt=prompt, notes=notes,
                                       productId=productId),
                          'prompt': prompt})
    except Exception as error:
        if not saved:
            raise
        logging.warning({'event': 'productPhotoSaveFailed',
                         'errorClass': type(error).__name__,
                         'saved': len(saved),
                         'returned': len(images)})
    return saved


def _makeAndSave(userId: str, store: dict, paths: list[str],
                 prompts: list[str], deadline: float, *, notes: str | None = None,
                 productId: str | None = None,
                 readReferences=None,
                 keepRefusals: bool = False
                 ) -> tuple[int, list[dict], str | None]:
    """The core of both photo tools: in a photo slot, the references read
    (`readReferences()`, else the admin's photos at `paths`, after the
    quota is taken), one photo made per prompt of `prompts`, all at once
    (_makeImages), and saved (_saveImages). Returns how many the model
    returned, the rows saved and notGenerated. With `keepRefusals` a cap,
    the model declining or it being unavailable is notGenerated
    (CAPS_ORIGINAL_ONLY, UNAVAILABLE_ORIGINAL_ONLY) with nothing made;
    without, it is raised. A full slot, or a failed readReferences, is
    always raised."""
    with slot(_photoSlots, imageModel.unavailable):
        references = readReferences() if readReferences else None
        try:
            images = _makeImages(userId, store, paths, prompts, deadline,
                                 references)
            photos = _saveImages(userId, store, paths, images,
                                 notes=notes, productId=productId)
        except ValueError as refusal:
            # A cap reached, photos turned off, or the model declining:
            # the admin's to read.
            if not keepRefusals:
                raise
            return 0, [], CAPS_ORIGINAL_ONLY.get(str(refusal), str(refusal))
        except ServiceUnavailable:
            if not keepRefusals:
                raise
            return 0, [], UNAVAILABLE_ORIGINAL_ONLY
    return len(images), photos, _notGenerated(len(photos))


def generateProductPhotos(model,
                          userId: str,
                          imageUrls: list[str],
                          prompt: str | None = None,
                          productName: str | None = None,
                          notes: str | None = None) -> dict:
    deadline = turnDeadline.toolDeadline(PHOTO_DEADLINE_SECONDS)
    store = requireOwnerStore(userId, PROMPT_STORE_FIELDS)
    paths = _validImageUrls(userId, imageUrls)
    productName = _validText(productName, PRODUCT_NAME_LABEL,
                             PRODUCT_NAME_MAX_LENGTH, required=False)
    prompts, notes = _prompts(store, prompt, notes, productName)
    started = time.monotonic()
    returned, photos, notGenerated = _makeAndSave(userId, store, paths,
                                                  prompts, deadline,
                                                  notes=notes)
    # Never the URLs, the paths, the prompt, the notes or the key.
    logging.info({
        'event': 'productPhotos',
        'storeId': store['_id'],
        'count': PHOTOS_PER_CALL,
        'returned': returned,
        'saved': len(photos),
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return projectedCopy(model, {'photos': photos,
                                 'notGenerated': notGenerated})


def _validUpload(userId: str, item: dict, earlier: list) -> tuple[str, str]:
    """A setUploadedProductPhotos item as (productId, the photo's object
    path): a productId not sent before in the list, and the admin's own
    upload (never naming the URL)."""
    productId = item.get('productId')
    if not isinstance(productId, str) or not productId:
        raise ValueError(PRODUCT_NOT_FOUND)
    if any(productId == done for done, _ in earlier):
        raise ValueError(PRODUCT_TWICE)
    imageUrl = item.get('imageUrl')
    if not isinstance(imageUrl, str) or not imageUrl.strip():
        raise ValueError(IMAGE_URL_MISSING)
    path = photoStorage.ownImagePath(
        userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
        'imageUrl')
    return productId, path


def _putPhotoOnProduct(storeId: str, productId: str, photoUrl: str | None,
                       projection: dict) -> dict | None:
    """Set the product's photoUrl (the _version bump of every product
    write, Product) and return it as `projection` reads it; None when the
    store has no such product."""
    return db.findOneAndUpdate(
        {'_id': productId, 'storeId': storeId},
        {'$set': {'photoUrl': photoUrl}, '$inc': {'_version': 1}},
        PRODUCTS, upsert=False, projection=projection)


def _failure(error: Exception) -> str:
    """Why a product's uploaded photo was not saved, for the admin: a
    refusal's own message (it names no URL or path), else
    PHOTO_NOT_SAVED."""
    return str(error) if isinstance(error, ValueError) else PHOTO_NOT_SAVED


def _latestOriginal(storeId: str, productId: str) -> dict | None:
    """The product's latest original (setUploadedProductPhotos), the
    newest _id first on a createdAt tie; index (storeId, productId, source,
    createdAt, _id)."""
    return db.aggregate([
        {'$match': {'storeId': storeId, 'productId': productId,
                    'source': 'original'}},
        {'$sort': {'createdAt': -1, '_id': -1}},
        {'$limit': 1},
        {'$project': {'url': 1, 'path': 1, 'sourcePaths': 1, 'source': 1}},
    ], GENERATED_PHOTOS, findOne=True)


def _copyOriginal(userId: str, store: dict, productId: str, path: str,
                  deadline: float) -> dict:
    """Copy the admin's upload at `path` into the store's photos as the
    product's original, re-encoded (photoStorage.storeOriginal). Returns
    its _id and url."""
    [(data, contentType)] = photoStorage.readOwnImages(
        userId, [('imageUrl', path)], deadline)
    data, contentType = photoStorage.storeOriginal(data, contentType,
                                                   'imageUrl')
    return _savePhoto(userId, store, [path], data, deadline,
                      productId=productId, contentType=contentType)


def setUploadedProductPhotos(model,
                             userId: str,
                             products: list[UploadedProductPhoto]) -> dict:
    deadline = turnDeadline.toolDeadline(PHOTO_DEADLINE_SECONDS)
    store = requireOwnerStore(userId, PROMPT_STORE_FIELDS)
    if (not isinstance(products, list)
            or not 1 <= len(products) <= PRODUCT_BATCH_MAX):
        raise ValueError(UPLOADED_PHOTOS_COUNT)
    items = validBatch(products, UploadedProductPhoto, 'Product',
                       lambda item, earlier: _validUpload(userId, item,
                                                          earlier))
    names = {product['_id']: product['name'] for product in db.read(
        {'_id': {'$in': [productId for productId, _ in items]},
         'storeId': store['_id']}, PRODUCTS, projection={'name': 1})}
    for position, (productId, _) in enumerate(items, start=1):
        if productId not in names:
            raise ValueError(f'Product {position}: product not found')
    started = time.monotonic()
    done, notSaved, failures = [], [], []
    copied = reused = 0
    with slot(_copySlots, photoStorage.unavailable):
        # An upload already copied for its product is put back, not copied
        # (nor counted) again.
        latest = {productId: _latestOriginal(store['_id'], productId)
                  for productId, _ in items}
        toCopy = sum(1 for productId, path in items
                     if (latest[productId] or {}).get('sourcePaths')
                     != [path])
        keys = _takeOriginalQuota(store['_id'], toCopy) if toCopy else []
        try:
            for productId, path in items:
                original = latest[productId]
                try:
                    if (original or {}).get('sourcePaths') == [path]:
                        original = {'_id': original['_id'],
                                    'url': original['url']}
                        reused += 1
                    else:
                        original = _copyOriginal(userId, store, productId,
                                                 path, deadline)
                        copied += 1
                    if _putPhotoOnProduct(store['_id'], productId,
                                          original['url'],
                                          {'_id': 1}) is None:
                        # Deleted since the check: its photo stays an
                        # orphan.
                        raise ValueError(PRODUCT_NOT_FOUND)
                except Exception as error:
                    failures.append(error)
                    if not isinstance(error, (ValueError,
                                              ServiceUnavailable)):
                        logging.warning({'event': 'uploadedPhotoFailed',
                                         'errorClass': type(error).__name__})
                    notSaved.append({'productId': productId,
                                     'productName': names[productId],
                                     'reason': _failure(error)})
                    continue
                done.append({'productId': productId,
                             'productName': names[productId],
                             'photoId': original['_id'],
                             'photoUrl': original['url'],
                             'defaultPrompt': defaultPhotoPrompt(
                                 store, names[productId])})
        finally:
            # Only the copies made count.
            giveBackQuotas(PHOTO_USAGE, keys, toCopy - copied)
    # Never the URLs, the paths or the names.
    logging.info({
        'event': 'uploadedProductPhotos',
        'storeId': store['_id'],
        'copied': copied,
        'reused': reused,
        'notSaved': len(notSaved),
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    if not done:
        raise failures[0]
    return projectedCopy(model, {'products': done, 'notSaved': notSaved,
                                 'shots': cardShots(store)})


def _photoIdOfUrl(storeId: str, photoUrl) -> str | None:
    """The _id of the store photo whose download URL is `photoUrl`
    (storePhotoPath's file name), None for any other URL."""
    try:
        path = photoStorage.objectPathFromUrl(photoUrl, 'photoUrl')
    except ValueError:
        return None
    folder = photoStorage.storePhotosFolder(storeId)
    if not path.startswith(folder) or '/' in path[len(folder):]:
        return None
    return path[len(folder):].rpartition('.')[0] or None


def _productReference(storeId: str, product: dict) -> dict | None:
    """The store photo makeProductPhotoChoices makes a product's photos
    from: its latest original (setUploadedProductPhotos), else the photo
    on it, when that is one of the store's (an original copied before
    originals kept their product, or a photo made earlier); None without
    either."""
    projection = {'url': 1, 'path': 1, 'sourcePaths': 1, 'source': 1}
    original = _latestOriginal(storeId, product['_id'])
    if original is not None:
        return original
    photoId = _photoIdOfUrl(storeId, product.get('photoUrl'))
    if photoId is None:
        return None
    photo = db.read({'_id': photoId, 'storeId': storeId}, GENERATED_PHOTOS,
                    projection=projection, findOne=True)
    # The URL carries its own token: the photo on the product is this one.
    if photo is None or photo['url'] != product['photoUrl']:
        return None
    return photo


def makeProductPhotoChoices(model,
                            userId: str,
                            productId: str,
                            prompt: str | None = None,
                            notes: str | None = None) -> dict:
    deadline = turnDeadline.toolDeadline(PHOTO_DEADLINE_SECONDS)
    store = requireOwnerStore(userId, PROMPT_STORE_FIELDS)
    product = None
    if isinstance(productId, str) and productId:
        product = db.read({'_id': productId, 'storeId': store['_id']},
                          PRODUCTS, projection={'name': 1, 'photoUrl': 1},
                          findOne=True)
    if product is None:
        raise ValueError(PRODUCT_NOT_FOUND)
    prompts, notes = _prompts(store, prompt, notes, product['name'])
    reference = _productReference(store['_id'], product)
    if reference is None:
        raise ValueError(NO_PRODUCT_PHOTO)
    started = time.monotonic()
    returned, generated, notGenerated = _makeAndSave(
        userId, store, reference['sourcePaths'], prompts, deadline,
        notes=notes, productId=productId,
        readReferences=lambda: _references([photoStorage.readStorePhoto(
            store['_id'], reference['path'], deadline)]),
        keepRefusals=True)
    photos = [{'_id': reference['_id'], 'url': reference['url'],
               'source': reference.get('source', 'generated'),
               'prompt': None},
              *({**photo, 'source': 'generated'} for photo in generated)]
    # Never the URL, the path, the prompt, the notes or the refusal's text.
    logging.info({
        'event': 'productPhotoChoices',
        'storeId': store['_id'],
        'returned': returned,
        'saved': len(generated),
        'generated': notGenerated is None,
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return projectedCopy(model, {
        'productId': productId, 'productName': product['name'],
        'photos': photos, 'notGenerated': notGenerated})


def _saveFound(userId: str, found: list, keys: list[str]
               ) -> tuple[list[dict], list[str]]:
    """Save each cut-out of `found` (productFinder.FoundProduct) to the
    admin's found folder within SAVE_SECONDS (turnDeadline.saveDeadline),
    one after another. Returns the rows of those saved and the names of
    those not: a save failing keeps the cut-outs saved before it (returned,
    so none is left unused; the failure logged, class only) and stops; with
    none saved, the find's quota (`keys`) is given back and it is raised."""
    saveBy = turnDeadline.saveDeadline(SAVE_SECONDS)
    products = []
    for position, product in enumerate(found):
        try:
            saved = photoStorage.saveFoundPhoto(
                userId, generateRandomString(), product.photo, saveBy)
        except Exception as error:
            if not products:
                giveBackQuotas(PHOTO_USAGE, keys)
                raise
            logging.warning({'event': 'foundPhotoSaveFailed',
                             'errorClass': type(error).__name__,
                             'saved': len(products),
                             'found': len(found)})
            return products, [left.name for left in found[position:]]
        products.append({'name': product.name,
                         'imageUrl': photoStorage.publicPathUrl(saved)})
    return products, []


def findProductsInPhoto(model,
                        userId: str,
                        imageUrl: str) -> dict:
    deadline = turnDeadline.toolDeadline(PHOTO_DEADLINE_SECONDS)
    store = requireOwnerStore(userId, {'_id': 1})
    if not isinstance(imageUrl, str) or not imageUrl.strip():
        raise ValueError(IMAGE_URL_MISSING)
    path = photoStorage.ownImagePath(
        userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
        'imageUrl')
    started = time.monotonic()
    # The find slot first: a busy one takes no photo slot and no quota.
    with (slot(_findSlots, samModel.unavailable),
          slot(_photoSlots, samModel.unavailable)):
        keys = _takeFindQuota(store['_id'])
        try:
            [(data, _)] = photoStorage.readOwnImages(
                userId, [('imageUrl', path)], deadline)
            finding = productFinder.findProducts(data, deadline)
        except Exception:
            # A find that failed is not counted.
            giveBackQuotas(PHOTO_USAGE, keys)
            raise
        products, notSaved = _saveFound(userId, finding.found, keys)
    # Never the URL, the path or the names.
    logging.info({
        'event': 'productsInPhoto',
        'storeId': store['_id'],
        'found': len(products),
        'notFound': len(finding.notFound),
        'notSaved': len(notSaved),
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return projectedCopy(model, {'products': products,
                                 'notFound': finding.notFound + notSaved})


def findColorsInPhoto(model,
                      userId: str,
                      imageUrl: str,
                      focus: str | None = None) -> dict:
    deadline = turnDeadline.toolDeadline(PHOTO_DEADLINE_SECONDS)
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
    return projectedCopy(model, {
        'colors': [{'hex': color.hex, 'share': color.share,
                    'neutral': color.neutral}
                   for color in reading.colors],
        'focus': focus, 'focusFound': reading.focusFound})


def setProductPhoto(model,
                    userId: str,
                    productId: str,
                    photoId: str | None = None) -> dict:
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
        updated = _putPhotoOnProduct(store['_id'], productId, photoUrl,
                                     projection_of(model))
    if updated is None:
        raise ValueError(PRODUCT_NOT_FOUND)
    return adminProductRow(model, updated)


class PhotoTools():

    @mcp_tool('admin',
              thinkingText=['Making your product photos',
                            'Styling your photos',
                            'Creating new photos'],
              responseModel=GeneratedPhotosResponse,
              twinOf=None)
    def generateProductPhotos(self,
                              userId: str,
                              imageUrls: list[str],
                              prompt: str | None = None,
                              productName: str | None = None,
                              notes: str | None = None) -> dict:
        """Turn the admin's plain photos of one item into 3 polished
        product photos (a catalog shot, the item in use, a hero shot), all
        made from one base text of instructions for the image model (up to
        2000 characters), each with its own fixed shot added. imageUrls are 1 to 10 photos
        of the same item that the admin uploaded in the app (their
        download URLs, each once): never invent or change one. Pass
        the instructions the admin sent on a form answer: copy them
        character for character, never shorten, translate or reword them,
        and leave them out when the answer's instructions are null (an
        unchanged box on the form means the store's current default
        instructions, so null is sent). Never write these instructions
        yourself: what the admin asks for in chat ("make it brighter", "on
        a wooden table") goes in notes (up to 300 characters, in their
        words), which is added to whichever instructions are used as their
        last line, the special request. Left out, the store's default instructions are
        used (its brand colors, and its photo style on the 2nd and 3rd
        photos, set with updateMyStoreTheme). productName is optional: the product's name
        (up to 80 characters), when the photos are for one of the store's
        products, so the default instructions name it. A store makes at
        most 100 photos a day, and the app has a daily limit across all
        stores too. Returns each photo's _id, and notGenerated, why fewer
        than 3 were made (null when all were): tell the admin. The chat
        shows the photos, and the instructions they were made from, on a
        card. To show one in the shop, put it on its product with
        setProductPhoto. If this tool refuses, relay its message to the
        admin."""
        return generateProductPhotos(GeneratedPhotosResponse, userId,
                                     imageUrls, prompt, productName, notes)

    @mcp_tool('admin',
              thinkingText=['Putting your photos on your products',
                            'Saving your product photos',
                            'Adding the photos you uploaded'],
              responseModel=UploadedProductPhotosResponse,
              twinOf=None)
    def setUploadedProductPhotos(self,
                                 userId: str,
                                 products: list[UploadedProductPhoto]
                                 ) -> dict:
        """Put on products of the admin's store the photo the admin
        uploaded for each, as it is: no photo is generated. Call it once,
        right after addStoreProducts, with every product the new products
        form sent with an imageUrl (its new _id from addStoreProducts). It
        also puts a plain photo on any EXISTING product of the store (its
        productId from listMyStoreProducts): when the admin attaches a
        photo in chat for a product, pass that attached image's url. Each
        item is a productId and an imageUrl (never invent or change one),
        each product once, 1 to 50. A store saves at most 300 such photos a
        day (a photo it already holds for that product is not counted
        again), taken for the whole call at once: a call with more photos
        than are left today is refused and nothing is saved. Only the product ids and the photo URLs are checked first:
        a refusal names the product's position (Product 1 is the first)
        and nothing is saved; relay it. A photo that then cannot be read or
        saved is listed in notSaved with why, the others are still saved:
        tell the admin which. Returns the products whose photo is on; the
        chat shows them on a card that offers to make better photos of
        each, with instructions for the image model the admin can edit
        there. In one line, tell the admin their photos are on and that
        they can make better photos from them on the card; never call
        makeProductPhotoChoices unless they ask."""
        return setUploadedProductPhotos(UploadedProductPhotosResponse,
                                        userId, products)

    @mcp_tool('admin',
              thinkingText=['Styling your product photo',
                            'Making photo choices for your product',
                            'Preparing your photo options'],
              responseModel=ProductPhotoChoicesResponse,
              twinOf=None)
    def makeProductPhotoChoices(self,
                                userId: str,
                                productId: str,
                                prompt: str | None = None,
                                notes: str | None = None) -> dict:
        """Make better photos of one product of the admin's store (its
        productId from addStoreProducts or listMyStoreProducts) from the
        photo it has (the one setUploadedProductPhotos put on it, else the
        photo on it), only when the admin asks: on the photo offer card
        ("Make photos of ...", a form answer) or in chat. Makes 3 photos,
        which count against the daily photo limits (a catalog shot, the
        product in use, a hero shot), all from one base text of
        instructions for the image model (up to 2000 characters), each
        with its own fixed shot added: pass
        the instructions the admin sent on a form answer: copy them
        character for character, never shorten, translate or reword them,
        and leave them out when the answer's instructions are null (an
        unchanged box on the form means the store's current default
        instructions, so null is sent). Never write these instructions
        yourself: what the admin asks for in chat ("make it brighter", "on
        a wooden table") goes in notes (up to 300 characters, in their
        words), which is added to whichever instructions are used as their
        last line, the special request. Left out, the store's default instructions are
        used. When fewer are made (today's limit reached, generation
        unavailable), the photo they were to be made from and those made
        are still returned and notGenerated says why: tell the admin.
        Returns the photos (that photo first), each with its _id and
        source; the chat shows them, with the instructions they were made
        from, as a card the admin picks from, numbered in this order (1 is
        the photo they were made from). Call it for one product per turn;
        when the admin picks a photo on the card, or names one by its
        number ("use the 2nd"), call setProductPhoto with it. A product
        with no photo of its own is refused: ask the admin to attach a
        photo of it in chat (then setUploadedProductPhotos), or offer the
        photo request form. If this tool refuses, relay its message to the
        admin."""
        return makeProductPhotoChoices(ProductPhotoChoicesResponse, userId,
                                       productId, prompt, notes)

    @mcp_tool('admin',
              thinkingText=['Looking for products in your photo',
                            'Naming your products',
                            'Cutting out each product'],
              responseModel=FoundProductsResponse,
              twinOf=None)
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
        return findProductsInPhoto(FoundProductsResponse, userId, imageUrl)

    @mcp_tool('admin',
              thinkingText=['Reading the colors in your photo',
                            'Matching your colors',
                            'Picking out the main colors'],
              responseModel=PhotoColorsResponse,
              twinOf=None)
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
        return findColorsInPhoto(PhotoColorsResponse, userId, imageUrl, focus)

    @mcp_tool('admin',
              thinkingText=['Setting the product photo',
                            'Updating the product photo',
                            'Putting the photo on the product'],
              responseModel=AdminProductRow,
              twinOf=None)
    def setProductPhoto(self,
                        userId: str,
                        productId: str,
                        photoId: str | None = None) -> dict:
        """Show one of the store's product photos (its _id from
        generateProductPhotos, makeProductPhotoChoices or
        setUploadedProductPhotos) on any product of the admin's own store
        (its productId from listMyStoreProducts), whichever product the
        photo was first made for: customers see it in the shop. Leave
        photoId out to take the product's photo off. Confirm the product
        and the photo with the admin before calling, unless they picked it
        on a photo choice card or named its number there. If it refuses,
        relay its message to the admin."""
        return setProductPhoto(AdminProductRow, userId, productId, photoId)

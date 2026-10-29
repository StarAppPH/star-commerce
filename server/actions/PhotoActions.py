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

setProductPhoto shows one of the store's photos on one of its products
(Product.photoUrl, the photo's download URL): the shop shows it. Only a
photo the store made can be set, never a URL the client or the AI sends.

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
from objects import (PHOTO_NOTES_MAX_LENGTH, PHOTO_REFERENCES_MAX,
                     PHOTOS_PER_CALL_MAX, AdminProductRow, GeneratedPhoto,
                     GeneratedPhotosResponse, projectedCopy, projection_of)
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
PHOTO_COUNT = f'count is a whole number from 1 to {PHOTOS_PER_CALL_MAX}'
NOTES_LABEL = 'The notes'
PHOTO_NOT_FOUND = 'Photo not found'
PHOTO_INSTRUCTION = (
    'Create a professional e-commerce product photo of the item shown in '
    'the reference photos. Keep the product exactly as it is: its shape, '
    'colors, labels and printed text. Add no text, logos, watermarks or '
    'people unless the style asks for them.')


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


def photoPrompt(store: dict, notes: str | None) -> str:
    """The prompt of a photo call: the fixed instruction, the store's
    style, its brand colors (when set) and the admin's notes (when
    sent)."""
    lines = [PHOTO_INSTRUCTION, f'Style: {store["photoStyle"]}']
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


def _makeImages(userId: str, store: dict, paths: list[str], count: int,
                notes: str | None, deadline: float) -> list[bytes]:
    """`count` photos of the store from the reference photos at `paths`,
    counted against the daily caps: those the model did not return are
    given back (all of them when this fails)."""
    keys = _takePhotoQuota(store['_id'], count)
    images = []
    try:
        references = []
        read = photoStorage.readOwnImages(
            userId, [(f'imageUrls[{i}]', path) for i, path in enumerate(paths)],
            deadline)
        for i, (data, contentType) in enumerate(read):
            extension = 'png' if contentType == 'image/png' else 'jpg'
            references.append((f'reference{i}.{extension}', data,
                               contentType))
        if deadline - time.monotonic() < MODEL_MIN_SECONDS:
            raise imageModel.unavailable()
        images = imageModel.editImages(references, photoPrompt(store, notes),
                                       count, deadline)
    finally:
        # Only the photos returned are billed: the rest are not counted.
        giveBackQuotas(PHOTO_USAGE, keys, count - len(images))
    if not images:
        raise imageModel.unavailable()
    return images


def _saveImages(userId: str, store: dict, paths: list[str],
                notes: str | None, images: list[bytes]) -> list[dict]:
    """Upload each image and record it (a GeneratedPhotos document) right
    after, within SAVE_SECONDS. Returns the response's rows. When one
    fails, the saved ones are returned (the failure logged, class only);
    with none saved, it is raised."""
    deadline = time.monotonic() + SAVE_SECONDS
    saved = []
    try:
        for image in images:
            photoId = generateRandomString()
            url = photoStorage.saveStorePhoto(store['_id'], photoId, image,
                                              deadline)
            db.create(GeneratedPhoto(
                _id=photoId,
                storeId=store['_id'],
                createdBy=userId,
                path=photoStorage.storePhotoPath(store['_id'], photoId),
                url=url,
                sourcePaths=paths,
                notes=notes,
                model=imageModel.IMAGE_MODEL,
                createdAt=_now(),
                _version=0).model_dump(by_alias=True), GENERATED_PHOTOS)
            saved.append({'_id': photoId, 'url': url})
    except Exception as error:
        if not saved:
            raise
        logging.warning({'event': 'productPhotoSaveFailed',
                         'errorClass': type(error).__name__,
                         'saved': len(saved),
                         'returned': len(images)})
    return saved


class PhotoActions():

    @mcp_tool('admin')
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Photos created',
                  responseModel=GeneratedPhotosResponse)
    def generateProductPhotos(self,
                              userId: str,
                              imageUrls: list[str],
                              count: int = 1,
                              notes: str | None = None) -> dict:
        """Turn the admin's plain photos of one item into polished product
        photos in the store's look (its photo style and brand colors, set
        with updateMyStoreTheme: a store with no photo style is refused).
        imageUrls are 1 to 10 photos of the same item that the admin
        uploaded in the app (their download URLs, each once): never invent
        or change one. count is how many photos to make (1 to 4, default
        1); a store makes at most 100 a day, and the app has a daily limit
        across all stores too. notes is optional extra
        direction from the admin for these photos (up to 300 characters),
        e.g. "show it from above". Confirm the count with the admin before
        calling. Returns each photo's _id and url: to show one in the shop,
        put it on its product with setProductPhoto. If this tool refuses,
        relay its message to the admin."""
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
        started = time.monotonic()
        with slot(_photoSlots, imageModel.unavailable):
            images = _makeImages(userId, store, paths, count, notes,
                                 deadline)
            photos = _saveImages(userId, store, paths, notes, images)
        # Never the URLs, the paths, the prompt, the notes or the key.
        logging.info({
            'event': 'productPhotos',
            'storeId': store['_id'],
            'count': count,
            'returned': len(images),
            'saved': len(photos),
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'photos': photos}

    @mcp_tool('admin')
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Product photo set',
                  responseModel=AdminProductRow)
    def setProductPhoto(self,
                        userId: str,
                        productId: str,
                        photoId: str | None = None) -> dict:
        """Show one of the store's product photos (its _id from
        generateProductPhotos) on a product of the admin's store (its
        productId from listMyStoreProducts): customers see it in the shop.
        Leave photoId out to take the product's photo off. Confirm the
        product and the photo with the admin before calling. If it refuses,
        relay its message to the admin."""
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

"""PhotoActions: styled product photos from the admin's own photos (Meta's
Muse Image, imageModel.py).

The admin uploads plain photos of an item to their own users/<uid>/ folder
in the Firebase bucket; generateProductPhotos reads them there by bucket
and path (photoStorage, never by fetching a URL), asks the image model for
1 to PHOTOS_PER_CALL_MAX photos in the store's look (its photoStyle and
brand colors, StoreActions.updateMyStoreTheme), saves each under
stores/<storeId>/photos/ and returns their download URLs. Admins only, at
the store they own (requireOwnerStore); the call runs inside the request.

Each store makes at most STORE_DAILY_PHOTO_CAP photos a day (UTC+8): a
PhotoUsage counter (usageCounters, deleted by a TTL index) counts the
photos asked for, and gives back those the model never returned (all of
them when it failed). At most PHOTO_CALLS_AT_ONCE calls run at once per
process: a call past that is a 503 before any quota is taken.

The clock: every read of now is _now(), which reads this module's
`datetime`, so a test freezes it by replacing PhotoActions.datetime.
"""

import datetime
import logging
import threading
import time

import imageModel
import photoStorage
from actions.StoreActions import _validText, requireOwnerStore
from actions.usageCounters import (appDay, giveBackQuota, takeQuota,
                                   usageExpiry, usageKey)
from mcpTools import mcp_tool
from mongoDb import db
from objects import (PHOTO_NOTES_MAX_LENGTH, PHOTO_REFERENCES_MAX,
                     PHOTOS_PER_CALL_MAX, GeneratedPhoto,
                     GeneratedPhotosResponse)
from route_config import route_config
from utils import generateRandomString

GENERATED_PHOTOS = 'GeneratedPhotos'
PHOTO_USAGE = 'PhotoUsage'
# Read at import, so it stays the real timezone when a test replaces this
# module's `datetime`.
UTC = datetime.timezone.utc
STORE_DAILY_PHOTO_CAP = 100
# The photo calls one process runs at once: each holds one of gunicorn's 8
# threads for up to PHOTO_DEADLINE_SECONDS. With the chat's 4, 2 stay free
# for the rest of the app.
PHOTO_CALLS_AT_ONCE = 2
_photoSlots = threading.BoundedSemaphore(PHOTO_CALLS_AT_ONCE)
# The image model's share of the call (gunicorn's --timeout 120 is above
# it, with room for reading and saving the photos).
PHOTO_DEADLINE_SECONDS = 90
NO_PHOTO_STYLE = "Set your store's photo style first"
PHOTO_CAP_REACHED = ("Your store has reached today's photo limit, try again "
                     'tomorrow')
IMAGE_URLS_COUNT = f'Send 1 to {PHOTO_REFERENCES_MAX} imageUrls'
IMAGE_URLS_TWICE = 'imageUrls lists the same photo twice'
PHOTO_COUNT = f'count is a whole number from 1 to {PHOTOS_PER_CALL_MAX}'
NOTES_LABEL = 'The notes'
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
    paths = []
    for i, url in enumerate(imageUrls):
        label = f'imageUrls[{i}]'
        path = photoStorage.objectPathFromUrl(url, label)
        paths.append(photoStorage.ownImagePath(userId, path, label))
    if len(set(paths)) != len(paths):
        raise ValueError(IMAGE_URLS_TWICE)
    return paths


def _validCount(count) -> int:
    """How many photos to make: 1 to PHOTOS_PER_CALL_MAX (a bool is no
    number). The generated HTTP handler passes a missing body key as None,
    which is 1."""
    if count is None:
        return 1
    if (not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= PHOTOS_PER_CALL_MAX):
        raise ValueError(PHOTO_COUNT)
    return count


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


def _photoQuotaKey(storeId: str) -> tuple[str, datetime.datetime]:
    day = appDay(_now())
    return usageKey('photos', storeId, day), usageExpiry(day)


def _generate(userId: str, store: dict, paths: list[str], count: int,
              notes: str | None) -> list[dict]:
    """Make and save `count` photos of the store from the reference photos
    at `paths`, counting them against the store's daily cap: the photos
    the model did not return are given back. Returns the response's
    rows."""
    key, expireAt = _photoQuotaKey(store['_id'])
    if not takeQuota(PHOTO_USAGE, key, STORE_DAILY_PHOTO_CAP, expireAt,
                     amount=count):
        raise ValueError(PHOTO_CAP_REACHED)
    images = []
    try:
        references = []
        for i, path in enumerate(paths):
            data, contentType = photoStorage.readOwnImage(
                userId, path, f'imageUrls[{i}]')
            extension = 'png' if contentType == 'image/png' else 'jpg'
            references.append((f'reference{i}.{extension}', data,
                               contentType))
        images = imageModel.editImages(
            references, photoPrompt(store, notes), count,
            time.monotonic() + PHOTO_DEADLINE_SECONDS)
    finally:
        # Only the photos returned are billed: the rest are not counted.
        giveBackQuota(PHOTO_USAGE, key, count - len(images))
    if not images:
        raise imageModel.unavailable()
    now = _now()
    documents = []
    for image in images:
        photoId = generateRandomString()
        url = photoStorage.saveStorePhoto(store['_id'], photoId, image)
        documents.append(GeneratedPhoto(
            _id=photoId,
            storeId=store['_id'],
            createdBy=userId,
            path=photoStorage.storePhotoPath(store['_id'], photoId),
            url=url,
            sourcePaths=paths,
            notes=notes,
            model=imageModel.IMAGE_MODEL,
            createdAt=now,
            _version=0).model_dump(by_alias=True))
    db.createMany(documents, GENERATED_PHOTOS)
    return [{'_id': doc['_id'], 'url': doc['url']} for doc in documents]


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
        1); a store makes at most 100 a day. notes is optional extra
        direction from the admin for these photos (up to 300 characters),
        e.g. "show it from above". Confirm the count with the admin before
        calling. Returns each photo's _id and url. If this tool refuses,
        relay its message to the admin."""
        store = requireOwnerStore(userId, {'_id': 1, 'photoStyle': 1,
                                           'primaryColor': 1,
                                           'secondaryColor': 1})
        if not store.get('photoStyle'):
            raise ValueError(NO_PHOTO_STYLE)
        paths = _validImageUrls(userId, imageUrls)
        count = _validCount(count)
        notes = _validText(notes, NOTES_LABEL, PHOTO_NOTES_MAX_LENGTH,
                           required=False)
        if not _photoSlots.acquire(blocking=False):
            raise imageModel.unavailable()
        started = time.monotonic()
        try:
            photos = _generate(userId, store, paths, count, notes)
        finally:
            _photoSlots.release()
        # Never the URLs, the paths, the prompt, the notes or the key.
        logging.info({
            'event': 'productPhotos',
            'storeId': store['_id'],
            'count': count,
            'returned': len(photos),
            'durationMs': round((time.monotonic() - started) * 1000),
        })
        return {'photos': photos}

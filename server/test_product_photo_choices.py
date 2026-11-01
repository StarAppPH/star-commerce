"""A new product's uploaded photo (PhotoActions.setUploadedProductPhotos),
the better photos made from it only when the admin asks
(makeProductPhotoChoices), and the prompts both photo tools return.

The fakes are test_photos': a FakeBucket for Storage and a fake images
client for Meta, so no real call is made."""

import io
import logging
import re
import threading

import openai
import pytest

import imageModel
import photoStorage
from actions import PhotoActions, usageCounters
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addProduct, assertExactShape, createTestStore,
                      wipeCollections)
from mongoDb import db
from objects import (PHOTO_NOTES_MAX_LENGTH, PHOTO_PROMPT_MAX_LENGTH,
                     PHOTOS_PER_CALL,
                     PRODUCT_BATCH_MAX, ProductPhotoChoicesResponse,
                     UploadedProductPhotosResponse)
from services.__request import ServiceUnavailable
from test_photos import (ADMIN, FAR_DAY, FAR_EXPIRY, OTHER, ScriptedImages,
                         callsByShot, expectedPrompts,
                         clean, counterKey, counterValue, fakes, frozen,
                         noSleep, putObject, sdkError, slotsFree,
                         storedTheme, themedStoreWithPhoto, useImages, webp)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clean', 'fakes', 'frozen', 'noSleep']

api = ApiRequests()
tools = ToolRequests()


@pytest.fixture
def photoProducts():
    wipeCollections(['Products'])
    yield
    wipeCollections(['Products'])

def phoneJpeg() -> bytes:
    """A real JPEG as a phone takes it: 6 x 4, its EXIF saying to turn it
    (orientation 6) and naming the phone and a place."""
    from PIL import Image
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = 'SENTINEL-PHONE'
    exif[0x010E] = 'SENTINEL-PLACE'
    out = io.BytesIO()
    Image.new('RGB', (6, 4), (200, 30, 30)).save(out, 'JPEG', exif=exif)
    return out.getvalue()


def transparentPng() -> bytes:
    """A real PNG with transparency and a text chunk."""
    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text('Comment', 'SENTINEL-TEXT')
    out = io.BytesIO()
    Image.new('RGBA', (5, 3), (10, 200, 30, 128)).save(out, 'PNG',
                                                       pnginfo=info)
    return out.getvalue()


JPEG = phoneJpeg()
PNG = transparentPng()


def kept(data: bytes, contentType: str) -> tuple[bytes, str]:
    """The bytes and type the store keeps of an upload."""
    return photoStorage.storeOriginal(data, contentType, 'imageUrl')


def kindCounts(kind: str) -> list[int]:
    """The PhotoUsage counts of `kind` (the store's, then all stores')."""
    return sorted((doc['count'] for doc in db.read(
        {'_id': {'$regex': f'^{kind}:'}}, PhotoActions.PHOTO_USAGE)),
        reverse=True)


def usageCounts() -> list[int]:
    """The photo counters (the originals counters apart: originalCounts)."""
    return kindCounts(PhotoActions.PHOTOS_KIND)


def originalCounts() -> list[int]:
    return kindCounts(PhotoActions.ORIGINALS_KIND)


def copySlotsFree() -> int:
    got = 0
    while PhotoActions._copySlots.acquire(blocking=False):
        got += 1
    for _ in range(got):
        PhotoActions._copySlots.release()
    return got


def productWithPhoto(fakes, photoStyle='white marble', data=JPEG,
                     contentType='image/jpeg', name='Mug',
                     store=None) -> tuple[dict, dict, str]:
    """A store (themed when photoStyle is set), one of its products, and the
    download URL of the admin's photo of it."""
    if store is None:
        store = createTestStore(ADMIN)
        if photoStyle is not None:
            tools.updateMyStoreTheme(ADMIN, photoStyle=photoStyle,
                                     primaryColor='#112233')
    product = addProduct(store['_id'], name, 250)
    url = putObject(fakes, f'users/{ADMIN}/chat-photos/{name}.png', data,
                    contentType=contentType)
    return store, product, url


def uploadedProduct(fakes, **kwargs) -> tuple[dict, dict, dict]:
    """productWithPhoto, its photo put on the product
    (setUploadedProductPhotos): the store, the product, and its row."""
    store, product, url = productWithPhoto(fakes, **kwargs)
    res = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': product['_id'], 'imageUrl': url}])
    [row] = res['products']
    return store, product, row


def storedPhotos(store: dict) -> list[dict]:
    return sorted(db.read({'storeId': store['_id']}, 'GeneratedPhotos'),
                  key=lambda doc: doc['source'] != 'original')


def storedProduct(product: dict) -> dict:
    return db.read({'_id': product['_id']}, 'Products', findOne=True)


# ------------------------- setUploadedProductPhotos ------------------------- #


def test_uploadedPhotos_areOnTheProducts_asReencodedOriginals_withNoPhotoQuota(
        fakes, photoProducts):
    store, mug, mugUrl = productWithPhoto(fakes, data=PNG,
                                          contentType='image/png')
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)

    res = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl},
                {'productId': cup['_id'], 'imageUrl': cupUrl}])

    assertExactShape(UploadedProductPhotosResponse, res)
    assert res['notSaved'] == []
    assert [row['productId'] for row in res['products']] == [mug['_id'],
                                                            cup['_id']]
    assert [row['productName'] for row in res['products']] == ['Mug', 'Cup']
    byId = {doc['_id']: doc for doc in storedPhotos(store)}
    for row, product, upload, data, contentType in (
            (res['products'][0], mug, 'Mug.png', PNG, 'image/png'),
            (res['products'][1], cup, 'Cup.png', JPEG, 'image/jpeg')):
        doc = byId[row['photoId']]
        # Re-encoded in its own type, with no model and no prompt, and kept
        # as the product's original.
        assert (doc['source'], doc['model'], doc['prompt']) == (
            'original', None, None)
        assert doc['productId'] == product['_id']
        assert doc['sourcePaths'] == [f'users/{ADMIN}/chat-photos/{upload}']
        copied = fakes.bucket.objects[doc['path']]
        assert (copied['data'], copied['contentType']) == kept(data,
                                                               contentType)
        assert row['photoUrl'] == doc['url']
        assert row['defaultPrompt'] == PhotoActions.defaultPhotoPrompt(
            storedTheme(store), product['name'])
        stored = storedProduct(product)
        assert stored['photoUrl'] == doc['url']
        assert stored['_version'] == product['_version'] + 1
    # The shots better photos are made in, for the offer card; the default
    # base holds none of them.
    assert res['shots'] == PhotoActions.cardShots(storedTheme(store))
    assert all(shot['line'] not in row['defaultPrompt']
               for shot in res['shots'] for row in res['products'])
    # No photo quota, its own originals counters: the store's and all
    # stores'.
    assert usageCounts() == []
    assert originalCounts() == [2, 2]
    assert fakes.images.calls == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE
    assert copySlotsFree() == PhotoActions.COPY_CALLS_AT_ONCE


def test_storeOriginal_dropsTheMetadata_turnsItUpright_keepsTransparency():
    from PIL import Image
    data, contentType = kept(JPEG, 'image/jpeg')
    with Image.open(io.BytesIO(data)) as image:
        assert contentType == 'image/jpeg' and image.format == 'JPEG'
        # Turned upright as the EXIF said, and the EXIF gone.
        assert image.size == (4, 6)
        assert dict(image.getexif()) == {}
    assert b'SENTINEL' not in data
    for upload, contentType, format in (
            (PNG, 'image/png', 'PNG'),
            (webp('RGBA'), 'image/webp', 'WEBP')):
        data, keptType = kept(upload, contentType)
        assert keptType == contentType and b'SENTINEL' not in data
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == format and image.mode == 'RGBA'
            assert image.getpixel((0, 0))[3] < 255
    data, keptType = kept(webp('RGB'), 'image/webp')
    with Image.open(io.BytesIO(data)) as image:
        assert keptType == 'image/webp' and image.mode == 'RGB'
    with pytest.raises(ValueError, match="^imageUrl isn't a real"):
        kept(b'\xff\xd8\xff' + b'jpeg-bytes', 'image/jpeg')


def test_uploadedPhotos_aPhotoAttachedInChat_goesOnAnExistingProduct(
        fakes, photoProducts):
    store = createTestStore(ADMIN)
    product = addProduct(store['_id'], 'Old mug', 250)
    url = putObject(fakes, f'users/{ADMIN}/chat/abc/mug.jpg', JPEG)

    [row] = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': product['_id'], 'imageUrl': url}])['products']

    assert storedProduct(product)['photoUrl'] == row['photoUrl']
    assert fakes.images.calls == []


def test_uploadedPhotos_theSameUploadAgain_isPutBack_notCopiedAgain(
        fakes, photoProducts):
    store, product, url = productWithPhoto(fakes)
    item = [{'productId': product['_id'], 'imageUrl': url}]
    [first] = tools.setUploadedProductPhotos(ADMIN, item)['products']
    tools.setProductPhoto(ADMIN, product['_id'], None)
    uploads = len(fakes.bucket.uploads)

    [again] = tools.setUploadedProductPhotos(ADMIN, item)['products']

    assert again['photoId'] == first['photoId']
    assert again['photoUrl'] == first['photoUrl']
    assert storedProduct(product)['photoUrl'] == first['photoUrl']
    assert len(storedPhotos(store)) == 1
    assert len(fakes.bucket.uploads) == uploads
    assert originalCounts() == [1, 1]


def test_uploadedPhotos_pastTheStoresDailyCap_areRefused_beforeAnyCopy(
        fakes, frozen, photoProducts, monkeypatch):
    store, mug, mugUrl = productWithPhoto(fakes)
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    items = [{'productId': mug['_id'], 'imageUrl': mugUrl},
             {'productId': cup['_id'], 'imageUrl': cupUrl}]
    key = usageCounters.usageKey(PhotoActions.ORIGINALS_KIND, store['_id'],
                                 FAR_DAY)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE, key,
                            PhotoActions.STORE_DAILY_ORIGINAL_CAP, FAR_EXPIRY,
                            amount=PhotoActions.STORE_DAILY_ORIGINAL_CAP - 1)

    with pytest.raises(ValueError, match='^' + re.escape(
            PhotoActions.ORIGINAL_CAP_REACHED) + '$'):
        tools.setUploadedProductPhotos(ADMIN, items)
    monkeypatch.setenv(PhotoActions.ORIGINAL_DAILY_CAP_ALL_VARIABLE, '0')
    with pytest.raises(ValueError, match='^' + re.escape(
            PhotoActions.ORIGINALS_TURNED_OFF) + '$'):
        tools.setUploadedProductPhotos(ADMIN, items[:1])

    assert db.read({}, 'GeneratedPhotos') == []
    assert fakes.bucket.uploads == []
    assert originalCounts() == [PhotoActions.STORE_DAILY_ORIGINAL_CAP - 1]
    assert copySlotsFree() == PhotoActions.COPY_CALLS_AT_ONCE


def test_uploadedPhotos_aDifferentUploadForTheSameProduct_isCopiedAndCounted(
        fakes, photoProducts):
    store, mug, urlA = productWithPhoto(fakes)
    urlB = putObject(fakes, f'users/{ADMIN}/chat-photos/Mug-2.png', PNG,
                     contentType='image/png')
    [first] = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': urlA}])['products']

    [second] = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': urlB}])['products']

    assert second['photoId'] != first['photoId']
    assert second['photoUrl'] != first['photoUrl']
    assert len(storedPhotos(store)) == 2
    assert storedProduct(mug)['photoUrl'] == second['photoUrl']
    assert PhotoActions._latestOriginal(
        store['_id'], mug['_id'])['url'] == second['photoUrl']
    assert originalCounts() == [2, 2]


def test_uploadedPhotos_aReuseAtAnExhaustedCap_succeeds_aNewCopyIsRefused(
        fakes, frozen, photoProducts):
    store, mug, mugUrl = productWithPhoto(fakes)
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    mugItem = [{'productId': mug['_id'], 'imageUrl': mugUrl}]
    [first] = tools.setUploadedProductPhotos(ADMIN, mugItem)['products']
    key = usageCounters.usageKey(PhotoActions.ORIGINALS_KIND, store['_id'],
                                 FAR_DAY)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE, key,
                            PhotoActions.STORE_DAILY_ORIGINAL_CAP, FAR_EXPIRY,
                            amount=PhotoActions.STORE_DAILY_ORIGINAL_CAP - 1)
    full = originalCounts()
    assert full[0] == PhotoActions.STORE_DAILY_ORIGINAL_CAP

    [again] = tools.setUploadedProductPhotos(ADMIN, mugItem)['products']
    assert again['photoId'] == first['photoId']
    assert originalCounts() == full

    with pytest.raises(ValueError, match='^' + re.escape(
            PhotoActions.ORIGINAL_CAP_REACHED) + '$'):
        tools.setUploadedProductPhotos(
            ADMIN, [{'productId': cup['_id'], 'imageUrl': cupUrl}])
    assert storedProduct(cup)['photoUrl'] is None
    assert originalCounts() == full


def test_uploadedPhotos_aMixedBatch_countsOnlyTheCopy_listsTheFailure(
        fakes, photoProducts):
    store, mug, mugUrl = productWithPhoto(fakes)
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    plate = addProduct(store['_id'], 'Plate', 1)
    plateUrl = putObject(fakes, f'users/{ADMIN}/chat-photos/plate.png')
    del fakes.bucket.objects[f'users/{ADMIN}/chat-photos/plate.png']
    tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl}])
    assert originalCounts() == [1, 1]

    res = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl},
                {'productId': cup['_id'], 'imageUrl': cupUrl},
                {'productId': plate['_id'], 'imageUrl': plateUrl}])

    assert [row['productId'] for row in res['products']] == [
        mug['_id'], cup['_id']]
    assert [row['productId'] for row in res['notSaved']] == [plate['_id']]
    assert storedProduct(plate)['photoUrl'] is None
    assert len(storedPhotos(store)) == 2
    # The reuse and the failed copy are given back: one more copy counted.
    assert originalCounts() == [2, 2]


def test_uploadedPhotos_takeNoPhotoSlot_andAFullCopySlotIsA503(
        fakes, photoProducts):
    store, mug, mugUrl = productWithPhoto(fakes)
    item = [{'productId': mug['_id'], 'imageUrl': mugUrl}]
    for _ in range(PhotoActions.PHOTO_CALLS_AT_ONCE):
        PhotoActions._photoSlots.acquire()
    try:
        # Every photo slot taken: the copy still runs.
        assert tools.setUploadedProductPhotos(ADMIN, item)['products']
    finally:
        for _ in range(PhotoActions.PHOTO_CALLS_AT_ONCE):
            PhotoActions._photoSlots.release()
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    for _ in range(PhotoActions.COPY_CALLS_AT_ONCE):
        PhotoActions._copySlots.acquire()
    try:
        with pytest.raises(ServiceUnavailable):
            tools.setUploadedProductPhotos(
                ADMIN, [{'productId': cup['_id'], 'imageUrl': cupUrl}])
    finally:
        for _ in range(PhotoActions.COPY_CALLS_AT_ONCE):
            PhotoActions._copySlots.release()
    assert storedProduct(cup)['photoUrl'] is None
    assert originalCounts() == [1, 1]


def test_uploadedPhotos_anUnexpectedError_isNotSaved_loggingOnlyItsClass(
        fakes, photoProducts, monkeypatch, caplog):
    store, mug, mugUrl = productWithPhoto(fakes)
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    real = photoStorage.storeOriginal
    calls = []

    def failSecond(data, contentType, label):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('SENTINEL-ERROR-TEXT')
        return real(data, contentType, label)

    monkeypatch.setattr(photoStorage, 'storeOriginal', failSecond)
    with caplog.at_level(logging.INFO):
        res = tools.setUploadedProductPhotos(
            ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl},
                    {'productId': cup['_id'], 'imageUrl': cupUrl}])

    assert [row['productId'] for row in res['products']] == [mug['_id']]
    assert res['notSaved'] == [{'productId': cup['_id'],
                                'productName': 'Cup',
                                'reason': PhotoActions.PHOTO_NOT_SAVED}]
    assert 'RuntimeError' in caplog.text
    assert 'SENTINEL-ERROR-TEXT' not in caplog.text
    # Only the copy made is counted.
    assert originalCounts() == [1, 1]
    # Nothing saved: the first failure is raised.
    monkeypatch.setattr(photoStorage, 'storeOriginal',
                        lambda *args: (_ for _ in ()).throw(KeyError('x')))
    other = addProduct(store['_id'], 'Plate', 1)
    plateUrl = putObject(fakes, f'users/{ADMIN}/chat-photos/plate.jpg', JPEG)
    with pytest.raises(KeyError):
        tools.setUploadedProductPhotos(
            ADMIN, [{'productId': other['_id'], 'imageUrl': plateUrl}])
    assert originalCounts() == [1, 1]


def test_uploadedPhotos_refuseUpFront_namingThePosition_beforeAnyWrite(
        fakes, photoProducts):
    store, mug, mugUrl = productWithPhoto(fakes)
    otherStore = createTestStore(OTHER, 'Other store')
    theirProduct = addProduct(otherStore['_id'], 'Not mine', 1)
    theirs = putObject(fakes, f'users/{OTHER}/mug.jpg')
    mine = {'productId': mug['_id'], 'imageUrl': mugUrl}

    for products, message in (
            ([mine, {'productId': theirProduct['_id'], 'imageUrl': mugUrl}],
             'Product 2: product not found'),
            ([{'productId': 'no-such-product', 'imageUrl': mugUrl}],
             'Product 1: product not found'),
            ([{'productId': '', 'imageUrl': mugUrl}],
             'Product 1: product not found'),
            ([{'productId': mug['_id'], 'imageUrl': theirs}],
             'Product 1: imageUrl must be a photo you uploaded'),
            ([{'productId': mug['_id'], 'imageUrl': '  '}],
             'Product 1: ' + PhotoActions.IMAGE_URL_MISSING[:1].lower()
             + PhotoActions.IMAGE_URL_MISSING[1:]),
            ([mine, mine], 'Product 2: ' + PhotoActions.PRODUCT_TWICE),
            ([], PhotoActions.UPLOADED_PHOTOS_COUNT),
            ([mine] * (PRODUCT_BATCH_MAX + 1),
             PhotoActions.UPLOADED_PHOTOS_COUNT),
            ('not a list', PhotoActions.UPLOADED_PHOTOS_COUNT)):
        with pytest.raises(ValueError, match='^' + re.escape(message) + '$'):
            tools.setUploadedProductPhotos(ADMIN, products)
    assert db.read({}, 'GeneratedPhotos') == []
    assert storedProduct(mug) == mug
    assert storedProduct(theirProduct) == theirProduct
    assert fakes.bucket.uploads == []


def test_uploadedPhotos_aPhotoNotSaved_isListed_andTheOthersAreKept(
        fakes, photoProducts, caplog):
    store, mug, mugUrl = productWithPhoto(fakes)
    cup = addProduct(store['_id'], 'Cup', 100)
    gone = putObject(fakes, f'users/{ADMIN}/chat-photos/gone.png')
    del fakes.bucket.objects[f'users/{ADMIN}/chat-photos/gone.png']

    with caplog.at_level(logging.INFO):
        res = tools.setUploadedProductPhotos(
            ADMIN, [{'productId': cup['_id'], 'imageUrl': gone},
                    {'productId': mug['_id'], 'imageUrl': mugUrl}])

    assertExactShape(UploadedProductPhotosResponse, res)
    assert [row['productId'] for row in res['products']] == [mug['_id']]
    [missed] = res['notSaved']
    assert missed['productId'] == cup['_id']
    assert missed['productName'] == 'Cup'
    assert 'was not found' in missed['reason']
    assert storedProduct(cup)['photoUrl'] is None
    assert storedProduct(mug)['photoUrl'] == res['products'][0]['photoUrl']
    assert gone not in caplog.text and 'gone.png' not in caplog.text
    assert mugUrl not in caplog.text


def test_uploadedPhotos_aStorageFailure_isListedAsNotSavedNow(
        fakes, photoProducts, monkeypatch):
    store, mug, mugUrl = productWithPhoto(fakes)
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)
    fakes.bucket.uploadsThatWork = 1

    res = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl},
                {'productId': cup['_id'], 'imageUrl': cupUrl}])

    assert [row['productId'] for row in res['products']] == [mug['_id']]
    assert res['notSaved'] == [{'productId': cup['_id'],
                                'productName': 'Cup',
                                'reason': PhotoActions.PHOTO_NOT_SAVED}]


def test_uploadedPhotos_noneSaved_raisesTheFirstFailure(fakes, photoProducts):
    _, mug, mugUrl = productWithPhoto(fakes)
    fakes.bucket.uploadsThatWork = 0

    with pytest.raises(ServiceUnavailable):
        tools.setUploadedProductPhotos(
            ADMIN, [{'productId': mug['_id'], 'imageUrl': mugUrl}])
    assert db.read({}, 'GeneratedPhotos') == []
    assert storedProduct(mug)['photoUrl'] is None
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE
    assert copySlotsFree() == PhotoActions.COPY_CALLS_AT_ONCE
    assert originalCounts() == [0, 0]


# ------------------------- makeProductPhotoChoices ------------------------- #


def test_photoChoices_make3FromTheOriginal_oneCallPerShot(
        fakes, photoProducts):
    """Replaces "one prompt makes all 3 in one call": each photo is its own
    call, from the default base and its own shot."""
    store, product, row = uploadedProduct(fakes, data=PNG,
                                          contentType='image/png')

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert res['productId'] == product['_id'] and res['productName'] == 'Mug'
    assert [p['source'] for p in res['photos']] == [
        'original', 'generated', 'generated', 'generated']
    assert res['notGenerated'] is None
    calls = fakes.images.byShot()
    prompts = expectedPrompts(store, productName='Mug')
    assert [call['prompt'] for call in calls] == prompts
    for i, call in enumerate(calls):
        # Made from the original the store kept, a PNG sent as it is.
        assert call['image'] == [kept(PNG, 'image/png')]
        assert call['prompt'].startswith(row['defaultPrompt'] + '\n')
        assert ('\nStyle: white marble\n' in call['prompt']) is (i > 0)
        assert 'primary #112233' in call['prompt']
    # The original has no prompt; each one made carries its own.
    assert res['photos'][0] == {'_id': row['photoId'],
                                'url': row['photoUrl'],
                                'source': 'original', 'prompt': None}
    assert [p['prompt'] for p in res['photos'][1:]] == prompts
    original, *styled = storedPhotos(store)
    assert original['_id'] == row['photoId']
    assert len(styled) == PHOTOS_PER_CALL
    for doc in styled:
        assert doc['source'] == 'generated'
        assert doc['model'] == imageModel.IMAGE_MODEL
        assert doc['prompt'] in prompts
        assert doc['productId'] == product['_id']
        assert doc['sourcePaths'] == original['sourcePaths']
        assert doc['path'].endswith('.jpg')
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]
    # The pick goes on the product, the original as well as a styled one.
    for photo in (res['photos'][0], res['photos'][2]):
        picked = tools.setProductPhoto(ADMIN, product['_id'], photo['_id'])
        assert picked['photoUrl'] == photo['url']


def test_photoChoices_theDefaultPromptNamesTheProduct(fakes, photoProducts):
    _, product, _ = uploadedProduct(fakes)

    tools.makeProductPhotoChoices(ADMIN, product['_id'])

    for call in fakes.images.byShot():
        assert call['prompt'].split('\n')[:6] == [
            *PhotoActions.PHOTO_BASE_LINES, 'Product: Mug']


def test_photoChoices_theAdminsPrompt_isTheBaseOfAll3(fakes, photoProducts):
    """Replaces "the admin's prompt is the whole prompt": it is the base of
    each shot, stripped, and blank is the default base."""
    store, product, row = uploadedProduct(fakes)

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                        prompt='  The mug on a desk \n')

    prompts = expectedPrompts(store, 'The mug on a desk')
    assert [c['prompt'] for c in fakes.images.byShot()] == prompts
    assert [p['prompt'] for p in res['photos'][1:]] == prompts
    for doc in storedPhotos(store)[1:]:
        assert doc['prompt'] in prompts
    tools.makeProductPhotoChoices(ADMIN, product['_id'], prompt='  ')
    assert [c['prompt'] for c in callsByShot(fakes.images.calls[3:])] == (
        expectedPrompts(store, row['defaultPrompt']))
    calls = len(fakes.images.calls)
    with pytest.raises(ValueError) as error:
        tools.makeProductPhotoChoices(
            ADMIN, product['_id'], prompt='n' * (PHOTO_PROMPT_MAX_LENGTH + 1))
    assert str(error.value).startswith(PhotoActions.PROMPT_LABEL)
    assert len(fakes.images.calls) == calls


def test_photoChoices_notesAreTheLastLine_ofTheDefaultPrompt(
        fakes, photoProducts):
    store, product, row = uploadedProduct(fakes)

    tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                  notes='  on a wooden table \n')

    prompts = [c['prompt'] for c in fakes.images.byShot()]
    assert prompts == expectedPrompts(store, row['defaultPrompt'],
                                      'on a wooden table')
    assert all(p.endswith('\nSpecial request: on a wooden table')
               for p in prompts)
    for doc in storedPhotos(store)[1:]:
        assert doc['notes'] == 'on a wooden table'
        assert doc['prompt'] in prompts


def test_photoChoices_notesAreTheLastLine_ofTheAdminsPrompt(
        fakes, photoProducts):
    store, product, _ = uploadedProduct(fakes)

    tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                  prompt='The mug on a desk', notes='brighter')
    tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                  prompt='The mug on a desk', notes='   ')

    first = [c['prompt'] for c in callsByShot(fakes.images.calls[:3])]
    second = [c['prompt'] for c in callsByShot(fakes.images.calls[3:])]
    assert first == expectedPrompts(store, 'The mug on a desk', 'brighter')
    assert all(p.startswith('The mug on a desk\n')
               and p.endswith('\nSpecial request: brighter') for p in first)
    assert second == expectedPrompts(store, 'The mug on a desk')


def test_photoPromptAndNotes_atTheExactLimit_areAccepted(
        fakes, photoProducts):
    store, product, _ = uploadedProduct(fakes)
    prompt = 'p' * PHOTO_PROMPT_MAX_LENGTH
    notes = 'n' * PHOTO_NOTES_MAX_LENGTH

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'], prompt=prompt,
                                        notes=notes)

    assert len(res['photos']) == PHOTOS_PER_CALL + 1
    assert [c['prompt'] for c in fakes.images.byShot()] == expectedPrompts(
        store, prompt, notes)


def test_photoNotes_overTheLimit_areRefusedBeforeAnyQuota(
        fakes, photoProducts):
    store, product, _ = uploadedProduct(fakes)
    tooLong = 'n' * (PHOTO_NOTES_MAX_LENGTH + 1)
    upload = putObject(fakes, f'users/{ADMIN}/chat-photos/ref.jpg', JPEG)

    for call in (
            lambda: tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                                  notes=tooLong),
            lambda: tools.generateProductPhotos(ADMIN, [upload],
                                                notes=tooLong)):
        with pytest.raises(ValueError) as error:
            call()
        assert str(error.value).startswith(PhotoActions.NOTES_LABEL)
        assert tooLong not in str(error.value)
    assert usageCounts() == [] and fakes.images.calls == []
    assert len(storedPhotos(store)) == 1


def test_generateProductPhotos_notesAreTheLastLine_andKept(fakes):
    store, url = themedStoreWithPhoto(fakes)

    tools.generateProductPhotos(ADMIN, [url], prompt='on a desk',
                                notes='with a spoon')

    assert [c['prompt'] for c in fakes.images.byShot()] == expectedPrompts(
        store, 'on a desk', 'with a spoon')
    for doc in db.read({'storeId': store['_id']}, 'GeneratedPhotos'):
        assert doc['notes'] == 'with a spoon'


def test_photoChoices_withoutAPhotoStyle_make3Shots_withNoStyleLine(
        fakes, photoProducts):
    """Replaces "make 3 from the global prompt": all 3 shots are made, with
    no style line."""
    store, product, row = uploadedProduct(fakes, photoStyle=None)

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert [p['source'] for p in res['photos']] == [
        'original', 'generated', 'generated', 'generated']
    prompts = [c['prompt'] for c in fakes.images.byShot()]
    assert prompts == expectedPrompts(store, row['defaultPrompt'])
    assert all('Style:' not in p for p in prompts)
    assert res['notGenerated'] is None
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]


def test_photoChoices_pastTheStoresDailyCap_sayTheLimitWasReached(
        fakes, frozen, photoProducts):
    store, product, _ = uploadedProduct(fakes)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE,
                            counterKey(store['_id']),
                            PhotoActions.STORE_DAILY_PHOTO_CAP, FAR_EXPIRY,
                            amount=PhotoActions.STORE_DAILY_PHOTO_CAP - 2)

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assert [p['source'] for p in res['photos']] == ['original']
    assert res['photos'][0]['prompt'] is None
    assert res['notGenerated'] == PhotoActions.CAPS_ORIGINAL_ONLY[
        PhotoActions.PHOTO_CAP_REACHED]
    assert "today's photo limit" in res['notGenerated']
    assert fakes.images.calls == []
    assert counterValue(store['_id']) == PhotoActions.STORE_DAILY_PHOTO_CAP - 2
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_photoChoices_whenTheModelFails_offerTheOriginal_andGiveAllBack(
        fakes, monkeypatch, noSleep, photoProducts):
    store, product, _ = uploadedProduct(fakes)
    # Every call fails, after its one retry.
    useImages(monkeypatch, ScriptedImages(perShot=[
        [sdkError(openai.InternalServerError, 500) for _ in range(2)]
        for _ in range(PHOTOS_PER_CALL)]))

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assert [p['source'] for p in res['photos']] == ['original']
    assert res['notGenerated'] == PhotoActions.UNAVAILABLE_ORIGINAL_ONLY
    assert res['photos'][0]['prompt'] is None
    assert usageCounts() == [0, 0]
    assert [doc['source'] for doc in storedPhotos(store)] == ['original']


def test_photoChoices_takeOnlyTheStoresProduct_withAPhotoOfItsOwn(
        fakes, photoProducts):
    """Replaces the imageUrl checks (the tool takes no URL now): a product
    of another store is not found, and one with no photo is refused."""
    store, product, _ = uploadedProduct(fakes)
    otherStore = createTestStore(OTHER, 'Other store')
    otherProduct = addProduct(otherStore['_id'], 'Not mine', 1)
    bare = addProduct(store['_id'], 'Bare', 1)
    before = db.read({}, 'GeneratedPhotos')

    for productId in (otherProduct['_id'], 'no-such-product', '', None):
        with pytest.raises(ValueError, match='^Product not found$'):
            tools.makeProductPhotoChoices(ADMIN, productId)
    with pytest.raises(ValueError, match='^' + re.escape(
            PhotoActions.NO_PRODUCT_PHOTO) + '$'):
        tools.makeProductPhotoChoices(ADMIN, bare['_id'])
    assert db.read({}, 'GeneratedPhotos') == before
    assert fakes.images.calls == []
    assert usageCounts() == []


def test_photoChoices_withoutAKeptOriginal_useThePhotoOnTheProduct(
        fakes, photoProducts):
    """A product whose original was copied before originals kept their
    product (or whose photo was made earlier) is made from the store photo
    on it."""
    store, product, row = uploadedProduct(fakes)
    db.db['GeneratedPhotos'].update_one({'_id': row['photoId']},
                                        {'$unset': {'productId': ''}})

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assert res['photos'][0]['_id'] == row['photoId']
    assert [c['image'] for c in fakes.images.byShot()] == [
        [kept(JPEG, 'image/jpeg')]] * PHOTOS_PER_CALL
    # A photo URL that is no store photo of this store is no reference.
    db.db['GeneratedPhotos'].update_many({'storeId': store['_id']},
                                         {'$unset': {'productId': ''}})
    db.db['Products'].update_one(
        {'_id': product['_id']},
        {'$set': {'photoUrl': row['photoUrl'].replace('token=', 'token=x')}})
    with pytest.raises(ValueError, match='^' + re.escape(
            PhotoActions.NO_PRODUCT_PHOTO) + '$'):
        tools.makeProductPhotoChoices(ADMIN, product['_id'])


def test_photoChoices_theLatestOriginal_isTheReference(fakes, photoProducts):
    store, product, first = uploadedProduct(fakes)
    newer = putObject(fakes, f'users/{ADMIN}/chat-photos/newer.png', PNG,
                      contentType='image/png')
    [second] = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': product['_id'], 'imageUrl': newer}])['products']

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'])

    assert second['photoId'] != first['photoId']
    assert res['photos'][0]['_id'] == second['photoId']
    assert [c['image'] for c in fakes.images.byShot()] == [
        [kept(PNG, 'image/png')]] * PHOTOS_PER_CALL


def test_photoChoices_theOriginalGoneFromStorage_isRefused(fakes,
                                                           photoProducts):
    store, product, row = uploadedProduct(fakes)
    [original] = storedPhotos(store)
    del fakes.bucket.objects[original['path']]

    with pytest.raises(ValueError, match='^' + re.escape(
            photoStorage.STORE_PHOTO_GONE) + '$'):
        tools.makeProductPhotoChoices(ADMIN, product['_id'])
    assert usageCounts() == [] and fakes.images.calls == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_photoChoices_logNeverThePrompt(fakes, photoProducts, caplog):
    _, product, row = uploadedProduct(fakes)

    with caplog.at_level(logging.INFO):
        tools.makeProductPhotoChoices(ADMIN, product['_id'],
                                      prompt='SENTINEL-PROMPT-TEXT')

    assert 'SENTINEL-PROMPT-TEXT' not in caplog.text
    assert row['photoUrl'] not in caplog.text


def test_generateProductPhotos_returnsAndKeepsEachPhotosPrompt(fakes):
    store, url = themedStoreWithPhoto(fakes)

    res = tools.generateProductPhotos(ADMIN, [url], prompt='on a desk')

    expected = expectedPrompts(store, 'on a desk')
    assert [c['prompt'] for c in fakes.images.byShot()] == expected
    prompts = {p['_id']: p['prompt'] for p in res['photos']}
    # Each photo its own full prompt (the "Prompt used" of each).
    assert list(prompts.values()) == expected
    for doc in db.read({'storeId': store['_id']}, 'GeneratedPhotos'):
        assert doc['prompt'] == prompts[doc['_id']]
        assert doc['source'] == 'generated'


def test_theOriginalLookup_usesTheProductIndex(fakes, photoProducts):
    from test_policies import _indexNames
    store, product, _ = uploadedProduct(fakes)
    db.ensureIndexes()

    plan = db.db.command('explain', {
        'aggregate': 'GeneratedPhotos', 'cursor': {}, 'pipeline': [
            {'$match': {'storeId': store['_id'],
                        'productId': product['_id'], 'source': 'original'}},
            {'$sort': {'createdAt': -1, '_id': -1}},
            {'$limit': 1}]})

    assert ('storeId_1_productId_1_source_1_createdAt_-1__id_-1'
            in _indexNames(plan))


# ------------------------ the decode cap's refusals ------------------------ #


def overCapPng() -> bytes:
    """A PNG over the decode cap the tests below set (1000 pixels)."""
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGBA', (40, 40), (10, 20, 30, 200)).save(out, 'PNG')
    return out.getvalue()


def test_decodeCap_aReferenceOverIt_isRefused_andGivesThePhotoQuotaBack(
        fakes, frozen, monkeypatch):
    monkeypatch.setattr(photoStorage, 'DECODE_MAX_PIXELS', 1000)
    monkeypatch.setattr(photoStorage, 'MODEL_REFERENCE_MAX_SIDE', 20)
    store = createTestStore(ADMIN)
    url = putObject(fakes, f'users/{ADMIN}/big.png', overCapPng(),
                    contentType='image/png')

    with pytest.raises(ValueError, match=r'^imageUrls\[0\] ' + re.escape(
            photoStorage.DECODE_TOO_LARGE) + '$'):
        tools.generateProductPhotos(ADMIN, [url])

    # Taken before the read, and all given back.
    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert fakes.images.calls == []
    assert db.read({}, 'GeneratedPhotos') == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_decodeCap_anUploadOverIt_isRefused_andGivesTheOriginalsQuotaBack(
        fakes, photoProducts, monkeypatch):
    monkeypatch.setattr(photoStorage, 'DECODE_MAX_PIXELS', 1000)
    store, mug, url = productWithPhoto(fakes, data=overCapPng(),
                                       contentType='image/png')
    _, cup, cupUrl = productWithPhoto(fakes, name='Cup', store=store)

    res = tools.setUploadedProductPhotos(
        ADMIN, [{'productId': mug['_id'], 'imageUrl': url},
                {'productId': cup['_id'], 'imageUrl': cupUrl}])

    # The one over the cap is not saved, with why; the other is.
    assert res['notSaved'] == [{
        'productId': mug['_id'], 'productName': 'Mug',
        'reason': f'imageUrl {photoStorage.DECODE_TOO_LARGE}'}]
    assert [row['productId'] for row in res['products']] == [cup['_id']]
    assert originalCounts() == [1, 1]
    # Alone, it is raised, and its count given back.
    with pytest.raises(ValueError, match=re.escape(
            photoStorage.DECODE_TOO_LARGE)):
        tools.setUploadedProductPhotos(
            ADMIN, [{'productId': mug['_id'], 'imageUrl': url}])
    assert originalCounts() == [1, 1]
    assert storedProduct(mug)['photoUrl'] is None

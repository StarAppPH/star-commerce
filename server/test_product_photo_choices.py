"""A new product's photo choices (PhotoActions.makeProductPhotoChoices) and
the prompt both photo tools return.

The fakes are test_photos': a FakeBucket for Storage and a fake images
client for Meta, so no real call is made."""

import re

import openai
import pytest

import imageModel
from actions import PhotoActions, usageCounters
from ApiRequests import ApiRequests
from conftest import (addProduct, assertExactShape, createTestStore,
                      wipeCollections)
from mongoDb import db
from objects import PHOTO_CHOICES_GENERATED, ProductPhotoChoicesResponse
from test_photos import (ADMIN, FAR_EXPIRY, JPEG, OTHER, ScriptedImages,
                         clean, counterKey, counterValue, fakes, frozen,
                         noSleep, putObject, sdkError, slotsFree,
                         themedStoreWithPhoto, usageCounts, useImages)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clean', 'fakes', 'frozen', 'noSleep']

api = ApiRequests()


@pytest.fixture
def photoProducts():
    wipeCollections(['Products'])
    yield
    wipeCollections(['Products'])

PNG = b'\x89PNG\r\n\x1a\n' + b'png-bytes'


def productWithPhoto(fakes, photoStyle='white marble', data=JPEG,
                     contentType='image/jpeg') -> tuple[dict, dict, str]:
    """A store (themed when photoStyle is set), one of its products, and the
    download URL of the admin's photo of it."""
    store = createTestStore(ADMIN)
    if photoStyle is not None:
        api.updateMyStoreTheme(ADMIN, photoStyle=photoStyle,
                               primaryColor='#112233')
    product = addProduct(store['_id'], 'Mug', 250)
    url = putObject(fakes, f'users/{ADMIN}/chat-photos/mug.png', data,
                    contentType=contentType)
    return store, product, url


def storedPhotos(store: dict) -> list[dict]:
    return sorted(db.read({'storeId': store['_id']}, 'GeneratedPhotos'),
                  key=lambda doc: doc['source'] != 'original')


def test_photoChoices_keepTheOriginal_andMake3StyledWithTheirPrompt(
        fakes, photoProducts):
    store, product, url = productWithPhoto(fakes, data=PNG,
                                           contentType='image/png')
    fakes.images.returned = PHOTO_CHOICES_GENERATED

    res = api.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert res['productId'] == product['_id'] and res['productName'] == 'Mug'
    assert [p['source'] for p in res['photos']] == [
        'original', 'generated', 'generated', 'generated']
    assert res['notGenerated'] is None
    [call] = fakes.images.calls
    assert call['n'] == PHOTO_CHOICES_GENERATED
    assert call['image'] == [(PNG, 'image/png')]
    assert res['prompt'] == call['prompt']
    assert 'Style: white marble' in res['prompt']
    assert 'primary #112233' in res['prompt']
    original, *styled = storedPhotos(store)
    # The original is copied as it is, a PNG kept a PNG, with no model and
    # no prompt; it counts against no photo quota.
    assert original['_id'] == res['photos'][0]['_id']
    assert original['path'] == (
        f'stores/{store["_id"]}/photos/{original["_id"]}.png')
    copied = fakes.bucket.objects[original['path']]
    assert (copied['data'], copied['contentType']) == (PNG, 'image/png')
    assert original['model'] is None and original['prompt'] is None
    assert original['sourcePaths'] == [f'users/{ADMIN}/chat-photos/mug.png']
    for doc in styled:
        assert doc['source'] == 'generated'
        assert doc['model'] == imageModel.IMAGE_MODEL
        assert doc['prompt'] == res['prompt']
        assert doc['path'].endswith('.jpg')
    assert usageCounts() == [3, 3]
    # The pick goes on the product, the original as well as a styled one.
    for photo in (res['photos'][0], res['photos'][2]):
        row = api.setProductPhoto(ADMIN, product['_id'], photo['_id'])
        assert row['photoUrl'] == photo['url']


def test_photoChoices_thePromptNamesTheProduct(fakes, photoProducts):
    _, product, url = productWithPhoto(fakes)
    fakes.images.returned = PHOTO_CHOICES_GENERATED

    res = api.makeProductPhotoChoices(ADMIN, product['_id'], url)

    [call] = fakes.images.calls
    assert call['prompt'].split('\n')[:2] == [PhotoActions.PHOTO_INSTRUCTION,
                                              'Product: Mug']
    assert res['prompt'] == call['prompt']


def test_photoChoices_withoutAPhotoStyle_offerTheOriginalOnly(fakes,
                                                             photoProducts):
    _, product, url = productWithPhoto(fakes, photoStyle=None)

    res = api.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert [p['source'] for p in res['photos']] == ['original']
    assert res['prompt'] is None
    assert res['notGenerated'] == PhotoActions.NO_STYLE_ORIGINAL_ONLY
    assert fakes.images.calls == []
    assert usageCounts() == []


def test_photoChoices_pastTheStoresDailyCap_sayTheLimitWasReached(
        fakes, frozen, photoProducts):
    store, product, url = productWithPhoto(fakes)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE,
                            counterKey(store['_id']),
                            PhotoActions.STORE_DAILY_PHOTO_CAP, FAR_EXPIRY,
                            amount=PhotoActions.STORE_DAILY_PHOTO_CAP - 2)

    res = api.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assert [p['source'] for p in res['photos']] == ['original']
    assert res['prompt'] is None
    assert res['notGenerated'] == PhotoActions.CAPS_ORIGINAL_ONLY[
        PhotoActions.PHOTO_CAP_REACHED]
    assert "today's photo limit" in res['notGenerated']
    assert fakes.images.calls == []
    assert counterValue(store['_id']) == PhotoActions.STORE_DAILY_PHOTO_CAP - 2
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_photoChoices_whenTheModelFails_offerTheOriginal_andGiveAllBack(
        fakes, monkeypatch, noSleep, photoProducts):
    store, product, url = productWithPhoto(fakes)
    useImages(monkeypatch, ScriptedImages(
        sdkError(openai.InternalServerError, 500),
        sdkError(openai.InternalServerError, 502)))

    res = api.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assert [p['source'] for p in res['photos']] == ['original']
    assert res['notGenerated'] == PhotoActions.UNAVAILABLE_ORIGINAL_ONLY
    assert res['prompt'] is None
    assert usageCounts() == [0, 0]
    assert [doc['source'] for doc in storedPhotos(store)] == ['original']


def test_photoChoices_takeOnlyTheStoresProduct_andTheAdminsOwnPhoto(
        fakes, photoProducts):
    store, product, url = productWithPhoto(fakes)
    otherStore = createTestStore(OTHER, 'Other store')
    otherProduct = addProduct(otherStore['_id'], 'Not mine', 1)
    theirs = putObject(fakes, f'users/{OTHER}/mug.jpg')

    for productId in (otherProduct['_id'], 'no-such-product', '', None):
        with pytest.raises(ValueError, match='^Product not found$'):
            api.makeProductPhotoChoices(ADMIN, productId, url)
    with pytest.raises(ValueError, match='^imageUrl must be a photo you '
                       'uploaded$') as error:
        api.makeProductPhotoChoices(ADMIN, product['_id'], theirs)
    assert theirs not in str(error.value)
    for missing in (None, '', '  '):
        with pytest.raises(ValueError, match='^' + re.escape(
                PhotoActions.IMAGE_URL_MISSING) + '$'):
            api.makeProductPhotoChoices(ADMIN, product['_id'], missing)
    assert db.read({}, 'GeneratedPhotos') == []
    assert fakes.images.calls == []
    assert usageCounts() == []


def test_generateProductPhotos_returnsAndKeepsItsPrompt(fakes):
    store, url = themedStoreWithPhoto(fakes)

    res = api.generateProductPhotos(ADMIN, [url], count=2, notes='on a desk')

    [call] = fakes.images.calls
    assert res['prompt'] == call['prompt']
    assert 'Notes: on a desk' in res['prompt']
    for doc in db.read({'storeId': store['_id']}, 'GeneratedPhotos'):
        assert doc['prompt'] == res['prompt']
        assert doc['source'] == 'generated'

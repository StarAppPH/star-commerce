"""A new product's photo choices (PhotoActions.makeProductPhotoChoices) and
the prompts both photo tools return.

The fakes are test_photos': a FakeBucket for Storage and a fake images
client for Meta, so no real call is made."""

import re

import openai
import pytest

import imageModel
from actions import PhotoActions, usageCounters
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addProduct, assertExactShape, createTestStore,
                      wipeCollections)
from mongoDb import db
from objects import (PHOTOS_PER_CALL, PHOTOS_THEMED,
                     ProductPhotoChoicesResponse)
from test_photos import (ADMIN, FAR_EXPIRY, JPEG, OTHER, ScriptedImages,
                         clean, counterKey, counterValue, fakes, frozen,
                         noSleep, putObject, sdkError, slotsFree,
                         themedStoreWithPhoto, usageCounts, useImages)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clean', 'fakes', 'frozen', 'noSleep']

api = ApiRequests()
tools = ToolRequests()


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
        tools.updateMyStoreTheme(ADMIN, photoStyle=photoStyle,
                               primaryColor='#112233')
    product = addProduct(store['_id'], 'Mug', 250)
    url = putObject(fakes, f'users/{ADMIN}/chat-photos/mug.png', data,
                    contentType=contentType)
    return store, product, url


def storedPhotos(store: dict) -> list[dict]:
    return sorted(db.read({'storeId': store['_id']}, 'GeneratedPhotos'),
                  key=lambda doc: doc['source'] != 'original')


def test_photoChoices_keepTheOriginal_andMake3WithTheirPrompts(
        fakes, photoProducts):
    store, product, url = productWithPhoto(fakes, data=PNG,
                                           contentType='image/png')

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert res['productId'] == product['_id'] and res['productName'] == 'Mug'
    assert [p['source'] for p in res['photos']] == [
        'original', 'generated', 'generated', 'generated']
    assert res['notGenerated'] is None
    calls = fakes.images.byN()
    assert sorted(calls) == [1, PHOTOS_THEMED]
    for call in calls.values():
        assert call['image'] == [(PNG, 'image/png')]
    # Photo 1 is the product on a plain primary color background; the
    # themed ones use the brand colors as accents instead.
    assert calls[1]['prompt'].endswith(
        f'\n{PhotoActions.backgroundLine("#112233")}')
    assert 'Style:' not in calls[1]['prompt']
    assert 'primary #112233' in calls[PHOTOS_THEMED]['prompt']
    assert 'Background:' not in calls[PHOTOS_THEMED]['prompt']
    assert calls[PHOTOS_THEMED]['prompt'].endswith('\nStyle: white marble')
    # The original has no prompt; photo 2 the global one, 3 and 4 the
    # themed one.
    assert [p['prompt'] for p in res['photos']] == [
        None, calls[1]['prompt'],
        *[calls[PHOTOS_THEMED]['prompt']] * PHOTOS_THEMED]
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
    prompts = {p['_id']: p['prompt'] for p in res['photos']}
    for doc in styled:
        assert doc['source'] == 'generated'
        assert doc['model'] == imageModel.IMAGE_MODEL
        assert doc['prompt'] == prompts[doc['_id']]
        assert doc['path'].endswith('.jpg')
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]
    # The pick goes on the product, the original as well as a styled one.
    for photo in (res['photos'][0], res['photos'][2]):
        row = tools.setProductPhoto(ADMIN, product['_id'], photo['_id'])
        assert row['photoUrl'] == photo['url']


def test_photoChoices_thePromptNamesTheProduct(fakes, photoProducts):
    _, product, url = productWithPhoto(fakes)

    tools.makeProductPhotoChoices(ADMIN, product['_id'], url)

    for call in fakes.images.calls:
        assert call['prompt'].split('\n')[:2] == [
            PhotoActions.PHOTO_INSTRUCTION, 'Product: Mug']


def test_photoChoices_notesAreTheSpecialRequest_ofEveryPrompt(fakes,
                                                             photoProducts):
    store, product, url = productWithPhoto(fakes)

    tools.makeProductPhotoChoices(ADMIN, product['_id'], url,
                                notes='  on a desk ')

    assert len(fakes.images.calls) == 2
    for call in fakes.images.calls:
        assert call['prompt'].split('\n')[:3] == [
            PhotoActions.PHOTO_INSTRUCTION, 'Special request: on a desk',
            'Product: Mug']
    for doc in storedPhotos(store)[1:]:
        assert doc['notes'] == 'on a desk'
    with pytest.raises(ValueError):
        tools.makeProductPhotoChoices(ADMIN, product['_id'], url,
                                    notes='n' * 301)


def test_photoChoices_withoutAPhotoStyle_offerTheOriginalAndTheGlobalPhoto(
        fakes, photoProducts):
    _, product, url = productWithPhoto(fakes, photoStyle=None)

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assertExactShape(ProductPhotoChoicesResponse, res)
    assert [p['source'] for p in res['photos']] == ['original', 'generated']
    [call] = fakes.images.calls
    assert call['n'] == 1 and 'Style:' not in call['prompt']
    assert res['photos'][1]['prompt'] == call['prompt']
    assert res['notGenerated'] == PhotoActions.NO_STYLE_GLOBAL_ONLY
    assert usageCounts() == [1, 1]


def test_photoChoices_pastTheStoresDailyCap_sayTheLimitWasReached(
        fakes, frozen, photoProducts):
    store, product, url = productWithPhoto(fakes)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE,
                            counterKey(store['_id']),
                            PhotoActions.STORE_DAILY_PHOTO_CAP, FAR_EXPIRY,
                            amount=PhotoActions.STORE_DAILY_PHOTO_CAP - 2)

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'], url)

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
    store, product, url = productWithPhoto(fakes)
    # Both calls fail, each after its one retry.
    useImages(monkeypatch, ScriptedImages(
        *[sdkError(openai.InternalServerError, 500) for _ in range(4)]))

    res = tools.makeProductPhotoChoices(ADMIN, product['_id'], url)

    assert [p['source'] for p in res['photos']] == ['original']
    assert res['notGenerated'] == PhotoActions.UNAVAILABLE_ORIGINAL_ONLY
    assert res['photos'][0]['prompt'] is None
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
            tools.makeProductPhotoChoices(ADMIN, productId, url)
    with pytest.raises(ValueError, match='^imageUrl must be a photo you '
                       'uploaded$') as error:
        tools.makeProductPhotoChoices(ADMIN, product['_id'], theirs)
    assert theirs not in str(error.value)
    for missing in (None, '', '  '):
        with pytest.raises(ValueError, match='^' + re.escape(
                PhotoActions.IMAGE_URL_MISSING) + '$'):
            tools.makeProductPhotoChoices(ADMIN, product['_id'], missing)
    assert db.read({}, 'GeneratedPhotos') == []
    assert fakes.images.calls == []
    assert usageCounts() == []


def test_generateProductPhotos_returnsAndKeepsEachPhotosPrompt(fakes):
    store, url = themedStoreWithPhoto(fakes)

    res = tools.generateProductPhotos(ADMIN, [url], notes='on a desk')

    calls = fakes.images.byN()
    prompts = {p['_id']: p['prompt'] for p in res['photos']}
    assert sorted(prompts.values()) == sorted(
        [calls[1]['prompt'], *[calls[PHOTOS_THEMED]['prompt']] * 2])
    for prompt in prompts.values():
        assert 'Special request: on a desk' in prompt
    for doc in db.read({'storeId': store['_id']}, 'GeneratedPhotos'):
        assert doc['prompt'] == prompts[doc['_id']]
        assert doc['source'] == 'generated'

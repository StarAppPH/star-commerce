"""A product's photo (setProductPhoto: one of the store's GeneratedPhotos on
one of its products, Product.photoUrl) as the shop and the admin see it, and
the shop's brand colors (startGuestSession's ShopStoreResponse)."""

import re

import pytest

from actions.PhotoActions import PHOTO_NOT_FOUND
from actions.ProductActions import PRODUCT_NOT_FOUND
from ApiRequests import ApiRequests
from conftest import (TOKEN, addProduct, assertExactShape, createTestStore,
                      guestClaims, wipeCollections)
from mongoDb import db
from objects import (AdminProductListResponse, AdminProductRow,
                     CustomerSessionResponse, GeneratedPhoto,
                     ProductListResponse)
from utils import generateRandomString

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'Customers',
               'Products', 'GeneratedPhotos']
ADMIN = 'pp-admin'
OTHER_ADMIN = 'pp-other-admin'
GUEST = 'pp-guest-uid'

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def addPhoto(store: dict, adminId: str = ADMIN) -> dict:
    """A GeneratedPhotos document of the store, as generateProductPhotos
    saves it."""
    photoId = generateRandomString()
    return db.create(GeneratedPhoto(
        _id=photoId,
        storeId=store['_id'],
        createdBy=adminId,
        path=f"stores/{store['_id']}/photos/{photoId}.png",
        url=f'https://firebasestorage.googleapis.com/v0/b/x/o/{photoId}',
        sourcePaths=[f'users/{adminId}/mug.jpg'],
        model='muse-image-1.0',
        createdAt=db.read({'_id': store['_id']}, 'Stores',
                          findOne=True)['createdAt'],
        _version=0).model_dump(by_alias=True), 'GeneratedPhotos')


def shopper(firebase, store: dict) -> dict:
    firebase['claims'] = guestClaims(GUEST)
    return api.startGuestSession(TOKEN, store['slug'])


# ----------------------------- setProductPhoto ----------------------------- #


def test_setProductPhoto_showsTheStoresPhotoOnTheProduct(clean, firebase):
    store = createTestStore(ADMIN)
    mug = addProduct(store['_id'], 'Mug', 250)
    photo = addPhoto(store)

    res = api.setProductPhoto(ADMIN, mug['_id'], photo['_id'])

    assertExactShape(AdminProductRow, res)
    assert res['photoUrl'] == photo['url']
    stored = db.read({'_id': mug['_id']}, 'Products', findOne=True)
    assert stored['photoUrl'] == photo['url']
    assert stored['_version'] == mug['_version'] + 1
    customerId = shopper(firebase, store)['_id']
    shop = api.listStoreProducts(customerId, store['_id'])
    assertExactShape(ProductListResponse, shop)
    assert shop['products'][0]['photoUrl'] == photo['url']
    admin = api.listMyStoreProducts(ADMIN)
    assertExactShape(AdminProductListResponse, admin)
    assert admin['products'][0]['photoUrl'] == photo['url']


@pytest.mark.parametrize('photoId', [None, ''])
def test_setProductPhoto_withNoPhoto_takesItOff(clean, photoId):
    store = createTestStore(ADMIN)
    mug = addProduct(store['_id'], 'Mug', 250)
    api.setProductPhoto(ADMIN, mug['_id'], addPhoto(store)['_id'])

    res = api.setProductPhoto(ADMIN, mug['_id'], photoId)

    assert res['photoUrl'] is None
    assert db.read({'_id': mug['_id']}, 'Products',
                   findOne=True)['photoUrl'] is None


def test_aProductWithNoPhoto_sendsNone(clean, firebase):
    store = createTestStore(ADMIN)
    mug = addProduct(store['_id'], 'Mug', 250)
    old = addProduct(store['_id'], 'Old Mug', 200)
    # Stored before photos existed: no photoUrl at all.
    db.db['Products'].update_one({'_id': old['_id']},
                                 {'$unset': {'photoUrl': ''}})
    customerId = shopper(firebase, store)['_id']

    shop = api.listStoreProducts(customerId, store['_id'])
    admin = api.listMyStoreProducts(ADMIN)

    assertExactShape(ProductListResponse, shop)
    assertExactShape(AdminProductListResponse, admin)
    for rows in (shop['products'], admin['products']):
        byId = {row['_id']: row for row in rows}
        assert byId[mug['_id']]['photoUrl'] is None
        assert byId[old['_id']].get('photoUrl') is None


def test_setProductPhoto_takesOnlyTheStoresOwnPhotoAndProduct(clean):
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    mug = addProduct(store['_id'], 'Mug', 250)
    theirMug = addProduct(other['_id'], 'Mug', 250)
    photo = addPhoto(store)
    theirPhoto = addPhoto(other, OTHER_ADMIN)

    for productId, photoId, message in (
            (mug['_id'], theirPhoto['_id'], PHOTO_NOT_FOUND),
            (mug['_id'], 'no-such-photo', PHOTO_NOT_FOUND),
            (theirMug['_id'], photo['_id'], PRODUCT_NOT_FOUND),
            ('no-such-product', photo['_id'], PRODUCT_NOT_FOUND),
            ('', photo['_id'], PRODUCT_NOT_FOUND)):
        with pytest.raises(ValueError, match=exactly(message)):
            api.setProductPhoto(ADMIN, productId, photoId)

    for product in (mug, theirMug):
        assert db.read({'_id': product['_id']}, 'Products',
                       findOne=True) == product


# ----------------------------- the shop's colors ----------------------------- #


def test_theShop_getsTheBrandColors_neverThePhotoStyle(clean, firebase):
    store = createTestStore(ADMIN)
    api.updateMyStoreTheme(ADMIN, photoStyle='soft light',
                           primaryColor='#1a73e8', secondaryColor='#fbbc04')

    res = shopper(firebase, store)

    assertExactShape(CustomerSessionResponse, res)
    assert res['store']['primaryColor'] == '#1A73E8'
    assert res['store']['secondaryColor'] == '#FBBC04'
    assert 'photoStyle' not in res['store']


def test_aColorNotSet_isLeftOut(clean, firebase):
    store = createTestStore(ADMIN)
    api.updateMyStoreTheme(ADMIN, primaryColor='#112233')

    shop = shopper(firebase, store)['store']

    assert shop['primaryColor'] == '#112233'
    assert 'secondaryColor' not in shop
    assert 'photoStyle' not in shop

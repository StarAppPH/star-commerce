"""Stores (one per admin) and customers (a guest, phone-verified at
checkout), called directly. Firebase verification is the conftest
`firebase` fixture: TOKEN decodes to whatever claims a test sets."""

import datetime
import re

import pytest
from pymongo.errors import DuplicateKeyError

from actions.StoreActions import SLUG_ATTEMPTS, _isOwnerCollision, storeSlug
from ApiRequests import ApiRequests
from conftest import (STORE_DETAILS, TOKEN, assertExactShape, createTestStore,
                      createTestUser, guestAt, guestClaims,
                      wipeCollections)
from mongoDb import db
from objects import (CustomerSessionResponse, CustomerView, MyStoreResponse,
                     PhoneVerifiedResponse, Store, StoreResponse, StoreView)

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers']
ADMIN = 'sc-admin'
OTHER_ADMIN = 'sc-other-admin'
GUEST = 'sc-guest-uid'
OTHER_GUEST = 'sc-other-guest-uid'
PHONE = '+639171234567'
SLUG = 'bakery-a'
COFFEE = '☕'

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _guestSession(firebase, uid=GUEST) -> dict:
    firebase['claims'] = guestClaims(uid)
    return api.startGuestSession(TOKEN, SLUG)


def _storedPhone(customerId=GUEST):
    return db.read({'_id': customerId}, 'Customers',
                   findOne=True)['phoneNumber']


# ----------------------------- stores ----------------------------- #


def test_createUserStore_thenGetUserStore(clean):
    createTestUser(ADMIN)
    before = api.getUserStore(ADMIN)
    assertExactShape(MyStoreResponse, before)
    assert before == {'store': None}

    created = api.createUserStore(ADMIN, '  Bakery A  ', **STORE_DETAILS)

    assertExactShape(StoreResponse, created)
    assert created['name'] == 'Bakery A'
    assert created['slug'] == SLUG
    assert created['currency'] == 'PHP'
    [stored] = db.read({}, 'Stores')
    StoreView.model_validate(stored)
    assert stored['ownerUserId'] == ADMIN
    assert api.getUserStore(ADMIN) == {'store': created}


def test_createUserStore_inAnotherCurrency(clean):
    createTestUser(ADMIN)

    created = api.createUserStore(ADMIN, 'Tokyo Ramen', **STORE_DETAILS,
                              currency='JPY')

    assert created['currency'] == 'JPY'
    assert db.read({}, 'Stores')[0]['currency'] == 'JPY'


def test_createUserStore_refusesAnUnsupportedCurrency(clean):
    createTestUser(ADMIN)

    with pytest.raises(ValueError, match='^That currency is not supported$'):
        api.createUserStore(ADMIN, 'Bakery A', **STORE_DETAILS,
                            currency='XYZ')
    assert db.read({}, 'Stores') == []


def test_createUserStore_aSecondStoreIsRefused(clean):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match='^You already have a store$'):
        api.createUserStore(ADMIN, 'Bakery B', **STORE_DETAILS)
    assert len(db.read({}, 'Stores')) == 1


def test_storesIndex_refusesASecondStoreOfTheSameOwner(clean):
    """Two creates that both pass createUserStore's read: the unique
    ownerUserId index refuses the second insert."""
    createTestStore(ADMIN)
    racing = Store(ownerUserId=ADMIN,
                   name='Racing',
                   slug='racing',
                   currency='PHP',
                   **STORE_DETAILS,
                   createdAt=datetime.datetime.now(tz=datetime.timezone.utc),
                   _version=0)

    with pytest.raises(DuplicateKeyError):
        db.create(racing.model_dump(by_alias=True), 'Stores')


def test_createUserStore_aTakenLinkNameGetsASuffix(clean):
    createTestStore(OTHER_ADMIN)
    createTestUser(ADMIN)

    created = api.createUserStore(ADMIN, 'Bakery A!', **STORE_DETAILS)

    assert re.fullmatch(r'bakery-a-[0-9a-f]{4}', created['slug'])
    assert len(db.read({}, 'Stores')) == 2


@pytest.mark.parametrize('name', ['', '   ', 'x' * 61])
def test_createUserStore_refusesAnEmptyOrLongName(clean, name):
    createTestUser(ADMIN)

    with pytest.raises(ValueError,
                       match='^A store name is 1 to 60 characters$'):
        api.createUserStore(ADMIN, name, **STORE_DETAILS)
    assert db.read({}, 'Stores') == []


@pytest.mark.parametrize('name, slug', [
    ('Bakery A', 'bakery-a'),
    ("  Juan's  Café & Grill ", 'juan-s-caf-grill'),
    (COFFEE * 2, 'store'),
])
def test_storeSlug(name, slug):
    assert storeSlug(name) == slug


def test_storeSlug_leavesRoomForASuffix():
    slug = storeSlug('ab ' * 40)
    assert len(slug) <= 45
    assert not slug.endswith('-')


def test_storeRoutes_refuseACustomer(clean, firebase):
    createTestStore(ADMIN)
    _guestSession(firebase)

    with pytest.raises(PermissionError,
                       match='^Only a store admin can do this$'):
        api.getUserStore(GUEST)
    with pytest.raises(PermissionError,
                       match='^Only a store admin can do this$'):
        api.createUserStore(GUEST, 'Guest Store', **STORE_DETAILS)
    assert len(db.read({}, 'Stores')) == 1


# ----------------------------- store details ----------------------------- #

SECRET = 'SECRET-'
CONTACT_FORMAT = '^The contact number must be in international format'


def _storeWith(**changes) -> dict:
    return api.createUserStore(ADMIN, 'Bakery A',
                               **{**STORE_DETAILS, **changes})


def test_createUserStore_storesAndReturnsEveryDetail(clean):
    createTestUser(ADMIN)

    created = api.createUserStore(ADMIN,
                                  ' Bakery A ',
                                  description='  Fresh bread  ',
                                  contactNumber=' +639171234567 ',
                                  address=' 12 Rizal St ',
                                  openingHours=' Mon-Sat 7am-7pm ')

    assertExactShape(StoreResponse, created)
    assert created['description'] == 'Fresh bread'
    assert created['contactNumber'] == '+639171234567'
    assert created['address'] == '12 Rizal St'
    assert created['openingHours'] == 'Mon-Sat 7am-7pm'
    assert created['location'] is None
    [stored] = db.read({}, 'Stores')
    StoreView.model_validate(stored)
    assert (stored['description'], stored['contactNumber'], stored['address'],
            stored['openingHours'], stored['location']) == (
                'Fresh bread', '+639171234567', '12 Rizal St',
                'Mon-Sat 7am-7pm', None)
    assert api.getUserStore(ADMIN) == {'store': created}


@pytest.mark.parametrize('field, value, message', [
    ('description', '', '^A description is 1 to 300 characters$'),
    ('description', '   ', '^A description is 1 to 300 characters$'),
    ('description', SECRET + 'x' * 300,
     '^A description is 1 to 300 characters$'),
    ('address', '', '^An address is 1 to 200 characters$'),
    ('address', '   ', '^An address is 1 to 200 characters$'),
    ('address', SECRET + 'x' * 200, '^An address is 1 to 200 characters$'),
    ('openingHours', SECRET + 'x' * 100,
     '^The opening hours text is at most 100 characters$'),
    ('contactNumber', '', CONTACT_FORMAT),
    ('contactNumber', '09171234567', CONTACT_FORMAT),
    ('contactNumber', '+63 917 123 4567', CONTACT_FORMAT),
    ('contactNumber', '639171234567', CONTACT_FORMAT),
    ('contactNumber', '+123456', CONTACT_FORMAT),
    ('contactNumber', '+' + '1' * 16, CONTACT_FORMAT),
    ('contactNumber', '+0123456789', CONTACT_FORMAT),
    ('contactNumber', '+SECRET', CONTACT_FORMAT),
    ('description', 123, '^A description must be text$'),
    ('description', None, '^A description must be text$'),
    ('address', 5, '^An address must be text$'),
    ('contactNumber', 123, '^The contact number must be text$'),
    ('openingHours', 5, '^The opening hours text must be text$'),
])
def test_createUserStore_refusesAnInvalidDetail(clean, field, value, message):
    createTestUser(ADMIN)

    with pytest.raises(ValueError, match=message) as error:
        _storeWith(**{field: value})

    assert SECRET not in str(error.value)
    assert db.read({}, 'Stores') == []


@pytest.mark.parametrize('field, value', [
    ('description', 'x' * 300),
    ('address', 'x' * 200),
    ('openingHours', 'x' * 100),
    ('contactNumber', '+1234567'),
    ('contactNumber', '+' + '1' * 15),
])
def test_createUserStore_acceptsTheLimits(clean, field, value):
    createTestUser(ADMIN)

    created = _storeWith(**{field: value})

    assert created[field] == value
    assert db.read({}, 'Stores')[0][field] == value


def test_createUserStore_checksTheAdminBeforeTheDetails(clean, firebase):
    store = createTestStore(OTHER_ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(PermissionError,
                       match='^Only a store admin can do this$'):
        api.createUserStore(customerId, 'Guest Store',
                            **{**STORE_DETAILS, 'description': ''})
    assert len(db.read({}, 'Stores')) == 1


@pytest.mark.parametrize('openingHours', ['', '   ', None])
def test_createUserStore_emptyOpeningHoursAreStoredAsNone(clean, openingHours):
    createTestUser(ADMIN)

    created = _storeWith(openingHours=openingHours)

    assert created['openingHours'] is None
    assert db.read({}, 'Stores')[0]['openingHours'] is None


def test_createUserStore_aNewStoreHasNoLocation_andNoneCurrencyIsPHP(clean):
    createTestUser(ADMIN)

    created = _storeWith(currency=None)

    assert created['currency'] == 'PHP'
    assert created['location'] is None
    assert db.read({}, 'Stores')[0]['location'] is None
    assert api.getUserStore(ADMIN)['store']['location'] is None


def test_createUserStore_anOwnerIndexCollisionIsAlreadyHasAStore(
        clean, monkeypatch):
    createTestUser(ADMIN)
    calls = []

    def fake(data, collection, *args, **kwargs):
        calls.append(data)
        raise DuplicateKeyError('E11000 dup', 11000,
                                {'keyPattern': {'ownerUserId': 1}})

    monkeypatch.setattr(db, 'create', fake)

    with pytest.raises(ValueError, match='^You already have a store$'):
        _storeWith()
    assert len(calls) == 1


def test_createUserStore_aSlugCollisionRetries_thenGivesUp(clean, monkeypatch):
    createTestUser(ADMIN)
    slugs = []

    def fake(data, collection, *args, **kwargs):
        slugs.append(data['slug'])
        raise DuplicateKeyError('E11000 dup', 11000,
                                {'keyPattern': {'slug': 1}})

    monkeypatch.setattr(db, 'create', fake)

    with pytest.raises(ValueError,
                       match='^Could not pick a link name for this store, '
                       'try another name$'):
        _storeWith()
    assert len(slugs) == SLUG_ATTEMPTS
    assert slugs[0] == 'bakery-a'
    assert all(re.fullmatch(r'bakery-a-[0-9a-f]{4}', s) for s in slugs[1:])


OWNER_MESSAGE = ('E11000 duplicate key error collection: db.Stores index: '
                 'ownerUserId_1 dup key: { ownerUserId: "a" }')
SLUG_MESSAGE = ('E11000 duplicate key error collection: db.Stores index: '
                'slug_1 dup key: { slug: "bakery-a" }')


@pytest.mark.parametrize('message, details, isOwner', [
    ('E11000', {'keyPattern': {'ownerUserId': 1}}, True),
    ('E11000', {'keyValue': {'ownerUserId': 'a'}}, True),
    (OWNER_MESSAGE, None, True),
    (OWNER_MESSAGE, {}, True),
    ('E11000', {'keyPattern': {}, 'keyValue': {'ownerUserId': 'a'}}, True),
    (OWNER_MESSAGE, {'keyPattern': {}, 'keyValue': {}}, True),
    ('E11000', {'keyPattern': {'slug': 1}}, False),
    ('E11000', {'keyValue': {'slug': 'bakery-a'}}, False),
    (SLUG_MESSAGE, None, False),
    (SLUG_MESSAGE, {}, False),
    ('E11000 dup key', {}, False),
    (SLUG_MESSAGE, {'keyPattern': {}, 'keyValue': {'slug': 'bakery-a'}},
     False),
    # The named keys beat the message.
    (OWNER_MESSAGE, {'keyPattern': {'slug': 1}}, False),
])
def test_isOwnerCollision(message, details, isOwner):
    error = DuplicateKeyError(message, 11000, details)

    assert _isOwnerCollision(error) is isOwner


# ----------------------------- store location ----------------------------- #


def test_setUserStoreLocation_savesTheLocation(clean):
    created = createTestStore(ADMIN)

    res = api.setUserStoreLocation(ADMIN, 14.5995, 120.9842)

    assertExactShape(StoreResponse, res)
    assert res['location'] == {'latitude': 14.5995, 'longitude': 120.9842}
    assert {k: v for k, v in res.items() if k != 'location'} == {
        k: v for k, v in created.items() if k != 'location'}
    [stored] = db.read({}, 'Stores')
    StoreView.model_validate(stored)
    assert stored['location'] == res['location']
    assert stored['_version'] == 1
    assert api.getUserStore(ADMIN) == {'store': res}

    again = api.setUserStoreLocation(ADMIN, 10, 20)

    assert again['location'] == {'latitude': 10.0, 'longitude': 20.0}
    assert db.read({}, 'Stores')[0]['_version'] == 2


@pytest.mark.parametrize('latitude, longitude', [(90, 180), (-90, -180),
                                                 (0, 0), (10, 20)])
def test_setUserStoreLocation_acceptsTheLimits(clean, latitude, longitude):
    createTestStore(ADMIN)

    api.setUserStoreLocation(ADMIN, latitude, longitude)

    location = db.read({}, 'Stores')[0]['location']
    assert location == {'latitude': latitude, 'longitude': longitude}
    assert isinstance(location['latitude'], float)
    assert isinstance(location['longitude'], float)


def test_setUserStoreLocation_refusesABadValue(clean):
    createTestStore(ADMIN)
    latitudeMessage = '^The latitude is a number from -90 to 90$'
    longitudeMessage = '^The longitude is a number from -180 to 180$'

    for value in [90.1, -90.1, True, False, float('nan'), float('inf'),
                  float('-inf'), 10**400, '14.6', None]:
        with pytest.raises(ValueError, match=latitudeMessage):
            api.setUserStoreLocation(ADMIN, value, 10)
        assert db.read({}, 'Stores')[0]['location'] is None
    for value in [180.1, -180.1, True, float('nan'), float('inf'), 10**400,
                  '1', None]:
        with pytest.raises(ValueError, match=longitudeMessage):
            api.setUserStoreLocation(ADMIN, 10, value)
        assert db.read({}, 'Stores')[0]['location'] is None
    assert db.read({}, 'Stores')[0]['_version'] == 0


def test_setUserStoreLocation_refusesACustomer(clean, firebase):
    store = createTestStore(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(PermissionError,
                       match='^Only a store admin can do this$'):
        api.setUserStoreLocation(customerId, 10, 20)
    assert db.read({}, 'Stores')[0]['location'] is None


def test_setUserStoreLocation_refusesAnAdminWithoutAStore(clean):
    createTestUser(ADMIN)

    with pytest.raises(ValueError, match='^Create your store first$'):
        api.setUserStoreLocation(ADMIN, 10, 20)
    assert db.read({}, 'Stores') == []


def test_setUserStoreLocation_onlyTouchesTheCallersStore(clean):
    createTestStore(ADMIN)
    createTestStore(OTHER_ADMIN, 'Bakery B')

    api.setUserStoreLocation(ADMIN, 10, 20)

    other = db.read({'ownerUserId': OTHER_ADMIN}, 'Stores', findOne=True)
    assert other['location'] is None
    assert other['_version'] == 0


def test_startGuestSession_sendsTheStoresPublicDetails(clean, firebase):
    """A customer, an anonymous visitor included, sees the store's contact
    number, address and map location."""
    createTestStore(ADMIN)
    pinned = api.setUserStoreLocation(ADMIN, 14.5, 121.0)

    store = _guestSession(firebase)['store']

    assert store['contactNumber'] == STORE_DETAILS['contactNumber']
    assert store['address'] == STORE_DETAILS['address']
    assert store['location'] == {'latitude': 14.5, 'longitude': 121.0}
    assert store == pinned


# ----------------------------- guest sessions ----------------------------- #


def test_startGuestSession_createsTheCustomerOnce(clean, firebase):
    store = createTestStore(ADMIN)

    res = _guestSession(firebase)

    assertExactShape(CustomerSessionResponse, res)
    assert res == {'_id': GUEST, 'phoneNumber': None, 'store': store}
    [stored] = db.read({}, 'Customers')
    CustomerView.model_validate(stored)
    assert stored['phoneNumber'] is None

    assert _guestSession(firebase) == res
    assert len(db.read({}, 'Customers')) == 1


def test_startGuestSession_aPhoneSignInsFirstVisitKeepsItsNumber(
        clean, firebase):
    createTestStore(ADMIN)
    firebase['claims'] = guestClaims(GUEST, provider='phone', phoneNumber=PHONE)

    res = api.startGuestSession(TOKEN, SLUG)

    assert res['phoneNumber'] == PHONE
    assert _storedPhone() == PHONE


def test_startGuestSession_refusesAGoogleSignIn(clean, firebase):
    """An admin's Google token never becomes a customer."""
    createTestStore(ADMIN)

    with pytest.raises(PermissionError, match='^Open the store as a guest'):
        api.startGuestSession(TOKEN, SLUG)
    assert db.read({}, 'Customers') == []


def test_startGuestSession_refusesAnUnknownStore(clean, firebase):
    createTestStore(ADMIN)
    firebase['claims'] = guestClaims(GUEST)

    with pytest.raises(ValueError, match='^Store not found$'):
        api.startGuestSession(TOKEN, 'no-such-store')
    assert db.read({}, 'Customers') == []


@pytest.mark.parametrize('idToken', [None, '', 123, 'x' * 5000])
def test_startGuestSession_refusesAMissingOrOddToken(clean, firebase,
                                                     idToken):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match='^Invalid sign-in request$'):
        api.startGuestSession(idToken, SLUG)
    assert firebase['verified'] == []


def test_startGuestSession_refusesATokenThatDoesNotVerify(clean, firebase):
    createTestStore(ADMIN)

    with pytest.raises(PermissionError,
                       match='^Sign-in could not be verified'):
        api.startGuestSession('sc-forged-token', SLUG)
    assert db.read({}, 'Customers') == []


# ----------------------------- phone at checkout ----------------------------- #


def test_verifyCustomerPhone_storesTheVerifiedNumber(clean, firebase):
    createTestStore(ADMIN)
    _guestSession(firebase)
    firebase['claims'] = guestClaims(GUEST, provider='phone', phoneNumber=PHONE)

    res = api.verifyCustomerPhone(GUEST, TOKEN)

    assertExactShape(PhoneVerifiedResponse, res)
    assert res == {'phoneNumber': PHONE}
    stored = db.read({'_id': GUEST}, 'Customers', findOne=True)
    assert stored['phoneNumber'] == PHONE
    assert stored['_version'] == 1


def test_verifyCustomerPhone_refusesAnotherVisitorsToken(clean, firebase):
    createTestStore(ADMIN)
    _guestSession(firebase)
    firebase['claims'] = guestClaims(uid=OTHER_GUEST,
                                      provider='phone',
                                      phoneNumber=PHONE)

    with pytest.raises(PermissionError, match='for another visitor$'):
        api.verifyCustomerPhone(GUEST, TOKEN)
    assert _storedPhone() is None


@pytest.mark.parametrize('phoneNumber', [None, '09171234567', 'not a number'])
def test_verifyCustomerPhone_refusesATokenWithoutAVerifiedNumber(
        clean, firebase, phoneNumber):
    createTestStore(ADMIN)
    _guestSession(firebase)
    firebase['claims'] = guestClaims(GUEST, phoneNumber=phoneNumber)

    with pytest.raises(PermissionError, match='^Verify your phone number'):
        api.verifyCustomerPhone(GUEST, TOKEN)
    assert _storedPhone() is None


def test_verifyCustomerPhone_refusesAnAdmin(clean, firebase):
    createTestUser(ADMIN)
    firebase['claims'] = guestClaims(uid=ADMIN,
                                      provider='phone',
                                      phoneNumber=PHONE)

    with pytest.raises(PermissionError,
                       match='^Only a customer can do this$'):
        api.verifyCustomerPhone(ADMIN, TOKEN)
    assert db.read({}, 'Customers') == []

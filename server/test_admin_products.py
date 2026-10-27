"""The admin product routes, called directly: addStoreProduct,
addStoreProducts, updateStoreProduct and listMyStoreProducts.

_validProduct is the one validator of a product's fields, so its full
message matrix is tested once, directly (no database). The routes then
keep one bad value per distinct message, the batch "Product N:" format,
their check order, the tenant boundary and what they store.
"""

import math
import re

import pytest
from pymongo.errors import BulkWriteError, DuplicateKeyError

import actions.ProductActions as productActions
from actions.ProductActions import NAME_TAKEN, PRODUCT_NOT_FOUND, _validProduct
from actions.StoreActions import ownerStore
from ApiRequests import ApiRequests
from conftest import (addProduct, assertExactShape, assertRowKeys,
                      createTestStore, createTestUser, guestAt,
                      storeWithProducts, wipeCollections)
from mongoDb import OutdatedDataError, db
from objects import (AdminProductListResponse, AdminProductListRow,
                     AdminProductRow, NewProduct, ProductView)

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products']
ADMIN = 'ap-admin'
OTHER_ADMIN = 'ap-other-admin'
GUEST = 'ap-guest-uid'
SECRET = 'SECRET-'

NAME_LENGTH = 'The name is 1 to 80 characters'
NAME_TEXT = 'The name must be text'
NAME_CONTROL = 'The name cannot contain control characters'
DESCRIPTION_LENGTH = 'The description is at most 500 characters'
DESCRIPTION_TEXT = 'The description must be text'
DESCRIPTION_CONTROL = 'The description cannot contain control characters'
PRICE = 'The price is a number from 0 to 10000000'
INVENTORY = 'The inventory is a whole number from 0 to 1000000'
AVAILABLE = 'isAvailable is true or false'
BATCH_SIZE = 'Send 1 to 50 products at a time'
NO_CHANGE = 'Send at least one field to change'
NO_STORE = 'Create your store first'
NOT_ADMIN = 'Only a store admin can do this'
OUTDATED = 'Your data is outdated. Please refresh the page and try again.'
LIMIT = 'limit is a whole number from 1 to 50'

api = ApiRequests()


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _add(userId: str = ADMIN, **fields) -> dict:
    return api.addStoreProduct(
        userId, **{'name': 'Pandesal', 'price': 5, 'inventory': 100, **fields})


def _item(name: str, **fields) -> dict:
    return {'name': name, 'price': 1, 'inventory': 1, **fields}


def _stored(storeId: str | None = None) -> list:
    query = {} if storeId is None else {'storeId': storeId}
    return db.read(query, 'Products')


def _byName(name: str) -> dict:
    return db.read({'name': name}, 'Products', findOne=True)


# ----------------------------- _validProduct (pure) ----------------------------- #

BASE = {'name': 'A', 'description': None, 'price': 1, 'inventory': 1,
        'isAvailable': True}


@pytest.mark.parametrize('field, value, message', [
    ('name', '', NAME_LENGTH),
    ('name', '   ', NAME_LENGTH),
    ('name', None, NAME_LENGTH),
    ('name', SECRET + 'x' * 81, NAME_LENGTH),
    ('name', 5, NAME_TEXT),
    ('name', ['a'], NAME_TEXT),
    ('name', 'a\x00b', NAME_CONTROL),
    ('name', '\x07', NAME_CONTROL),
    ('name', 'a\x1bb', NAME_CONTROL),
    ('name', 'a\x7fb', NAME_CONTROL),
    ('name', 'a\x85b', NAME_CONTROL),
    ('description', SECRET + 'x' * 501, DESCRIPTION_LENGTH),
    ('description', 5, DESCRIPTION_TEXT),
    ('description', 'a\x00', DESCRIPTION_CONTROL),
    ('description', 'a\x7f', DESCRIPTION_CONTROL),
    ('price', -1, PRICE),
    ('price', -0.01, PRICE),
    ('price', 10000000.01, PRICE),
    ('price', 10**7 + 1, PRICE),
    ('price', True, PRICE),
    ('price', False, PRICE),
    ('price', '5', PRICE),
    ('price', None, PRICE),
    ('price', float('nan'), PRICE),
    ('price', float('inf'), PRICE),
    ('price', float('-inf'), PRICE),
    ('price', 10**400, PRICE),
    ('inventory', -1, INVENTORY),
    ('inventory', 1000001, INVENTORY),
    ('inventory', 1.5, INVENTORY),
    ('inventory', 2.0, INVENTORY),
    ('inventory', True, INVENTORY),
    ('inventory', '3', INVENTORY),
    ('inventory', None, INVENTORY),
    ('inventory', 10**400, INVENTORY),
    ('isAvailable', 'yes', AVAILABLE),
    ('isAvailable', 1, AVAILABLE),
    ('isAvailable', 0, AVAILABLE),
])
def test_validProduct_refusesAnInvalidField(field, value, message):
    with pytest.raises(ValueError, match=exactly(message)) as error:
        _validProduct(**{**BASE, field: value}, isNew=True)

    assert SECRET not in str(error.value)


def test_validProduct_aNewProduct_defaultsAndCoercions():
    assert _validProduct('  A  ', '', 7, 2, None, isNew=True) == {
        'name': 'A', 'description': None, 'price': 7.0, 'inventory': 2,
        'isAvailable': True}
    assert _validProduct('A', '   ', 1, 0, False, isNew=True)[
        'isAvailable'] is False
    price = _validProduct('A', None, -0.0, 0, True, isNew=True)['price']
    assert price == 0 and math.copysign(1, price) == 1.0


@pytest.mark.parametrize('field, value', [
    ('name', 'x' * 80),
    ('name', 'a\tb'),
    ('name', 'Family \U0001F468‍\U0001F469‍\U0001F467'),
    ('description', 'x' * 500),
    ('description', 'line one\nline two\r\nline three\tend'),
    ('price', 0),
    ('price', 0.01),
    ('price', 10000000),
    ('inventory', 0),
    ('inventory', 1000000),
])
def test_validProduct_acceptsTheLimits(field, value):
    fields = _validProduct(**{**BASE, field: value}, isNew=True)

    assert fields[field] == (value.strip() if isinstance(value, str)
                             else value)


def test_validProduct_anUpdate_returnsOnlyTheChanges():
    assert _validProduct(isNew=False) == {}
    assert _validProduct('  ', '', None, None, None, isNew=False) == {}
    assert _validProduct(price=0, isNew=False) == {'price': 0.0}
    assert _validProduct(inventory=0, isNew=False) == {'inventory': 0}
    assert _validProduct(isAvailable=False, isNew=False) == {
        'isAvailable': False}
    assert _validProduct(' Roll ', ' Soft ', isNew=False) == {
        'name': 'Roll', 'description': 'Soft'}


@pytest.mark.parametrize('field, value, message', [
    ('name', 5, NAME_TEXT),
    ('name', '\x00', NAME_CONTROL),
    ('name', 'x' * 81, NAME_LENGTH),
    ('description', '\x00', DESCRIPTION_CONTROL),
    ('price', None, None),
])
def test_validProduct_anUpdate_stillChecksWhatItSends(field, value, message):
    if message is None:  # None is "unchanged" for every field
        assert _validProduct(**{field: value}, isNew=False) == {}
        return
    with pytest.raises(ValueError, match=exactly(message)):
        _validProduct(**{field: value}, isNew=False)


# ----------------------------- shared refusals ----------------------------- #

INVALID_CALLS = {
    'addStoreProduct': lambda uid: api.addStoreProduct(uid, '', -1, 1.5),
    'addStoreProducts': lambda uid: api.addStoreProducts(uid, [5] * 60),
    'updateStoreProduct': lambda uid: api.updateStoreProduct(
        uid, 'no-such-product', price=-1),
    'listMyStoreProducts': lambda uid: api.listMyStoreProducts(
        uid, search=5, limit=0),
}


@pytest.mark.parametrize('route', sorted(INVALID_CALLS))
def test_adminProductRoutes_refuseACustomer(clean, firebase, route):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)
    before = _stored()

    with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
        INVALID_CALLS[route](customerId)
    assert _stored() == before


@pytest.mark.parametrize('route', sorted(INVALID_CALLS))
def test_adminProductRoutes_refuseAnAdminWithoutAStore(clean, route):
    createTestUser(ADMIN)

    with pytest.raises(ValueError, match=exactly(NO_STORE)):
        INVALID_CALLS[route](ADMIN)
    assert _stored() == []


# ----------------------------- addStoreProduct ----------------------------- #


def test_addStoreProduct_storesAndReturnsTheProduct(clean):
    store = createTestStore(ADMIN, currency='JPY')

    created = _add(name=' Pan de Sal ', description=' Soft ')

    assertExactShape(AdminProductRow, created)
    assert created['name'] == 'Pan de Sal'
    assert created['description'] == 'Soft'
    assert created['price'] == 5.0 and isinstance(created['price'], float)
    assert created['inventory'] == 100
    assert created['isAvailable'] is True
    assert created['soldCount'] == 0
    assert created['reservedInventory'] == []
    [stored] = _stored()
    ProductView.model_validate(stored)
    assert stored['_id'] == created['_id']
    assert stored['storeId'] == store['_id']
    assert stored['nameKey'] == 'pan de sal'
    assert stored['_version'] == 0


def test_addStoreProduct_defaultsAndCoercions(clean):
    createTestStore(ADMIN)

    hidden = _add(name='Hidden', isAvailable=False)
    noDescription = _add(name='Blank', description='   ', isAvailable=None)
    free = _add(name='Free', price=-0.0)

    assert hidden['isAvailable'] is False
    assert noDescription['description'] is None
    assert noDescription['isAvailable'] is True
    assert free['price'] == 0 and math.copysign(1, free['price']) == 1.0
    assert math.copysign(1, _byName('Free')['price']) == 1.0


@pytest.mark.parametrize('field, value, message', [
    ('name', '', NAME_LENGTH),
    ('name', 5, NAME_TEXT),
    ('name', 'a\x00b', NAME_CONTROL),
    ('description', SECRET + 'x' * 501, DESCRIPTION_LENGTH),
    ('price', -1, PRICE),
    ('inventory', 1.5, INVENTORY),
    ('isAvailable', 'yes', AVAILABLE),
])
def test_addStoreProduct_refusesAnInvalidField(clean, field, value, message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        _add(**{field: value})

    assert SECRET not in str(error.value)
    assert _stored() == []


@pytest.mark.parametrize('name', [
    'Pandesal', 'PANDESAL', ' pandesal ', 'Pan  desal'
])
def test_addStoreProduct_aTakenNameIsRefused(clean, name):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pan desal' if name == 'Pan  desal' else 'Pandesal',
               5, isAvailable=False)  # a hidden product holds its name too

    with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
        _add(name=name)
    assert len(_stored()) == 1


def test_addStoreProduct_theSameNameInAnotherStoreIsFine(clean):
    createTestStore(ADMIN)
    createTestStore(OTHER_ADMIN, 'Coffee B')

    _add(ADMIN)
    _add(OTHER_ADMIN)

    assert len(_stored()) == 2


def test_addStoreProduct_theCap(clean, monkeypatch):
    monkeypatch.setattr(productActions, 'PRODUCTS_PER_STORE_MAX', 3)
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    for number in range(3):  # another store's products do not count
        addProduct(other['_id'], f'Latte {number}', 1)
    addProduct(store['_id'], 'One', 1)
    addProduct(store['_id'], 'Two', 1, isAvailable=False)

    _add(name='Three')

    with pytest.raises(ValueError, match=exactly(
            'A store has at most 3 products')):
        _add(name='Four')
    assert len(_stored(store['_id'])) == 3


def test_addStoreProduct_checkOrder(clean, monkeypatch):
    monkeypatch.setattr(productActions, 'PRODUCTS_PER_STORE_MAX', 1)
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pandesal', 1)

    with pytest.raises(ValueError, match=exactly(NAME_LENGTH)):
        _add(name='', price=-1)
    with pytest.raises(ValueError, match=exactly(PRICE)):
        _add(price=-1, inventory=1.5)
    with pytest.raises(ValueError, match=exactly(INVENTORY)):
        _add(name='New', inventory=-1)  # a bad field beats the full store
    with pytest.raises(ValueError, match=exactly(
            'A store has at most 1 products')):
        _add(name='Pandesal')  # the cap beats the taken name


@pytest.mark.parametrize('route', ['add', 'update'])
def test_aRacingDuplicateName_isNameTaken(clean, monkeypatch, route):
    store = createTestStore(ADMIN)
    existing = addProduct(store['_id'], 'Existing', 1)

    def raiseDuplicate(*args, **kwargs):
        raise DuplicateKeyError('E11000', 11000,
                                {'keyPattern': {'nameKey': 1}})

    if route == 'add':
        monkeypatch.setattr(db, 'create', raiseDuplicate)
        call = lambda: _add(name='New')  # noqa: E731
    else:
        monkeypatch.setattr(db, 'update', raiseDuplicate)
        call = lambda: api.updateStoreProduct(  # noqa: E731
            ADMIN, existing['_id'], name='New')

    with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
        call()


def test_productsIndex_refusesTheSameNameKeyInOneStore(clean):
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    addProduct(store['_id'], 'Pandesal', 1)

    addProduct(other['_id'], 'Pandesal', 1)  # another store: fine
    with pytest.raises(DuplicateKeyError):
        addProduct(store['_id'], 'PANDESAL', 1)


# ----------------------------- addStoreProducts ----------------------------- #


def test_addStoreProducts_addsAllInOrder(clean):
    createTestStore(ADMIN, currency='JPY')

    res = api.addStoreProducts(ADMIN, [
        _item('Tea', price=3, inventory=20, description='  Hot  ',
              extra='ignored'),
        NewProduct(name='Coffee', price=4.5, inventory=0, isAvailable=False),
        {'name': 'Cake', 'price': 9, 'inventory': 2},
    ])

    assertExactShape(AdminProductListResponse, res)
    assertRowKeys(res['products'], AdminProductListRow)
    assert res['currency'] == 'JPY'
    assert [p['name'] for p in res['products']] == ['Tea', 'Coffee', 'Cake']
    assert [p['reservedQuantity'] for p in res['products']] == [0, 0, 0]
    assert [p['isAvailable'] for p in res['products']] == [True, False, True]
    assert [p['description'] for p in res['products']] == ['Hot', None, None]
    assert [p['soldCount'] for p in res['products']] == [0, 0, 0]
    stored = _stored()
    assert len(stored) == 3
    for doc in stored:
        ProductView.model_validate(doc)
    assert len({doc['createdAt'] for doc in stored}) == 1
    assert {doc['_id'] for doc in stored} == {p['_id'] for p in res['products']}


@pytest.mark.parametrize('count, accepted', [(1, True), (50, True),
                                             (0, False), (51, False)])
def test_addStoreProducts_sizeLimits(clean, count, accepted):
    createTestStore(ADMIN)
    products = [_item(f'Item {number}') for number in range(count)]

    if accepted:
        assert len(api.addStoreProducts(ADMIN, products)['products']) == count
    else:
        with pytest.raises(ValueError, match=exactly(BATCH_SIZE)):
            api.addStoreProducts(ADMIN, products)
        assert _stored() == []


@pytest.mark.parametrize('products', ['x', {}, None, 5])
def test_addStoreProducts_refusesANonList(clean, products):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(BATCH_SIZE)):
        api.addStoreProducts(ADMIN, products)


@pytest.mark.parametrize('items, message', [
    ([_item('A'), 5], 'Product 2: send it as an object'),
    (['x'], 'Product 1: send it as an object'),
    ([None], 'Product 1: send it as an object'),
    ([_item('A'), _item('B'), []], 'Product 3: send it as an object'),
    ([{'price': 1, 'inventory': 1}], 'Product 1: the name is 1 to 80 characters'),
    ([NewProduct(name='x' * 81, price=1, inventory=1)],
     'Product 1: the name is 1 to 80 characters'),
    ([_item('A'), {**_item('B'), 'name': 5}], 'Product 2: the name must be text'),
    ([_item('A\x00')], 'Product 1: the name cannot contain control characters'),
    ([_item('A'), _item('B'), _item('C', price=True)],
     'Product 3: the price is a number from 0 to 10000000'),
    ([{'name': 'A', 'inventory': 1}],
     'Product 1: the price is a number from 0 to 10000000'),
    ([{'name': 'A', 'price': 1}],
     'Product 1: the inventory is a whole number from 0 to 1000000'),
    ([_item('A'), _item('B', description='x' * 501)],
     'Product 2: the description is at most 500 characters'),
    ([_item('A', isAvailable='yes')], 'Product 1: isAvailable is true or false'),
    ([_item('A', price=-1), _item('', inventory=-1)],
     'Product 1: the price is a number from 0 to 10000000'),
])
def test_addStoreProducts_refusesAnItemWithItsPosition(clean, items, message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)):
        api.addStoreProducts(ADMIN, items)
    assert _stored() == []  # valid items before the bad one are not added


def test_addStoreProducts_neverEchoesAValue(clean):
    createTestStore(ADMIN)

    with pytest.raises(ValueError) as error:
        api.addStoreProducts(ADMIN, [_item(SECRET + 'x' * 81)])

    assert SECRET not in str(error.value)


@pytest.mark.parametrize('items, message', [
    ([_item('Tea'), _item('TEA')],
     'Product 2: another product in this list has the same name'),
    ([_item('Tea'), _item('Cake'), _item(' tea ')],
     'Product 3: another product in this list has the same name'),
    ([_item('Green Tea'), _item('green   tea')],
     'Product 2: another product in this list has the same name'),
])
def test_addStoreProducts_refusesDuplicatesInTheBatch(clean, items, message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)):
        api.addStoreProducts(ADMIN, items)
    assert _stored() == []


def test_addStoreProducts_refusesANameTheStoreHas(clean):
    store = createTestStore(ADMIN)
    createTestStore(OTHER_ADMIN, 'Coffee B')
    addProduct(store['_id'], 'Hidden Tea', 1, isAvailable=False)

    with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
        api.addStoreProducts(ADMIN, [_item('Fresh'), _item('HIDDEN tea')])
    assert len(_stored()) == 1
    # The same names in another store are fine.
    api.addStoreProducts(OTHER_ADMIN, [_item('Hidden Tea')])


def test_addStoreProducts_checkOrder(clean, monkeypatch):
    monkeypatch.setattr(productActions, 'PRODUCTS_PER_STORE_MAX', 2)
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Existing', 1)

    # The size comes before the items' contents.
    with pytest.raises(ValueError, match=exactly(BATCH_SIZE)):
        api.addStoreProducts(ADMIN, [5] * 51)
    # The first bad item is the one reported.
    with pytest.raises(ValueError, match=exactly(
            'Product 1: the price is a number from 0 to 10000000')):
        api.addStoreProducts(ADMIN, [_item('A', price=-1), _item('')])
    # A field refusal beats a duplicate found earlier in the list.
    with pytest.raises(ValueError, match=exactly(
            'Product 3: the inventory is a whole number from 0 to 1000000')):
        api.addStoreProducts(ADMIN, [_item('A'), _item('a'),
                                     _item('C', inventory=-1)])
    # A duplicate in the list beats the cap; the cap beats a taken name.
    with pytest.raises(ValueError, match=exactly(
            'Product 2: another product in this list has the same name')):
        api.addStoreProducts(ADMIN, [_item('A'), _item('a'), _item('B')])
    with pytest.raises(ValueError, match=exactly(
            'A store has at most 2 products')):
        api.addStoreProducts(ADMIN, [_item('Existing'), _item('B')])
    assert len(_stored()) == 1


def test_addStoreProducts_theCap(clean, monkeypatch):
    monkeypatch.setattr(productActions, 'PRODUCTS_PER_STORE_MAX', 3)
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'One', 1)
    addProduct(store['_id'], 'Two', 1, isAvailable=False)

    with pytest.raises(ValueError, match=exactly(
            'A store has at most 3 products')):
        api.addStoreProducts(ADMIN, [_item('A'), _item('B')])
    assert len(_stored()) == 2
    api.addStoreProducts(ADMIN, [_item('A')])
    assert len(_stored()) == 3


@pytest.mark.parametrize('code, mapped', [(11000, True), (1, False)])
def test_addStoreProducts_aRacingDuplicate(clean, monkeypatch, code, mapped):
    createTestStore(ADMIN)

    def raiseBulk(updateFunction):
        raise BulkWriteError({'writeErrors': [{'code': code}]})

    monkeypatch.setattr(db, 'createTransaction', raiseBulk)

    if mapped:
        with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
            api.addStoreProducts(ADMIN, [_item('A')])
    else:
        with pytest.raises(BulkWriteError):
            api.addStoreProducts(ADMIN, [_item('A')])
    assert _stored() == []


@pytest.mark.skipif(
    not db.replicaSetEnabled,
    reason='needs a replica set: on a standalone mongod, createTransaction '
    'opens no transaction, so a duplicate mid-batch cannot roll back the '
    'other inserts. This test never runs locally.')
def test_addStoreProducts_aRacingDuplicateRollsBackTheWholeBatch(
        clean, monkeypatch):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Existing', 1)
    # Skip the pre-check, as a request that raced past it would.
    monkeypatch.setattr(productActions, '_refuseTakenNames',
                        lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
        api.addStoreProducts(ADMIN, [_item('Fresh'), _item('Existing')])

    assert [d['name'] for d in _stored()] == ['Existing']


# ----------------------------- updateStoreProduct ----------------------------- #

RESERVED = [{'orderId': 'o1', 'quantity': 2}, {'orderId': 'o2', 'quantity': 3}]


def test_updateStoreProduct_changesOnlyWhatIsSent_andKeepsTheReservations(
        clean):
    store = createTestStore(ADMIN)
    product = addProduct(store['_id'], 'Pandesal', 5, description='Soft',
                         inventory=10, reservedInventory=RESERVED,
                         soldCount=7)
    productId = product['_id']

    def update(**fields) -> dict:
        res = api.updateStoreProduct(ADMIN, productId, **fields)
        assertExactShape(AdminProductRow, res)
        assert res['reservedInventory'] == RESERVED
        assert res['soldCount'] == 7
        assert _byName(res['name'])['reservedInventory'] == RESERVED
        return res

    res = update(price=7)
    assert (res['name'], res['description'], res['price'],
            res['inventory'], res['isAvailable']) == ('Pandesal', 'Soft', 7.0,
                                                     10, True)
    assert _byName('Pandesal')['_version'] == 1

    # A blank name or description, or None, means unchanged.
    res = update(name='  ', description='', inventory=None, price=8)
    assert (res['name'], res['description'], res['inventory'],
            res['price']) == ('Pandesal', 'Soft', 10, 8.0)

    # A falsy value is a change.
    assert update(price=0)['price'] == 0.0
    assert update(inventory=0)['inventory'] == 0
    assert update(isAvailable=False)['isAvailable'] is False
    price = update(price=-0.0)['price']
    assert math.copysign(1, price) == 1.0

    res = update(name=' Roll ', description=' New ')
    assert (res['name'], res['description']) == ('Roll', 'New')
    stored = _byName('Roll')
    assert stored['nameKey'] == 'roll'
    ProductView.model_validate(stored)


@pytest.mark.parametrize('fields', [
    {},
    {'name': None, 'description': None, 'price': None, 'inventory': None,
     'isAvailable': None},
    {'name': '  '},
    {'description': ''},
    {'name': '', 'description': '   '},
])
def test_updateStoreProduct_needsAChange(clean, fields):
    store = createTestStore(ADMIN)
    product = addProduct(store['_id'], 'Pandesal', 5)

    with pytest.raises(ValueError, match=exactly(NO_CHANGE)):
        api.updateStoreProduct(ADMIN, product['_id'], **fields)
    assert _byName('Pandesal')['_version'] == 0


@pytest.mark.parametrize('fields, message', [
    ({'name': 5}, NAME_TEXT),
    ({'name': '\x00'}, NAME_CONTROL),
    ({'description': '\x00'}, DESCRIPTION_CONTROL),
    ({'price': -1}, PRICE),
    ({'inventory': True}, INVENTORY),
    ({'isAvailable': 'yes'}, AVAILABLE),
])
def test_updateStoreProduct_refusesAnInvalidField(clean, fields, message):
    store = createTestStore(ADMIN)
    product = addProduct(store['_id'], 'Pandesal', 5)

    with pytest.raises(ValueError, match=exactly(message)):
        api.updateStoreProduct(ADMIN, product['_id'], **fields)
    assert _byName('Pandesal')['_version'] == 0


@pytest.mark.parametrize('productId', ['no-such-product', '', None, 5])
def test_updateStoreProduct_productNotFound(clean, productId):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pandesal', 5)

    with pytest.raises(ValueError, match=exactly(PRODUCT_NOT_FOUND)):
        api.updateStoreProduct(ADMIN, productId, price=1)


def test_updateStoreProduct_tenantBoundary(clean):
    store = createTestStore(ADMIN)
    createTestStore(OTHER_ADMIN, 'Coffee B')
    product = addProduct(store['_id'], 'Pandesal', 5)

    with pytest.raises(ValueError, match=exactly(PRODUCT_NOT_FOUND)):
        api.updateStoreProduct(OTHER_ADMIN, product['_id'], price=1,
                               name='Hijacked')

    stored = _byName('Pandesal')
    assert (stored['price'], stored['_version'], stored['storeId']) == (
        5.0, 0, store['_id'])
    assert _byName('Hijacked') is None


def test_updateStoreProduct_nameRules(clean):
    store = createTestStore(ADMIN)
    createTestStore(OTHER_ADMIN, 'Coffee B')
    pandesal = addProduct(store['_id'], 'Pandesal', 5)
    addProduct(store['_id'], 'Ube Cake', 6, isAvailable=False)
    addProduct(_byStoreOf(OTHER_ADMIN), 'Roll', 1)

    for taken in ('Ube Cake', 'UBE CAKE', ' ube  cake '):
        with pytest.raises(ValueError, match=exactly(NAME_TAKEN)):
            api.updateStoreProduct(ADMIN, pandesal['_id'], name=taken)

    # A case-only change of its own name, and its exact name, are fine.
    res = api.updateStoreProduct(ADMIN, pandesal['_id'], name='PANDESAL')
    assert res['name'] == 'PANDESAL'
    assert db.read({'_id': pandesal['_id']}, 'Products',
                   findOne=True)['nameKey'] == 'pandesal'
    api.updateStoreProduct(ADMIN, pandesal['_id'], name='PANDESAL')
    # The same name as a product of another store is fine.
    api.updateStoreProduct(ADMIN, pandesal['_id'], name='Roll')
    # A rename frees the old name.
    _add(name='Pandesal')


def _byStoreOf(adminId: str) -> str:
    return db.read({'ownerUserId': adminId}, 'Stores', findOne=True)['_id']


def test_updateStoreProduct_checkOrder(clean):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pandesal', 5)

    with pytest.raises(ValueError, match=exactly(NO_CHANGE)):
        api.updateStoreProduct(ADMIN, 'no-such-product')
    with pytest.raises(ValueError, match=exactly(PRICE)):
        api.updateStoreProduct(ADMIN, 'no-such-product', price=-1)
    with pytest.raises(ValueError, match=exactly(PRODUCT_NOT_FOUND)):
        api.updateStoreProduct(ADMIN, 'no-such-product', name='Pandesal')


def test_updateStoreProduct_anOutdatedVersionIsRefused(clean, monkeypatch):
    store = createTestStore(ADMIN)
    product = addProduct(store['_id'], 'Pandesal', 5, inventory=10)
    original = db.update
    seen = []

    def withAConcurrentReservation(query, newValues, collection, *args,
                                   **kwargs):
        if collection == 'Products' and not seen:
            seen.append(True)
            original({'_id': query['_id'], '_version': query['_version']},
                     {'reservedInventory': [{'orderId': 'o9',
                                             'quantity': 4}]}, 'Products')
        return original(query, newValues, collection, *args, **kwargs)

    monkeypatch.setattr(db, 'update', withAConcurrentReservation)

    with pytest.raises(OutdatedDataError, match=exactly(OUTDATED)):
        api.updateStoreProduct(ADMIN, product['_id'], price=99, inventory=0)

    stored = _byName('Pandesal')
    assert (stored['price'], stored['inventory']) == (5.0, 10)
    assert stored['reservedInventory'] == [{'orderId': 'o9', 'quantity': 4}]


# ----------------------------- listMyStoreProducts ----------------------------- #


def test_listMyStoreProducts_includesHiddenProducts_sortedByName(clean):
    storeWithProducts(ADMIN)

    res = api.listMyStoreProducts(ADMIN)

    assertExactShape(AdminProductListResponse, res)
    assertRowKeys(res['products'], AdminProductListRow)
    assert res['currency'] == 'PHP'
    assert [p['name'] for p in res['products']] == [
        'Ensaymada', 'Old Recipe', 'Pandesal', 'Ube Cake']
    hidden = [p for p in res['products'] if p['name'] == 'Old Recipe']
    assert hidden[0]['isAvailable'] is False
    assert hidden[0]['soldCount'] == 999


def test_listMyStoreProducts_sortsWithoutRegardToCase(clean):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Zebra', 1)
    addProduct(store['_id'], 'apple', 1)
    addProduct(store['_id'], 'Mango', 1)

    names = [p['name'] for p in api.listMyStoreProducts(ADMIN)['products']]

    assert names == ['apple', 'Mango', 'Zebra']


def test_listMyStoreProducts_sumsTheReservations(clean):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'A held', 5, inventory=4, reservedInventory=RESERVED)
    addProduct(store['_id'], 'B one', 5, inventory=9,
               reservedInventory=[{'orderId': 'o3', 'quantity': 4}])
    addProduct(store['_id'], 'C none', 5, inventory=0)

    products = api.listMyStoreProducts(ADMIN)['products']

    assert [(p['name'], p['reservedQuantity'], p['inventory'])
            for p in products] == [('A held', 5, 4), ('B one', 4, 9),
                                   ('C none', 0, 0)]
    assert all('reservedInventory' not in p for p in products)
    # The sum is per row even when a limit cuts the list.
    [first] = api.listMyStoreProducts(ADMIN, limit=1)['products']
    assert first['reservedQuantity'] == 5


def test_listMyStoreProducts_searchAndLimit(clean):
    store = createTestStore(ADMIN)
    for number in range(12):
        addProduct(store['_id'], f'Item {number:02d}', 1)
    addProduct(store['_id'], 'Cake.* Special', 1, isAvailable=False)

    def names(**kwargs):
        return [p['name'] for p in
                api.listMyStoreProducts(ADMIN, **kwargs)['products']]

    assert names(search='.*') == ['Cake.* Special']
    assert names(search='  CAKE ') == ['Cake.* Special']
    assert len(names(search='')) == len(names(search='   ')) == 10
    assert names(search='item 1') == ['Item 10', 'Item 11']
    assert len(names(search='x' * 80)) == 0
    with pytest.raises(ValueError, match=exactly(
            'A search is at most 80 characters')):
        names(search='x' * 81)
    for search in (5, ['a'], {'$ne': 1}, True):
        with pytest.raises(ValueError, match=exactly(
                'The search must be text')):
            names(search=search)
    assert len(names()) == len(names(limit=None)) == 10
    assert names()[0] == 'Cake.* Special'
    assert len(names(limit=1)) == 1
    assert len(names(limit=50)) == 13
    for limit in (0, 51, -1, True, 2.5):
        with pytest.raises(ValueError, match=exactly(LIMIT)):
            names(limit=limit)


def test_listMyStoreProducts_anEmptyStore(clean):
    createTestStore(ADMIN, currency='USD')

    assert api.listMyStoreProducts(ADMIN) == {'currency': 'USD',
                                              'products': []}


def test_listMyStoreProducts_checkOrder(clean):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly('The search must be text')):
        api.listMyStoreProducts(ADMIN, search=5, limit=0)


def test_listMyStoreProducts_onlyTheCallersStore(clean):
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    addProduct(store['_id'], 'Cake A', 1)
    addProduct(other['_id'], 'Cake B', 1)

    mine = api.listMyStoreProducts(ADMIN, search='cake')['products']
    theirs = api.listMyStoreProducts(OTHER_ADMIN, search='cake')['products']

    assert [p['name'] for p in mine] == ['Cake A']
    assert [p['name'] for p in theirs] == ['Cake B']


# ----------------------------- the stores' own ownerStore ----------------------------- #


def test_ownerStore_returnsOnlyTheProjection_orRefusesAnAdminWithoutAStore(
        clean):
    createTestUser(ADMIN)

    with pytest.raises(ValueError, match=exactly(NO_STORE)):
        ownerStore(ADMIN, {'_id': 1})

    store = createTestStore(OTHER_ADMIN)
    assert ownerStore(OTHER_ADMIN, {'_id': 1, 'currency': 1}) == {
        '_id': store['_id'], 'currency': 'PHP'}

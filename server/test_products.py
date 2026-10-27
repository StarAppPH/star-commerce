"""listStoreProducts, called directly: a store's products as a customer
sees them, best sellers first."""

import pytest

from ApiRequests import ApiRequests
from conftest import (addProduct, assertExactShape, assertRowKeys,
                      createTestStore, guestAt, storeWithProducts,
                      wipeCollections)
from objects import ProductListResponse, ProductRow

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products']
ADMIN = 'pr-admin'
OTHER_ADMIN = 'pr-other-admin'
GUEST = 'pr-guest-uid'

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def test_listStoreProducts_bestSellersFirst_availableOnly(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    res = api.listStoreProducts(customerId, store['_id'])

    assertExactShape(ProductListResponse, res)
    assertRowKeys(res['products'], ProductRow)
    assert res['currency'] == 'PHP'
    # Ties on soldCount go by name.
    assert [p['name'] for p in res['products']
            ] == ['Pandesal', 'Ensaymada', 'Ube Cake']
    assert res['products'][0]['description'] == 'Soft bread roll'
    assert res['products'][0]['price'] == 5


def test_listStoreProducts_sendsTheStoresCurrency(clean, firebase):
    store = storeWithProducts(ADMIN, name='Tokyo Ramen', currency='JPY')
    customerId = guestAt(firebase, store, GUEST)

    assert api.listStoreProducts(customerId, store['_id'])['currency'] == 'JPY'


def test_listStoreProducts_onlyThatStoresProducts(clean, firebase):
    store = storeWithProducts(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    addProduct(other['_id'], 'Latte', 150, soldCount=5000)
    customerId = guestAt(firebase, store, GUEST)

    names = [p['name'] for p in api.listStoreProducts(customerId,
                                                      store['_id'])['products']]

    assert 'Latte' not in names
    assert [p['name'] for p in api.listStoreProducts(
        customerId, other['_id'])['products']] == ['Latte']


def test_listStoreProducts_searchAndLimit(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    assert [p['name'] for p in api.listStoreProducts(
        customerId, store['_id'], search='  UBE ')['products']] == ['Ube Cake']
    assert [p['name'] for p in api.listStoreProducts(
        customerId, store['_id'], limit=1)['products']] == ['Pandesal']
    assert api.listStoreProducts(customerId, store['_id'],
                                 search='')['products'][0]['name'] == 'Pandesal'


def test_listStoreProducts_aSearchIsMatchedLiterally(clean, firebase):
    """A regex character in the search is no operator: '.*' finds a name
    containing '.*', not every product."""
    store = storeWithProducts(ADMIN)
    addProduct(store['_id'], 'Cake.* Special', 99)
    customerId = guestAt(firebase, store, GUEST)

    res = api.listStoreProducts(customerId, store['_id'], search='.*')

    assert [p['name'] for p in res['products']] == ['Cake.* Special']


@pytest.mark.parametrize('limit', [0, 51, -1, True, 2.5])
def test_listStoreProducts_refusesALimitOutOfRange(clean, firebase, limit):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(ValueError, match='^limit is a whole number'):
        api.listStoreProducts(customerId, store['_id'], limit=limit)


def test_listStoreProducts_refusesALongSearch(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(ValueError, match='at most 80 characters$'):
        api.listStoreProducts(customerId, store['_id'], search='x' * 81)


def test_listStoreProducts_refusesAnUnknownStore(clean, firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(ValueError, match='^Store not found$'):
        api.listStoreProducts(customerId, 'pr-no-such-store')


def test_listStoreProducts_refusesAnAdmin(clean):
    store = storeWithProducts(ADMIN)

    with pytest.raises(PermissionError, match='^Only a customer can do this$'):
        api.listStoreProducts(ADMIN, store['_id'])


def test_listStoreProducts_withoutALimit_returnsTheDefault(clean, firebase):
    store = createTestStore(ADMIN)
    for number in range(12):
        addProduct(store['_id'], f'Item {number:02d}', 10)
    customerId = guestAt(firebase, store, GUEST)

    unset = api.listStoreProducts(customerId, store['_id'])
    none = api.listStoreProducts(customerId, store['_id'], limit=None)

    assert len(unset['products']) == 10
    assert none == unset


def test_listStoreProducts_sendsInventory_andListsSoldOutProducts(
        clean, firebase):
    store = createTestStore(ADMIN)
    addProduct(store['_id'], 'Pandesal', 5, soldCount=9, inventory=12,
               reservedInventory=[{'orderId': 'o1', 'quantity': 2}])
    addProduct(store['_id'], 'Sold Out', 5, soldCount=3, inventory=0)
    addProduct(store['_id'], 'Hidden', 5, soldCount=99, inventory=5,
               isAvailable=False)
    customerId = guestAt(firebase, store, GUEST)

    res = api.listStoreProducts(customerId, store['_id'])

    assertExactShape(ProductListResponse, res)
    assertRowKeys(res['products'], ProductRow)
    assert [(p['name'], p['inventory']) for p in res['products']] == [
        ('Pandesal', 12), ('Sold Out', 0)]
    for row in res['products']:
        assert 'reservedInventory' not in row
        assert 'nameKey' not in row


@pytest.mark.parametrize('search', [5, ['a'], True])
def test_listStoreProducts_refusesANonTextSearch(clean, firebase, search):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)

    with pytest.raises(ValueError, match='^The search must be text$'):
        api.listStoreProducts(customerId, store['_id'], search=search)

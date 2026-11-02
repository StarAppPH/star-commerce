"""hasInventory: a product whose stock orders lessen (true, the default) or
one customers order unlimited units of (false: inventory stays 0 and no
order, cancel or return moves it). A product stored before hasInventory
reads as true."""

import pytest

import actions.OrderActions as orderActions
from actions.ProductActions import NO_INVENTORY_KEPT, _validProduct
from ToolRequests import ToolRequests
from conftest import addProduct, createTestStore, wipeCollections
from mongoDb import db
from test_checkout import (ADMIN, T0, WINDOW, _expired, _order, _place,
                           _product, _shop, clock, exactly)
from test_fulfillment import _orderIn, refunds

tools = ToolRequests()
COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products',
               'Orders', 'OrderLocks', 'Policies', 'Returns', 'Carts']


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _unlimited(product: dict) -> dict:
    """`product` made one with no stock count, as addStoreProduct stores
    it (hasInventory false, inventory 0)."""
    db.db['Products'].update_one({'_id': product['_id']}, {'$set': {
        'hasInventory': False, 'inventory': 0}})
    return _product(product['_id'])


def _legacy(product: dict) -> dict:
    """`product` as stored before hasInventory existed: no such field."""
    db.db['Products'].update_one({'_id': product['_id']},
                                 {'$unset': {'hasInventory': ''}})
    return _product(product['_id'])


# ----------------------------- _validProduct (pure) ----------------------------- #


def test_validProduct_noInventory_needsNoCount_andStoresZero():
    assert _validProduct('A', None, 1, None, None, False, isNew=True) == {
        'name': 'A', 'description': None, 'price': 1.0,
        'hasInventory': False, 'inventory': 0, 'isAvailable': True}
    # A count sent with it is ignored, never stored.
    assert _validProduct('A', None, 1, 7, None, False,
                         isNew=True)['inventory'] == 0
    assert _validProduct(hasInventory=False, isNew=False) == {
        'hasInventory': False, 'inventory': 0}
    assert _validProduct(inventory=3, hasInventory=True, isNew=False) == {
        'hasInventory': True, 'inventory': 3}


def test_validProduct_aNewProductWithInventory_stillNeedsTheCount():
    with pytest.raises(ValueError, match='^The inventory is a whole number'):
        _validProduct('A', None, 1, None, None, True, isNew=True)


@pytest.mark.parametrize('value', ['no', 0, 1, []])
def test_validProduct_hasInventory_isTrueOrFalse(value):
    with pytest.raises(ValueError,
                       match=exactly('hasInventory is true or false')):
        _validProduct('A', None, 1, 1, None, value, isNew=True)


# ----------------------------- adding and changing ----------------------------- #


def test_addStoreProduct_withoutInventory_isUnlimited(clean):
    createTestStore(ADMIN)

    row = tools.addStoreProduct(ADMIN, 'Haircut', 300, hasInventory=False)

    assert (row['hasInventory'], row['inventory']) == (False, 0)
    stored = _product(row['_id'])
    assert (stored['hasInventory'], stored['inventory']) == (False, 0)


def test_addStoreProduct_byDefault_countsItsStock(clean):
    createTestStore(ADMIN)

    row = tools.addStoreProduct(ADMIN, 'Pandesal', 5, 12)

    assert (row['hasInventory'], row['inventory']) == (True, 12)
    assert _product(row['_id'])['hasInventory'] is True


def test_addStoreProducts_mixesBothKinds(clean):
    createTestStore(ADMIN)

    res = tools.addStoreProducts(ADMIN, [
        {'name': 'Cake', 'price': 9, 'inventory': 2},
        {'name': 'Delivery', 'price': 50, 'hasInventory': False}])

    assert [(p['name'], p['hasInventory'], p['inventory'])
            for p in res['products']] == [('Cake', True, 2),
                                          ('Delivery', False, 0)]


def test_updateStoreProduct_aCountOnAnUnlimitedProduct_isRefused(clean):
    store = createTestStore(ADMIN)
    product = _unlimited(addProduct(store['_id'], 'Haircut', 300))

    with pytest.raises(ValueError, match=exactly(NO_INVENTORY_KEPT)):
        tools.updateStoreProduct(ADMIN, product['_id'], inventory=5)
    assert _product(product['_id'])['_version'] == product['_version']

    row = tools.updateStoreProduct(ADMIN, product['_id'], inventory=5,
                                   hasInventory=True)
    assert (row['hasInventory'], row['inventory']) == (True, 5)
    row = tools.updateStoreProduct(ADMIN, product['_id'], hasInventory=False)
    assert (row['hasInventory'], row['inventory']) == (False, 0)


# ----------------------------- listing ----------------------------- #


def test_lists_sendHasInventory_trueForAProductStoredBeforeIt(clean,
                                                              firebase):
    store, customerId, products = _shop(firebase)
    _legacy(products['Pandesal'])
    _unlimited(products['Ube Cake'])

    seen = {p['name']: p['hasInventory'] for p in
            tools.listStoreProducts(customerId, store['_id'])['products']}
    assert seen == {'Pandesal': True, 'Ensaymada': True, 'Ube Cake': False}
    mine = {p['name']: p['hasInventory'] for p in
            tools.listMyStoreProducts(ADMIN)['products']}
    assert mine['Pandesal'] is True and mine['Ube Cake'] is False


def test_listMyStoreProducts_lowStock_leavesUnlimitedProductsOut(clean,
                                                                 firebase):
    _, _, products = _shop(firebase)
    _unlimited(products['Ube Cake'])

    names = [p['name'] for p in
             tools.listMyStoreProducts(ADMIN, inventoryAtMost=0)['products']]

    assert names == []


# ----------------------------- ordering ----------------------------- #


def test_createOrder_anUnlimitedProduct_takesAnyQuantity_andLessensNothing(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    cake = _unlimited(products['Ube Cake'])

    order = _place(customerId, store, (cake, 25))

    after = _product(cake['_id'])
    assert after['inventory'] == 0
    assert after['reservedInventory'] == [
        {'orderId': order['_id'], 'quantity': 25, 'tracked': False}]
    assert after['_version'] == cake['_version'] + 1


def test_cancellingAnUnlimitedProductsOrder_putsNothingBack(clean, firebase,
                                                            clock):
    store, customerId, products = _shop(firebase)
    cake = _unlimited(products['Ube Cake'])
    pandesal = products['Pandesal']
    order = _place(customerId, store, (cake, 3), (pandesal, 2))

    tools.cancelMyOrder(customerId, store['_id'], order['_id'])

    after = _product(cake['_id'])
    assert (after['inventory'], after['reservedInventory']) == (0, [])
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])


def test_aReservationTakenWithoutStock_staysOutAfterTheAdminCountsIt(
        clean, firebase, clock):
    """An order placed while the product had no stock count gave nothing
    out of inventory: its expiry puts nothing back, even once the admin
    has turned the count on."""
    store, customerId, products = _shop(firebase)
    cake = _unlimited(products['Ube Cake'])
    orderId = _expired(store, customerId, [(cake, 4)])
    db.db['Products'].update_one(
        {'_id': cake['_id']},
        {'$set': {'reservedInventory': [
            {'orderId': orderId, 'quantity': 4, 'tracked': False}]}})
    tools.updateStoreProduct(ADMIN, cake['_id'], inventory=6,
                             hasInventory=True)

    assert orderActions.cancelExpiredOrders() == 1

    after = _product(cake['_id'])
    assert (after['inventory'], after['reservedInventory']) == (6, [])
    assert _order(orderId)['status'] == 'cancelled'


def test_aProductStoredBeforeHasInventory_stillCountsItsStock(clean,
                                                              firebase,
                                                              clock):
    store, customerId, products = _shop(firebase)
    pandesal = _legacy(products['Pandesal'])

    order = _place(customerId, store, (pandesal, 4))
    assert _product(pandesal['_id'])['inventory'] == 6
    tools.cancelMyOrder(customerId, store['_id'], order['_id'])

    assert _product(pandesal['_id'])['inventory'] == 10
    with pytest.raises(ValueError):
        _place(customerId, store, (pandesal, 11))


# ----------------------------- the cart ----------------------------- #


def test_cart_anUnlimitedProduct_isNeverShortOfStock(clean, firebase):
    store, customerId, products = _shop(firebase)
    cake = _unlimited(products['Ube Cake'])

    cart = tools.addToCart(customerId, store['_id'],
                           [{'productId': cake['_id'], 'quantity': 40}])

    assert [(i['quantity'], i['inventory'], i['hasInventory'])
            for i in cart['items']] == [(40, 0, False)]
    preview = tools.previewMyCheckout(customerId, store['_id'])
    assert [item['quantity'] for item in preview['items']] == [40]


# ----------------------------- returns ----------------------------- #


def test_receiveReturn_restockingAnUnlimitedProduct_writesNothing(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    cake = _unlimited(products['Ube Cake'])
    orderId = _orderIn('completed', store, customerId, [(cake, 2)])
    tools.updateMyStorePolicy(ADMIN, defectReportDays=7)
    requested = tools.requestReturn(customerId, store['_id'], orderId,
                                    [{'item': 1, 'quantity': 2}], 'defective')
    tools.approveReturn(ADMIN, requested['_id'])
    before = _product(cake['_id'])

    received = tools.receiveReturn(ADMIN, requested['_id'], True)

    assert (received['status'], received['restocked']) == ('received', True)
    after = _product(cake['_id'])
    assert (after['inventory'], after['_version']) == (0, before['_version'])

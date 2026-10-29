"""Customer returns (ReturnActions), called directly: a smoke test of every
route. The full suite of returns tests is written separately."""

import pytest

from actions.ReturnActions import HANDOVER_UNKNOWN, PAST_STOCK_LIMIT
from ApiRequests import ApiRequests
from conftest import addOrder, assertExactShape, wipeCollections
from mongoDb import db
from objects import (PRODUCT_INVENTORY_MAX, MyReturnListResponse,
                     MyReturnRow, StoreReturnListResponse, StoreReturnRow)
from test_checkout import (ADMIN, COLLECTIONS, T0, WINDOW, _shop, atItem,
                           clock, exactly)
from test_fulfillment import _orderIn, refunds

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections([*COLLECTIONS, 'Policies', 'Returns'])
    yield
    wipeCollections([*COLLECTIONS, 'Policies', 'Returns'])


def test_everyReturnRoute_runsTheWholeFlow(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _orderIn('completed', store, customerId, [(pandesal, 3)])
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)

    requested = api.requestReturn(customerId, store['_id'], orderId,
                                  [{'item': 1, 'quantity': 1}], 'defective')
    assertExactShape(MyReturnRow, requested)
    assert (requested['status'], requested['refundAmount']) == (
        'requested', pandesal['price'])
    assertExactShape(MyReturnListResponse,
                     api.getMyReturns(customerId, store['_id']))
    assertExactShape(StoreReturnListResponse, api.getStoreReturns(ADMIN))

    approved = api.approveReturn(ADMIN, requested['_id'])
    assert approved['status'] == 'approved'
    received = api.receiveReturn(ADMIN, requested['_id'], True)
    assertExactShape(StoreReturnRow, received)
    assert (received['status'], received['restocked']) == ('received', True)
    assert received['refundedAt'] == T0
    stored = db.read({'_id': pandesal['_id']}, 'Products', findOne=True)
    assert stored['inventory'] == pandesal['inventory'] + 1
    assert stored['_version'] == pandesal['_version'] + 1

    second = api.requestReturn(customerId, store['_id'], orderId,
                               [{'item': 1, 'quantity': 1}], 'wrongItem')
    assert api.rejectReturn(ADMIN, second['_id'],
                            'Not what we sold')['status'] == 'rejected'
    third = api.requestReturn(customerId, store['_id'], orderId,
                              [{'item': 1, 'quantity': 2}], 'defective')
    assert api.cancelMyReturn(customerId, store['_id'],
                              third['_id'])['status'] == 'cancelled'


def _product(productId: str) -> dict:
    return db.read({'_id': productId}, 'Products', findOne=True)


def _approved(store: dict, customerId: str, orderId: str,
              items: list[dict]) -> str:
    """A return of `items` of the order, requested and approved."""
    requested = api.requestReturn(customerId, store['_id'], orderId, items,
                                  'defective')
    api.approveReturn(ADMIN, requested['_id'])
    return requested['_id']


def test_requestReturn_namesALeftoverRefusalByTheOrdersItemPosition(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    lines = [(product, 1) for product in list(products.values())[:2]]
    orderId = _orderIn('completed', store, customerId, lines)
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    api.requestReturn(customerId, store['_id'], orderId,
                      [{'item': 2, 'quantity': 1}], 'defective')

    with pytest.raises(ValueError,
                       match=exactly('Order item 2: only 0 left to return')):
        api.requestReturn(customerId, store['_id'], orderId,
                          [{'item': 1, 'quantity': 1},
                           {'item': 2, 'quantity': 1}], 'defective')
    with pytest.raises(ValueError, match=r'^Return item 2: '):
        api.requestReturn(customerId, store['_id'], orderId,
                          [{'item': 1, 'quantity': 1},
                           {'item': 1, 'quantity': 1}], 'defective')


def test_requestReturn_capsTheRefundAtWhatTheOrderLeft(clean, firebase,
                                                         clock):
    """3 units at 0.336 cost 1.01; three returns of one unit each round to
    0.34, so the last is capped at the 0.33 left."""
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _orderIn('completed', store, customerId,
                       [({**pandesal, 'price': 0.336}, 3)])
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)

    amounts = [api.requestReturn(customerId, store['_id'], orderId,
                                 [{'item': 1, 'quantity': 1}],
                                 'defective')['refundAmount']
               for _ in range(3)]

    assert amounts == [0.34, 0.34, 0.33]


def test_requestReturn_anOrderWithNoHandoverTime_isRefused(clean, firebase,
                                                           clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = addOrder(store['_id'], customerId,
                     [{'productId': pandesal['_id'], 'name': 'Pandesal',
                       'price': pandesal['price'], 'quantity': 1}],
                     'completed', T0 - WINDOW, paymentMethod='card',
                     paymentReference='DEMO-seed', paidAt=T0 - WINDOW)
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)

    with pytest.raises(ValueError, match=exactly(HANDOVER_UNKNOWN)):
        api.requestReturn(customerId, store['_id'], order['_id'],
                          [{'item': 1, 'quantity': 1}], 'defective')


def test_receiveReturn_pastTheStockLimit_isRefusedByOrderPosition(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    first, second = list(products.values())[:2]
    orderId = _orderIn('completed', store, customerId,
                       [(first, 1), (second, 1)])
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    returnId = _approved(store, customerId, orderId,
                         [{'item': 2, 'quantity': 1}])
    # Reserved units count against the limit too.
    db.db['Products'].update_one(
        {'_id': second['_id']},
        {'$set': {'inventory': PRODUCT_INVENTORY_MAX - 1,
                  'reservedInventory': [{'orderId': 'o-held',
                                         'quantity': 1}]}})
    before = _product(second['_id'])

    with pytest.raises(ValueError,
                       match=exactly(atItem(2, PAST_STOCK_LIMIT).replace(
                           'Item', 'Order item', 1))):
        api.receiveReturn(ADMIN, returnId, True)

    assert _product(second['_id']) == before
    assert refunds.calls == []
    # Refused before the flip: it can be received without restocking.
    received = api.receiveReturn(ADMIN, returnId, False)
    assert (received['restocked'], received['refundedAt']) == (False, T0)


def test_receiveReturn_aDeletedProduct_recordsItNotRestocked(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    first, second = list(products.values())[:2]
    orderId = _orderIn('completed', store, customerId,
                       [(first, 1), (second, 1)])
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    returnId = _approved(store, customerId, orderId,
                         [{'item': 1, 'quantity': 1},
                          {'item': 2, 'quantity': 1}])
    db.delete({'_id': second['_id']}, 'Products')

    received = api.receiveReturn(ADMIN, returnId, True)

    assertExactShape(StoreReturnRow, received)
    assert (received['status'], received['restocked']) == ('received', False)
    assert received['refundedAt'] == T0
    assert _product(first['_id'])['inventory'] == first['inventory'] + 1

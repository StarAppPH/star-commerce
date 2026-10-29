"""Customer returns (ReturnActions), called directly: a smoke test of every
route. The full suite of returns tests is written separately."""

import pytest

from ApiRequests import ApiRequests
from conftest import assertExactShape, wipeCollections
from mongoDb import db
from objects import (MyReturnListResponse, MyReturnRow,
                     StoreReturnListResponse, StoreReturnRow)
from test_checkout import (ADMIN, COLLECTIONS, T0, _shop, clock)
from test_fulfillment import _orderIn

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

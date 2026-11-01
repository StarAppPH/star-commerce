"""storeReport (ReportActions), called directly: each kind on orders seeded
around UTC+8 midnights, which orders count as sales, the total and the
limit, the refunds, the refusals, the tenant boundary, the indexes its
reads use and its time budget."""

import datetime
import re

import pytest
from pymongo.errors import ExecutionTimeout

import actions.ReportActions as reportActions
from actions.adminQueries import (ADMIN_QUERY_MAX_TIME_MS, RANGE_TOO_LONG,
                                  START_AFTER_END, TOO_BROAD)
from actions.OrderActions import INVALID_PERIOD
from actions.ReportActions import INVALID_KIND, NO_NAME, RANGE_REQUIRED
from ToolRequests import ToolRequests
from conftest import (addOrder, addReturn, appTime, assertExactShape,
                      createTestStore, createTestUser, freezeOrderClock,
                      guestAt, planStages, storeWithProducts,
                      wipeCollections)
from mongoDb import db
from objects import StoreReportResponse, aiCopy
from sharedConstants import SALES_STATUSES
from utils import _ISO_DATE_MESSAGE

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products',
               'Orders', 'Returns', 'Policies']
ADMIN = 'sr-admin'
OTHER_ADMIN = 'sr-other-admin'
GUEST = 'sr-guest-uid'
SENTINEL = 'ZQ-secret-9'
NOT_ADMIN = 'Only a store admin can do this'
NO_STORE = 'Create your store first'
LIMIT = 'limit is a whole number from 1 to 50'
UTC = datetime.timezone.utc
CUSTOMER = 'sr-customer'
MARCH_10 = {'startDate': '2026-03-10', 'endDate': '2026-03-10'}
MARCH = {'startDate': '2026-03-01', 'endDate': '2026-03-31'}

tools = ToolRequests()


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _item(productId: str, name: str, price: float, quantity: int) -> dict:
    return {'productId': productId, 'name': name, 'price': price,
            'quantity': quantity}


def _order(storeId: str, at: datetime.datetime, status: str = 'paid',
           items: list | None = None, fee: float = 0, **fields) -> dict:
    """An order of `items` placed at `at`; a fee makes it a delivery, its
    total the items' value plus the fee (as createOrder stores it)."""
    items = items or [_item('p1', 'Ube Cake', 100, 1)]
    if fee:
        fields = {'deliveryZone': 'Town', 'deliveryAddress': '1 Main St',
                  'deliveryLatitude': 14.5, 'deliveryLongitude': 121.0,
                  **fields}
    return addOrder(storeId, CUSTOMER, items, status, at, deliveryFee=fee,
                    **fields)


def _report(kind: str, **kwargs) -> dict:
    return tools.storeReport(ADMIN, kind, **kwargs)


def _rows(kind: str, **kwargs) -> list:
    return [(row['label'], row['orders'], row['units'], row['revenue'],
             row['deliveryFees']) for row in _report(kind, **kwargs)['rows']]


# ------------------------------- the kinds ------------------------------- #


def test_salesByDay_bucketsEachOrderInItsUtcPlus8Day(clean):
    store = createTestStore(ADMIN)
    # 03-09 23:59:59.999 local is 03-09 15:59:59.999Z; 03-10 00:00 local
    # is 03-09 16:00Z: one UTC day, two app days.
    _order(store['_id'], appTime(2026, 3, 9, 23, 59, 59, 999))
    _order(store['_id'], appTime(2026, 3, 10, 0, 0),
           items=[_item('p1', 'Ube Cake', 100, 2)])
    _order(store['_id'], appTime(2026, 3, 10, 23, 59, 59, 999), fee=50)
    _order(store['_id'], appTime(2026, 3, 11, 0, 0))

    assert _rows('salesByDay', startDate='2026-03-09',
                 endDate='2026-03-11') == [
        ('2026-03-09', 1, 1, 100.0, 0.0),
        ('2026-03-10', 2, 3, 350.0, 50.0),
        ('2026-03-11', 1, 1, 100.0, 0.0),
    ]
    # The range's own edges are whole app days too.
    assert _rows('salesByDay', **MARCH_10) == [
        ('2026-03-10', 2, 3, 350.0, 50.0)]


def test_salesKinds_countOnlyPaidAndCompletedOrders(clean, monkeypatch):
    store = createTestStore(ADMIN)
    at = appTime(2026, 3, 10, 9)
    # The pending order is still payable (ordersByStatus sweeps first).
    freezeOrderClock(monkeypatch, at + datetime.timedelta(minutes=5))
    for status in ('pending', 'awaitingPayment', 'paid', 'completed',
                   'cancelled'):
        _order(store['_id'], at, status)
    # A paid order cancelled later (refunded) is no sale either.
    _order(store['_id'], at, 'cancelled', paidAt=at, cancelReason='byStore',
           refundedAt=at)

    assert SALES_STATUSES == ('paid', 'completed')
    for kind in ('salesByDay', 'salesByProduct', 'salesByFulfillment'):
        res = _report(kind, **MARCH_10)
        assert [row['orders'] for row in res['rows']] == [2], kind
        assert (res['total']['orders'], res['total']['revenue']) == (2,
                                                                     200.0)
    assert _rows('ordersByStatus', **MARCH_10) == [
        ('cancelled', 2, None, None, None),
        ('awaitingPayment', 1, None, None, None),
        ('completed', 1, None, None, None),
        ('paid', 1, None, None, None),
        ('pending', 1, None, None, None),
    ]


def test_salesByProduct_unitsAndItemsRevenue_topFirst_newestName(clean):
    store = createTestStore(ADMIN)
    _order(store['_id'], appTime(2026, 3, 1, 9),
           items=[_item('cake', 'Ube Cake (old)', 600, 1),
                  _item('roll', 'Pandesal', 5, 10)], fee=40)
    _order(store['_id'], appTime(2026, 3, 2, 9),
           items=[_item('cake', 'Ube Cake', 650, 2)])
    _order(store['_id'], appTime(2026, 3, 3, 9), 'completed',
           items=[_item('tie-b', 'B tie', 25, 2), _item('tie-a', 'A tie', 50,
                                                          1)])

    res = _report('salesByProduct', **MARCH)

    # Revenue is the items' value, no delivery fee; the label is the
    # name its newest order stored; ties go by name.
    assert _rowsOf(res) == [
        ('Ube Cake', 2, 3, 1900.0, None),
        ('A tie', 1, 1, 50.0, None),
        ('B tie', 1, 2, 50.0, None),
        ('Pandesal', 1, 10, 50.0, None),
    ]
    assert _totalOf(res) == ('total', 3, 16, 2050.0, None)


def test_salesByFulfillment_anOldOrderIsAPickup(clean):
    store = createTestStore(ADMIN)
    _order(store['_id'], appTime(2026, 3, 1, 9), fee=40)
    _order(store['_id'], appTime(2026, 3, 1, 10), fee=60,
           items=[_item('p1', 'Ube Cake', 100, 3)])
    _order(store['_id'], appTime(2026, 3, 1, 11))
    legacy = _order(store['_id'], appTime(2026, 3, 1, 12))
    db.db['Orders'].update_one({'_id': legacy['_id']}, {'$unset': {
        'fulfillment': '', 'deliveryFee': ''}})

    res = _report('salesByFulfillment', **MARCH)

    assert _rowsOf(res) == [('delivery', 2, 4, 500.0, 100.0),
                            ('pickup', 2, 2, 200.0, 0.0)]
    assert _totalOf(res) == ('total', 4, 6, 700.0, 100.0)


def test_ordersByStatus_cancelsTheExpiredPendingOrdersFirst(clean):
    store = createTestStore(ADMIN)
    expired = _order(store['_id'], appTime(2026, 3, 1, 9), 'pending')

    res = _report('ordersByStatus', **MARCH)

    assert _rowsOf(res) == [('cancelled', 1, None, None, None)]
    assert _totalOf(res) == ('total', 1, None, None, None)
    assert res['refunds'] is None
    assert db.read({'_id': expired['_id']}, 'Orders',
                   findOne=True)['cancelReason'] == 'expired'


def _rowsOf(res: dict) -> list:
    return [(row['label'], row['orders'], row['units'], row['revenue'],
             row['deliveryFees']) for row in res['rows']]


def _totalOf(res: dict) -> tuple:
    total = res['total']
    return (total['label'], total['orders'], total['units'],
            total['revenue'], total['deliveryFees'])


# --------------------- the total, the limit, the shape --------------------- #


def test_theLimitCutsTheRows_neverTheTotal(clean):
    store = createTestStore(ADMIN)
    for day in range(1, 13):
        _order(store['_id'], appTime(2026, 3, day, 9))

    res = _report('salesByDay', **MARCH)
    assert [row['label'] for row in res['rows']] == [
        f'2026-03-{day:02d}' for day in range(1, 11)]
    assert res['truncated'] is True
    assert _totalOf(res) == ('total', 12, 12, 1200.0, 0.0)

    res = _report('salesByDay', limit=12, **MARCH)
    assert (len(res['rows']), res['truncated']) == (12, False)
    res = _report('salesByDay', limit=1, **MARCH)
    assert ([row['label'] for row in res['rows']], res['truncated']) == (
        ['2026-03-01'], True)
    assert _report('salesByDay', limit=50, **MARCH)['truncated'] is False


def test_salesByProduct_theLimitCutsInRevenueOrder_neverTheTotal(clean):
    store = createTestStore(ADMIN)
    for number in range(12):
        _order(store['_id'], appTime(2026, 3, 1, 9),
               items=[_item(f'p{number:02d}', f'Item {number:02d}',
                            number + 1, 1)])

    res = _report('salesByProduct', limit=3, **MARCH)

    assert [row['label'] for row in res['rows']] == ['Item 11', 'Item 10',
                                                     'Item 09']
    assert res['truncated'] is True
    assert _totalOf(res) == ('total', 12, 12, 78.0, None)
    res = _report('salesByProduct', limit=12, **MARCH)
    assert (len(res['rows']), res['truncated']) == (12, False)


def test_ordersByStatus_theLimitCutsTheSmallestCounts(clean, monkeypatch):
    store = createTestStore(ADMIN)
    at = appTime(2026, 3, 10, 9)
    freezeOrderClock(monkeypatch, at + datetime.timedelta(minutes=5))
    for status, count in (('paid', 3), ('completed', 2), ('pending', 1),
                          ('cancelled', 1)):
        for _ in range(count):
            _order(store['_id'], at, status)

    res = _report('ordersByStatus', limit=2, **MARCH_10)

    assert _rowsOf(res) == [('paid', 3, None, None, None),
                            ('completed', 2, None, None, None)]
    assert res['truncated'] is True
    assert _totalOf(res) == ('total', 7, None, None, None)
    assert _report('ordersByStatus', limit=4, **MARCH_10)['truncated'] is False


def test_salesByProduct_aMissingStoredName_isNeverNone(clean):
    """An order item stored with no name is labelled by its productId, and
    one with neither by NO_NAME, never "None"."""
    store = createTestStore(ADMIN)
    nameless = _order(store['_id'], appTime(2026, 3, 1, 9),
                      items=[_item('p-nameless', 'X', 30, 1)])
    bare = _order(store['_id'], appTime(2026, 3, 1, 10),
                  items=[_item('p-bare', 'Y', 20, 1)])
    db.db['Orders'].update_one({'_id': nameless['_id']},
                               {'$set': {'items.0.name': None}})
    db.db['Orders'].update_one({'_id': bare['_id']},
                               {'$unset': {'items.0.name': '',
                                           'items.0.productId': ''}})

    labels = [row['label'] for row in
              _report('salesByProduct', **MARCH)['rows']]

    assert labels == ['p-nameless', NO_NAME]
    assert NO_NAME == '(no name)'


def test_maxTimeMS_thatIsNoWholePositiveNumber_isAProgrammingError(clean):
    from mongoDb import _maxTimeKwargs
    assert _maxTimeKwargs(None) == {}
    assert _maxTimeKwargs(1) == {'maxTimeMS': 1}
    for bad in (0, -5, 2.5, True, '5000'):
        with pytest.raises(TypeError, match='maxTimeMS is a whole number'):
            _maxTimeKwargs(bad)
    with pytest.raises(TypeError):
        db.aggregate([], 'Orders', maxTimeMS=0)
    with pytest.raises(TypeError):
        db.read({}, 'Orders', maxTimeMS=False)


@pytest.mark.parametrize('kind', ['salesByProduct', 'salesByDay',
                                  'ordersByStatus', 'salesByFulfillment'])
def test_anEmptyRange_hasNoRows_andAZeroTotal(clean, kind):
    store = createTestStore(ADMIN, currency='USD')
    _order(store['_id'], appTime(2026, 2, 28, 23, 59))

    res = _report(kind, **MARCH)

    assertExactShape(StoreReportResponse, res)
    counted = kind != 'ordersByStatus'
    assert res == {
        'kind': kind, 'startDate': '2026-03-01', 'endDate': '2026-03-31',
        'currency': 'USD', 'rows': [], 'truncated': False,
        'total': {'label': 'total', 'orders': 0,
                  'units': 0 if counted else None,
                  'revenue': 0 if counted else None,
                  'deliveryFees': (0 if kind in ('salesByDay',
                                                 'salesByFulfillment')
                                   else None)},
        'refunds': 0 if counted else None,
    }


def test_theAiReadsTheWholeReport(clean):
    store = createTestStore(ADMIN)
    _order(store['_id'], appTime(2026, 3, 1, 9), fee=10)

    for kind in ('salesByProduct', 'salesByDay', 'ordersByStatus',
                 'salesByFulfillment'):
        res = _report(kind, **MARCH)
        assertExactShape(StoreReportResponse, res)
        assert aiCopy(StoreReportResponse, res) == res


def test_aPeriod_isWholeDaysOnTheServersClock(clean, monkeypatch):
    store = createTestStore(ADMIN)
    freezeOrderClock(monkeypatch, appTime(2026, 3, 10, 0, 30))
    _order(store['_id'], appTime(2026, 3, 3, 23, 59))
    _order(store['_id'], appTime(2026, 3, 4, 0, 0))
    _order(store['_id'], appTime(2026, 3, 10, 0, 10))

    res = _report('salesByDay', period='last7Days')

    assert (res['startDate'], res['endDate']) == ('2026-03-04', '2026-03-10')
    assert [row['label'] for row in res['rows']] == ['2026-03-04',
                                                     '2026-03-10']
    res = _report('salesByDay', period='yesterday')
    assert (res['startDate'], res['endDate'], res['rows']) == (
        '2026-03-09', '2026-03-09', [])


# ------------------------------- refunds ------------------------------- #


def test_refunds_areTheReturnsRefundedInTheRange_apartFromRevenue(clean):
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    _order(store['_id'], appTime(2026, 2, 20, 9), 'completed')
    at = appTime(2026, 2, 25, 9)
    # Refunded in March, of an order placed in February.
    addReturn(store['_id'], at, 'received', refundAmount=30.25,
              receivedAt=appTime(2026, 3, 1, 0, 0),
              refundedAt=appTime(2026, 3, 1, 0, 0))
    addReturn(store['_id'], at, 'received', refundAmount=12.5,
              receivedAt=appTime(2026, 3, 31, 23, 59, 59, 999),
              refundedAt=appTime(2026, 3, 31, 23, 59, 59, 999))
    # Outside the range, not refunded through the app (paid by hand), and
    # another store's: none counted.
    addReturn(store['_id'], at, 'received', refundAmount=1000,
              receivedAt=appTime(2026, 2, 28, 23, 59, 59, 999),
              refundedAt=appTime(2026, 2, 28, 23, 59, 59, 999))
    addReturn(store['_id'], at, 'received', refundAmount=1000,
              receivedAt=appTime(2026, 3, 5))
    addReturn(other['_id'], at, 'received', refundAmount=1000,
              receivedAt=appTime(2026, 3, 5), refundedAt=appTime(2026, 3, 5))

    res = _report('salesByDay', **MARCH)

    assert res['refunds'] == 42.75
    assert (res['rows'], res['total']['revenue']) == ([], 0)
    feb = _report('salesByDay', startDate='2026-02-01',
                  endDate='2026-02-28')
    assert (feb['total']['revenue'], feb['refunds']) == (100.0, 1000.0)


# ------------------------------- refusals ------------------------------- #


@pytest.mark.parametrize('kwargs, message', [
    ({'kind': 'salesByCustomer', **MARCH}, INVALID_KIND),
    ({'kind': SENTINEL, **MARCH}, INVALID_KIND),
    ({'kind': {'$ne': None}, **MARCH}, INVALID_KIND),
    ({'kind': 'salesByDay'}, RANGE_REQUIRED),
    ({'kind': 'salesByDay', 'startDate': '2026-03-01'}, RANGE_REQUIRED),
    ({'kind': 'salesByDay', 'endDate': '2026-03-01'}, RANGE_REQUIRED),
    ({'kind': 'salesByDay', 'startDate': '', 'endDate': ''}, RANGE_REQUIRED),
    ({'kind': 'salesByDay', 'startDate': '2026-03-02',
      'endDate': '2026-03-01'}, START_AFTER_END),
    ({'kind': 'salesByDay', 'startDate': '2026-01-01',
      'endDate': '2027-01-02'}, RANGE_TOO_LONG),
    ({'kind': 'salesByDay', 'startDate': SENTINEL,
      'endDate': '2026-03-01'}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'startDate': '2026-03-01T00:00:00',
      'endDate': '2026-03-01'}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'startDate': {'$gte': SENTINEL},
      'endDate': '2026-03-01'}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'startDate': '2026-03-01',
      'endDate': {'$lte': SENTINEL}}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'startDate': 20260301,
      'endDate': '2026-03-01'}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'startDate': '2026-03-01',
      'endDate': 20260301}, _ISO_DATE_MESSAGE),
    ({'kind': 'salesByDay', 'period': SENTINEL},
     INVALID_PERIOD),
    ({'kind': 'salesByDay', 'period': 'today', **MARCH},
     'Send either period or startDate/endDate, not both'),
    ({'kind': 'salesByDay', 'limit': 0, **MARCH}, LIMIT),
    ({'kind': 'salesByDay', 'limit': 51, **MARCH}, LIMIT),
    ({'kind': 'salesByDay', 'limit': True, **MARCH}, LIMIT),
    ({'kind': 'salesByDay', 'limit': SENTINEL, **MARCH}, LIMIT),
])
def test_storeReport_refusesABadParam_neverQuotingIt(clean, kwargs,
                                                     message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        tools.storeReport(ADMIN, **kwargs)

    assert SENTINEL not in str(error.value)


def test_aRangeOf366Days_isTaken(clean):
    store = createTestStore(ADMIN)
    _order(store['_id'], appTime(2028, 1, 1))
    _order(store['_id'], appTime(2028, 12, 31, 23, 59, 59, 999))

    res = _report('salesByDay', startDate='2028-01-01',
                  endDate='2028-12-31')

    assert res['total']['orders'] == 2


def test_storeReport_refusesACustomer_andAnAdminWithoutAStore(clean,
                                                              firebase):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)
    createTestUser('sr-no-store')

    for userId in (customerId, 'sr-nobody'):
        with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
            tools.storeReport(userId, 'salesByDay', **MARCH)
    with pytest.raises(ValueError, match=exactly(NO_STORE)):
        tools.storeReport('sr-no-store', 'salesByDay', **MARCH)


@pytest.mark.parametrize('kind', ['salesByProduct', 'salesByDay',
                                  'ordersByStatus', 'salesByFulfillment'])
def test_anotherStoresOrdersAndReturns_neverCount(clean, kind):
    createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    _order(other['_id'], appTime(2026, 3, 1, 9), fee=10)
    addReturn(other['_id'], appTime(2026, 3, 1, 9), 'received',
              receivedAt=appTime(2026, 3, 2), refundedAt=appTime(2026, 3, 2))

    res = _report(kind, **MARCH)

    assert (res['rows'], res['total']['orders']) == ([], 0)
    assert res['refunds'] in (0, None)
    assert _report(kind, **MARCH) != tools.storeReport(OTHER_ADMIN, kind,
                                                       **MARCH)


# ----------------------- indexes and the budget ----------------------- #


@pytest.mark.parametrize('kind', ['salesByProduct', 'salesByDay',
                                  'ordersByStatus', 'salesByFulfillment'])
def test_eachKind_readsThroughItsIndexes_withinTheBudget(clean,
                                                         sentPipelines,
                                                         kind):
    """The real pipelines' plans: the orders' $match scans the (storeId,
    status, createdAt) index within the range, the refunds' the
    (storeId, refundedAt) one; neither reads the collection."""
    db.ensureIndexes()
    store = createTestStore(ADMIN)
    for day in range(1, 29):
        _order(store['_id'], appTime(2026, 3, day, 9),
               ['paid', 'completed', 'pending', 'cancelled'][day % 4])
        addReturn(store['_id'], appTime(2026, 3, day, 9), 'received',
                  receivedAt=appTime(2026, 3, day, 10),
                  refundedAt=appTime(2026, 3, day, 10))

    _report(kind, **MARCH)

    reads = sentPipelines
    expected = {'Orders': [('storeId', 1), ('status', 1), ('createdAt', -1)],
                'Returns': [('storeId', 1), ('refundedAt', 1)]}
    assert [collection for collection, _, _ in reads] == (
        ['Orders'] if kind == 'ordersByStatus' else ['Orders', 'Returns'])
    for collection, pipeline, budget in reads:
        stages, indexes = planStages(collection, pipeline)
        assert 'COLLSCAN' not in stages
        assert expected[collection] in indexes
        assert budget == ADMIN_QUERY_MAX_TIME_MS


def test_aReadPastItsBudget_isTooBroad(clean, monkeypatch):
    createTestStore(ADMIN)

    def timesOut(*args, **kwargs):
        raise ExecutionTimeout('operation exceeded time limit', 50)

    monkeypatch.setattr(db, 'aggregate', timesOut)

    with pytest.raises(ValueError, match=exactly(TOO_BROAD)):
        _report('salesByProduct', **MARCH)


def test_theRefundsReadPastItsBudget_isTooBroad(clean, monkeypatch):
    """The second read (the refunds) runs within the same budget."""
    createTestStore(ADMIN)
    realAggregate = db.aggregate

    def refundsTimeOut(pipeline, collection, *args, **kwargs):
        if collection == 'Returns':
            raise ExecutionTimeout('operation exceeded time limit', 50)
        return realAggregate(pipeline, collection, *args, **kwargs)

    monkeypatch.setattr(db, 'aggregate', refundsTimeOut)

    with pytest.raises(ValueError, match=exactly(TOO_BROAD)):
        _report('salesByDay', **MARCH)


def test_maxTimeMS_reachesPymongo(clean, monkeypatch):
    """db.aggregate hands the budget to the driver (the real timeout is
    the server's; no failpoint runs on a local mongod)."""
    createTestStore(ADMIN)
    sent = []
    collectionType = type(db.db['Orders'])
    realAggregate = collectionType.aggregate

    def recording(self, pipeline, *args, **kwargs):
        sent.append((self.name, kwargs.get('maxTimeMS')))
        return realAggregate(self, pipeline, *args, **kwargs)

    monkeypatch.setattr(collectionType, 'aggregate', recording)

    _report('salesByDay', **MARCH)

    assert sent == [('Orders', ADMIN_QUERY_MAX_TIME_MS),
                    ('Returns', ADMIN_QUERY_MAX_TIME_MS)]


def test_everyKind_isAFixedPipeline():
    """No param reaches a stage: each kind's stages are the module's own,
    with no $lookup, $out, $merge, $where or $function anywhere."""
    text = repr(reportActions.REPORT_GROUPS)
    for banned in ('$lookup', '$out', '$merge', '$where', '$function',
                   '$accumulator'):
        assert banned not in text

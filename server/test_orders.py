"""The Order model and the order read routes, called directly:
getStoreOrders (the admin's store) and getMyOrders (the customer's own
orders at one store).

Time: "today" is decided by the server's clock in the app timezone (UTC+8).
The period tests freeze that clock (OrderActions.datetime) at two instants of
the same local day, 00:30 and 23:59:59, so midnight can never make them
flaky. The one test on the real clock asks for last7Days, which a test run
can cross no midnight of.

Every test that asserts an order, a truncation by limit or an equal list of
ids seeds createdAt = base + i minutes; the others compare sets.
"""

import datetime
import json
import re

import pytest
from pydantic import ValidationError

import actions.OrderActions as orderActions
from actions.adminQueries import RANGE_TOO_LONG, START_AFTER_END
from actions.OrderActions import (INVALID_PERIOD, INVALID_STATUS,
                                  ORDER_PERIODS, ORDER_STATUSES,
                                  PERIOD_AND_DATES)
from actions.ProductActions import (PRODUCT_LIST_DEFAULT_LIMIT,
                                    PRODUCT_LIST_MAX_LIMIT)
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addOrder, appTime, assertExactShape, assertRowKeys,
                      createTestStore, createTestUser, freezeOrderClock,
                      guestAt, orderIds, planStages, storeWithProducts, utcNow,
                      wipeCollections)
from mongoDb import db
from objects import (ORDER_ITEMS_MAX, MyOrderListResponse, MyOrderRow,
                     Order, OrderItemRow, StoreOrderListResponse,
                     StoreOrderRow, WaitingOnYouResponse)
from sharedConstants import WAITING_COUNT_CAP
from utils import _ISO_DATE_MESSAGE, appDayBounds

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Customers', 'Products', 'Orders'
]
ADMIN = 'or-admin'
OTHER_ADMIN = 'or-other-admin'
GUEST = 'or-guest-uid'
OTHER_GUEST = 'or-other-guest-uid'
SENTINEL = 'ZQ-secret-7'
NO_STORE = 'Create your store first'
NOT_ADMIN = 'Only a store admin can do this'
NOT_CUSTOMER = 'Only a customer can do this'
LIMIT = f'limit is a whole number from 1 to {PRODUCT_LIST_MAX_LIMIT}'
UTC = datetime.timezone.utc
BASE = datetime.datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
ITEMS = [{'productId': 'p1', 'name': 'Ube Cake', 'price': 650, 'quantity': 2}]

api = ApiRequests()
tools = ToolRequests()
# The real expiry sweep, kept for the one test the autouse stub below would
# make vacuous.
_REAL_SWEEP = orderActions.cancelExpiredOrders


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


@pytest.fixture(autouse=True)
def _listFiltersOnly(noExpirySweep):
    """These tests seed pending orders at fixed past instants; the expiry
    sweep the lists run first is test_checkout.py's."""


def _minute(index: int) -> datetime.datetime:
    """The createdAt of the index-th seeded order: BASE + index minutes."""
    return BASE + datetime.timedelta(minutes=index)


def _seed(storeId: str, customerId: str, count: int, **fields) -> list[str]:
    """`count` orders, the i-th created at BASE + i minutes; their _ids,
    oldest first."""
    return [
        addOrder(storeId, customerId, ITEMS, createdAt=_minute(i),
                 **fields)['_id'] for i in range(count)
    ]


def _storeAndCustomer(firebase) -> tuple[dict, str]:
    store = storeWithProducts(ADMIN)
    return store, guestAt(firebase, store, GUEST)


# ----------------------------- the clock ----------------------------- #

# Two instants of one local day, 2026-03-10: just after midnight and a
# second before the next one.
CLOCKS = [appTime(2026, 3, 10, 0, 30), appTime(2026, 3, 10, 23, 59, 59)]


# ----------------------------- the model ----------------------------- #


def _orderData(**fields) -> dict:
    return {
        'storeId': 's1',
        'customerId': 'c1',
        'items': [dict(ITEMS[0])],
        'total': 1300,
        'currency': 'PHP',
        'contactNumber': '+639171234567',
        'status': 'pending',
        'createdAt': BASE,
        'expiresAt': BASE + datetime.timedelta(minutes=30),
        **fields
    }


def _item(**fields) -> dict:
    return {**ITEMS[0], **fields}


@pytest.mark.parametrize('fields', [
    {},
    {'items': [_item(productId=f'p{i}') for i in range(ORDER_ITEMS_MAX)]},
    {'total': 0},
    {'items': [_item(quantity=1, price=0)]},
])
def test_order_acceptsItsBounds(fields):
    order = Order(**_orderData(**fields))

    assert order.createdAt.utcoffset() == datetime.timedelta(0)
    assert order.status == 'pending'


@pytest.mark.parametrize('fields', [
    {'items': []},
    {'items': [_item() for _ in range(ORDER_ITEMS_MAX + 1)]},
    {'items': [_item(quantity=0)]},
    {'items': [_item(price=-1)]},
    {'items': [_item(name='')]},
    {'items': [_item(productId='')]},
    {'items': [{'name': 'x', 'price': 1, 'quantity': 1}]},
    {'total': -1},
    {'status': 'shipped'},
    {'contactNumber': '0917'},
    {'currency': 'XYZ'},
    {'storeId': ''},
    {'customerId': ''},
])
def test_order_refusesWhatBreaksABound(fields):
    with pytest.raises(ValidationError):
        Order(**_orderData(**fields))


# ----------------------------- getStoreOrders ----------------------------- #


def test_getStoreOrders_newestFirst_andOnlyThisStores(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    otherStore = createTestStore(OTHER_ADMIN, 'Coffee B')
    # Seeded out of order: the sort, not the insert order, decides.
    middle = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=_minute(1))['_id']
    newest = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=_minute(2))['_id']
    oldest = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=_minute(0))['_id']
    addOrder(otherStore['_id'], customerId, ITEMS, createdAt=_minute(9))

    res = tools.getStoreOrders(ADMIN)

    assert orderIds(res) == [newest, middle, oldest]
    assert orderIds(tools.getStoreOrders(OTHER_ADMIN)) != orderIds(res)
    assert len(db.read({}, 'Orders')) == 4


def test_getStoreOrders_seesEveryCustomersOrdersAtItsStore(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    otherCustomerId = guestAt(firebase, store, OTHER_GUEST)
    mine = addOrder(store['_id'], customerId, ITEMS)['_id']
    theirs = addOrder(store['_id'], otherCustomerId, ITEMS)['_id']

    assert set(orderIds(tools.getStoreOrders(ADMIN))) == {mine, theirs}


def test_getStoreOrders_aStoreWithoutOrders_isAnEmptyList(clean):
    createTestStore(ADMIN)

    assert tools.getStoreOrders(ADMIN) == {'orders': []}


def test_getStoreOrders_statusFilter(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    byStatus = {
        status: addOrder(store['_id'], customerId, ITEMS, status,
                         _minute(i))['_id']
        for i, status in enumerate(ORDER_STATUSES)
    }

    for status, orderId in byStatus.items():
        assert orderIds(tools.getStoreOrders(ADMIN, status=status)) == [orderId]
    for status in (None, ''):
        assert set(orderIds(tools.getStoreOrders(
            ADMIN, status=status))) == set(byStatus.values())


def test_getStoreOrders_statusAndPeriod_combine(clean, firebase, monkeypatch):
    store, customerId = _storeAndCustomer(firebase)
    freezeOrderClock(monkeypatch, CLOCKS[0])
    today = appTime(2026, 3, 10, 9)
    keep = addOrder(store['_id'], customerId, ITEMS, 'paid',
                    today)['_id']
    addOrder(store['_id'], customerId, ITEMS, 'pending', today)
    addOrder(store['_id'], customerId, ITEMS, 'paid',
             appTime(2026, 3, 9, 9))

    res = tools.getStoreOrders(ADMIN, status='paid', period='today')

    assert orderIds(res) == [keep]


@pytest.mark.parametrize('kwargs, message', [
    ({'status': SENTINEL}, INVALID_STATUS),
    ({'status': 5}, INVALID_STATUS),
    ({'status': True}, INVALID_STATUS),
    ({'status': 'Pending'}, INVALID_STATUS),
    ({'period': SENTINEL}, INVALID_PERIOD),
    ({'period': 5}, INVALID_PERIOD),
    ({'period': 'Today'}, INVALID_PERIOD),
    ({'startDate': SENTINEL}, _ISO_DATE_MESSAGE),
    ({'endDate': SENTINEL}, _ISO_DATE_MESSAGE),
    ({'startDate': '2026-02-30'}, _ISO_DATE_MESSAGE),
    ({'endDate': '2026-13-01'}, _ISO_DATE_MESSAGE),
    ({'startDate': '２０２６-０３-１０'}, _ISO_DATE_MESSAGE),
    ({'endDate': '2026-03-10T00:00:00'}, _ISO_DATE_MESSAGE),
    ({'startDate': '2026-03-10T00:00:00+08:00'}, _ISO_DATE_MESSAGE),
    ({'startDate': '28/10/2026'}, _ISO_DATE_MESSAGE),
    ({'startDate': 20260310}, _ISO_DATE_MESSAGE),
    ({'startDate': '2026-03-11', 'endDate': '2026-03-10'}, START_AFTER_END),
    ({'period': 'today', 'startDate': '2026-03-10'}, PERIOD_AND_DATES),
    ({'period': 'today', 'endDate': '2026-03-10'}, PERIOD_AND_DATES),
    # A malformed date next to a period is the both-sent refusal, not a
    # date error.
    ({'period': 'today', 'startDate': SENTINEL}, PERIOD_AND_DATES),
    ({'period': 'today', 'endDate': '2026-02-30'}, PERIOD_AND_DATES),
    ({'limit': 0}, LIMIT),
    ({'limit': PRODUCT_LIST_MAX_LIMIT + 1}, LIMIT),
    ({'limit': -1}, LIMIT),
    ({'limit': 2.5}, LIMIT),
    ({'limit': True}, LIMIT),
    ({'limit': SENTINEL}, LIMIT),
])
def test_getStoreOrders_refusesABadParam_withAFixedMessage(
        clean, kwargs, message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        tools.getStoreOrders(ADMIN, **kwargs)

    assert SENTINEL not in str(error.value)


def test_getStoreOrders_dateBounds_areWholeDaysInUtcPlus8(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    # Day D is 2026-03-10 local = 03-09 16:00Z up to 03-10 15:59:59.999Z.
    instants = [
        datetime.datetime(2026, 3, 9, 15, 59, 59, 999000, tzinfo=UTC),  # D-1
        datetime.datetime(2026, 3, 9, 16, 0, tzinfo=UTC),  # D start
        datetime.datetime(2026, 3, 10, 15, 59, 59, 999000, tzinfo=UTC),  # D end
        datetime.datetime(2026, 3, 10, 16, 0, tzinfo=UTC),  # D+1 start
        datetime.datetime(2026, 3, 10, 17, 0, tzinfo=UTC),  # D+1
    ]
    ids = [
        addOrder(store['_id'], customerId, ITEMS, createdAt=at)['_id']
        for at in instants
    ]

    def found(**kwargs) -> set:
        return set(orderIds(tools.getStoreOrders(ADMIN, **kwargs)))

    assert found(startDate='2026-03-10', endDate='2026-03-10') == {
        ids[1], ids[2]}
    # One bound only: the other end is open, and the edges are the same.
    assert found(startDate='2026-03-10') == set(ids[1:])
    assert found(endDate='2026-03-10') == set(ids[:3])
    assert found(startDate='2026-03-11') == set(ids[3:])
    assert found(endDate='2026-03-09') == {ids[0]}
    # "" is no date.
    assert found(startDate='', endDate='') == set(ids)
    assert found(startDate='2026-03-10', endDate='') == set(ids[1:])


def test_getStoreOrders_aDayRangeAcrossAMonthBoundary(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    instants = [
        appTime(2026, 2, 28, 0, 0),
        appTime(2026, 2, 28, 23, 59, 59, 999),
        appTime(2026, 3, 1, 0, 0),
        appTime(2026, 3, 1, 23, 59, 59, 999),
        appTime(2026, 3, 2, 0, 0),
    ]
    ids = [
        addOrder(store['_id'], customerId, ITEMS, createdAt=at)['_id']
        for at in instants
    ]

    def found(start: str, end: str) -> set:
        return set(orderIds(tools.getStoreOrders(ADMIN, startDate=start,
                                               endDate=end)))

    assert found('2026-02-28', '2026-02-28') == {ids[0], ids[1]}
    assert found('2026-03-01', '2026-03-01') == {ids[2], ids[3]}
    assert found('2026-02-28', '2026-03-01') == set(ids[:4])
    assert found('2026-03-01', '2026-03-02') == set(ids[2:])


def test_getStoreOrders_periodDates_takeTheDatePath_whenPeriodIsEmpty(
        clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    inside = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=appTime(2026, 3, 10, 12))['_id']
    addOrder(store['_id'], customerId, ITEMS,
             createdAt=appTime(2026, 3, 11, 12))

    res = tools.getStoreOrders(ADMIN, period='', startDate='2026-03-10',
                             endDate='2026-03-10')

    assert orderIds(res) == [inside]
    assert len(orderIds(tools.getStoreOrders(ADMIN, period=''))) == 2


# 0 and 1 are the edges of 2026-02-09 .. 2026-03-10 etc.: each pair is the
# last millisecond of a local day and the first of the next.
PERIOD_INSTANTS = [
    appTime(2026, 2, 8, 23, 59, 59, 999),
    appTime(2026, 2, 9, 0, 0),
    appTime(2026, 3, 3, 23, 59, 59, 999),
    appTime(2026, 3, 4, 0, 0),
    appTime(2026, 3, 8, 23, 59, 59, 999),
    appTime(2026, 3, 9, 0, 0),
    appTime(2026, 3, 9, 23, 59, 59, 999),
    appTime(2026, 3, 10, 0, 0),
    appTime(2026, 3, 10, 23, 59, 59, 999),
    appTime(2026, 3, 11, 0, 0),
]
# Which of PERIOD_INSTANTS each period holds, with today = 2026-03-10.
PERIOD_INDEXES = {
    'today': [7, 8],
    'yesterday': [5, 6],
    'last7Days': [3, 4, 5, 6, 7, 8],
    'last30Days': [1, 2, 3, 4, 5, 6, 7, 8],
    # 2026-03-10 is a Tuesday: this week began Monday the 9th.
    'thisWeek': [5, 6, 7, 8],
    'lastWeek': [2, 3, 4],
    'thisMonth': [2, 3, 4, 5, 6, 7, 8],
    'lastMonth': [0, 1],
}


def test_orderPeriods_areTheOnesTested():
    assert set(PERIOD_INDEXES) == set(ORDER_PERIODS)


@pytest.mark.parametrize('clock', CLOCKS, ids=['0030', '235959'])
@pytest.mark.parametrize('period', list(PERIOD_INDEXES))
def test_getStoreOrders_periods_areWholeLocalDays(clean, firebase,
                                                  monkeypatch, period, clock):
    store, customerId = _storeAndCustomer(firebase)
    ids = [
        addOrder(store['_id'], customerId, ITEMS, createdAt=at)['_id']
        for at in PERIOD_INSTANTS
    ]
    freezeOrderClock(monkeypatch, clock)

    res = tools.getStoreOrders(ADMIN, period=period)

    # Distinct createdAt: newest first is the reverse of the seed order.
    assert orderIds(res) == [
        ids[i] for i in reversed(PERIOD_INDEXES[period])
    ]
    assert orderIds(tools.getStoreOrders(ADMIN, period=period,
                                       status='pending')) == orderIds(res)


def test_getStoreOrders_period_onTheRealClock(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    recent = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=utcNow())['_id']
    addOrder(store['_id'], customerId, ITEMS,
             createdAt=utcNow() - datetime.timedelta(days=30))

    assert orderIds(tools.getStoreOrders(ADMIN, period='last7Days')) == [recent]
    assert len(orderIds(tools.getStoreOrders(ADMIN, period='last30Days'))) >= 1


def test_getStoreOrders_limit(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    ids = _seed(store['_id'], customerId, PRODUCT_LIST_MAX_LIMIT + 2)
    newestFirst = list(reversed(ids))

    assert orderIds(tools.getStoreOrders(ADMIN)) == newestFirst[
        :PRODUCT_LIST_DEFAULT_LIMIT]
    assert orderIds(tools.getStoreOrders(ADMIN, limit=None)) == newestFirst[
        :PRODUCT_LIST_DEFAULT_LIMIT]
    assert orderIds(tools.getStoreOrders(ADMIN, limit=1)) == newestFirst[:1]
    assert orderIds(tools.getStoreOrders(
        ADMIN, limit=PRODUCT_LIST_MAX_LIMIT)) == newestFirst[
            :PRODUCT_LIST_MAX_LIMIT]


def test_getStoreOrders_refusesACustomer_andAnAdminWithoutAStore(
        clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    createTestUser('or-no-store')

    with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
        tools.getStoreOrders(customerId)
    with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
        tools.getStoreOrders('or-nobody')
    with pytest.raises((ValueError, PermissionError),
                       match=exactly(NO_STORE)):
        tools.getStoreOrders('or-no-store')


def test_getStoreOrders_rowShape_isTheItemSnapshot(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    addOrder(store['_id'], customerId, [
        {'productId': 'p1', 'name': 'Ube Cake', 'price': 650.5, 'quantity': 2},
        {'productId': 'p2', 'name': 'Tea', 'price': 3, 'quantity': 1},
    ], 'paid', _minute(0), currency='PHP', contactNumber='+639179999999')

    res = tools.getStoreOrders(ADMIN)

    assertExactShape(StoreOrderListResponse, res)
    assertRowKeys(res['orders'], StoreOrderRow)
    [row] = res['orders']
    assertRowKeys(row['items'], OrderItemRow)
    assert row['items'] == [
        {'name': 'Ube Cake', 'price': 650.5, 'quantity': 2},
        {'name': 'Tea', 'price': 3, 'quantity': 1},
    ]
    assert row['total'] == 1304
    assert row['status'] == 'paid'
    assert row['contactNumber'] == '+639179999999'
    assert row['createdAt'].utcoffset() == datetime.timedelta(0)
    assert row['createdAt'] == _minute(0)
    sent = json.dumps(res, default=str)
    for key in ('productId', 'storeId', 'customerId'):
        assert f'"{key}"' not in sent


def test_orderRoutes_doNotWrite(clean, firebase, monkeypatch):
    # Exempt from the autouse stub: the routes run the real expiry sweep,
    # which must find nothing to write. The orders are seeded in-window
    # (placed minutes ago), and a spy proves each route ran the sweep.
    sweeps = []

    def spy(storeId=None):
        sweeps.append(storeId)
        return _REAL_SWEEP(storeId)

    monkeypatch.setattr(orderActions, 'cancelExpiredOrders', spy)
    store, customerId = _storeAndCustomer(firebase)
    for minutes in range(3):
        addOrder(store['_id'], customerId, ITEMS,
                 createdAt=utcNow() - datetime.timedelta(minutes=minutes))
    before = db.read({}, 'Orders')

    tools.getStoreOrders(ADMIN, period='last30Days')
    tools.getMyOrders(customerId, store['_id'])

    assert db.read({}, 'Orders') == before
    assert {o['_version'] for o in before} == {0}
    assert sweeps == [store['_id'], store['_id']]


# ----------------------------- getMyOrders ----------------------------- #


def test_getMyOrders_onlyMineAtThisStore_newestFirst(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    otherCustomerId = guestAt(firebase, store, OTHER_GUEST)
    otherStore = createTestStore(OTHER_ADMIN, 'Coffee B')
    older = addOrder(store['_id'], customerId, ITEMS,
                     createdAt=_minute(1))['_id']
    newer = addOrder(store['_id'], customerId, ITEMS,
                     createdAt=_minute(3))['_id']
    # Another customer's order at the same store, and mine at another store.
    addOrder(store['_id'], otherCustomerId, ITEMS, createdAt=_minute(2))
    atOtherStore = addOrder(otherStore['_id'], customerId, ITEMS,
                            createdAt=_minute(4))['_id']

    assert orderIds(tools.getMyOrders(customerId, store['_id'])) == [
        newer, older]
    assert orderIds(tools.getMyOrders(customerId, otherStore['_id'])) == [
        atOtherStore]
    assert len(orderIds(tools.getMyOrders(otherCustomerId, store['_id']))) == 1


def test_getMyOrders_statusFilter(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    byStatus = {
        status: addOrder(store['_id'], customerId, ITEMS, status,
                         _minute(i))['_id']
        for i, status in enumerate(ORDER_STATUSES)
    }

    for status, orderId in byStatus.items():
        assert orderIds(tools.getMyOrders(customerId, store['_id'],
                                        status=status)) == [orderId]
    assert set(orderIds(tools.getMyOrders(customerId, store['_id'],
                                        status=''))) == set(byStatus.values())


@pytest.mark.parametrize('kwargs, message', [
    ({'status': SENTINEL}, INVALID_STATUS),
    ({'status': 5}, INVALID_STATUS),
    ({'limit': 0}, LIMIT),
    ({'limit': PRODUCT_LIST_MAX_LIMIT + 1}, LIMIT),
    ({'limit': True}, LIMIT),
    ({'limit': SENTINEL}, LIMIT),
])
def test_getMyOrders_refusesABadParam_withAFixedMessage(clean, firebase,
                                                        kwargs, message):
    store, customerId = _storeAndCustomer(firebase)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        tools.getMyOrders(customerId, store['_id'], **kwargs)

    assert SENTINEL not in str(error.value)


def test_getMyOrders_limit(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    ids = _seed(store['_id'], customerId, PRODUCT_LIST_MAX_LIMIT + 2)
    newestFirst = list(reversed(ids))

    assert orderIds(tools.getMyOrders(customerId, store['_id'])) == newestFirst[
        :PRODUCT_LIST_DEFAULT_LIMIT]
    assert orderIds(tools.getMyOrders(customerId, store['_id'],
                                    limit=1)) == newestFirst[:1]
    assert orderIds(tools.getMyOrders(
        customerId, store['_id'],
        limit=PRODUCT_LIST_MAX_LIMIT)) == newestFirst[:PRODUCT_LIST_MAX_LIMIT]


def test_getMyOrders_anUnknownStore_isAnEmptyList(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    addOrder(store['_id'], customerId, ITEMS)

    for storeId in ('or-no-such-store', ''):
        assert tools.getMyOrders(customerId, storeId) == {'orders': []}


def test_getMyOrders_refusesAnAdmin(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    addOrder(store['_id'], customerId, ITEMS)

    with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
        tools.getMyOrders(ADMIN, store['_id'])
    with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
        tools.getMyOrders('or-nobody', store['_id'])


def test_getMyOrders_rowShape_hasNoContactNumber(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    addOrder(store['_id'], customerId, ITEMS, 'completed', _minute(0))

    res = tools.getMyOrders(customerId, store['_id'])

    assertExactShape(MyOrderListResponse, res)
    assertRowKeys(res['orders'], MyOrderRow)
    [row] = res['orders']
    assertRowKeys(row['items'], OrderItemRow)
    assert row['items'] == [{'name': 'Ube Cake', 'price': 650, 'quantity': 2}]
    assert row['status'] == 'completed'
    assert row['total'] == 1300
    assert row['createdAt'].utcoffset() == datetime.timedelta(0)
    assert row['createdAt'] == _minute(0)
    sent = json.dumps(res, default=str)
    for key in ('productId', 'storeId', 'customerId', 'contactNumber'):
        assert f'"{key}"' not in sent


@pytest.mark.parametrize('storeId', [{'$ne': None}, ['a'], 5, None, True])
def test_getMyOrders_aStoreIdThatIsNoText_isRefused(clean, firebase, storeId):
    store, customerId = _storeAndCustomer(firebase)
    addOrder(store['_id'], customerId, ITEMS)

    with pytest.raises(ValueError, match=exactly('storeId must be text')):
        tools.getMyOrders(customerId, storeId)


# ----------------------------- getWaitingOnYou ----------------------------- #


def test_getWaitingOnYou_countsWhatWaitsAtTheAdminsStoreOnly(clean, firebase):
    wipeCollections(['Returns'])
    try:
        store, customerId = _storeAndCustomer(firebase)
        other = createTestStore(OTHER_ADMIN, 'Coffee B')
        _seed(store['_id'], customerId, 2, status='awaitingPayment')
        _seed(store['_id'], customerId, 3, status='paid')
        _seed(store['_id'], customerId, 1, status='pending')
        _seed(store['_id'], customerId, 1, status='completed')
        _seed(other['_id'], customerId, 4, status='paid')
        for storeId, status in ((store['_id'], 'requested'),
                                (store['_id'], 'approved'),
                                (other['_id'], 'requested')):
            db.create({'storeId': storeId, 'status': status, '_version': 0},
                      'Returns')

        res = api.getWaitingOnYou(ADMIN)

        assertExactShape(WaitingOnYouResponse, res)
        assert res == {'paymentsToConfirm': 2, 'ordersToHandOver': 3,
                       'returnsToDecide': 1}
    finally:
        wipeCollections(['Returns'])


def _seedWaiting(count: str, storeId: str, customerId: str, n: int):
    """n of what `count` counts, at the store."""
    if count == 'returnsToDecide':
        db.createMany([
            {'storeId': storeId, 'status': 'requested', '_version': 0}
            for _ in range(n)], 'Returns')
        return
    status = {'paymentsToConfirm': 'awaitingPayment',
              'ordersToHandOver': 'paid'}[count]
    _seed(storeId, customerId, n, status=status)


@pytest.mark.parametrize(
    'count', ['paymentsToConfirm', 'ordersToHandOver', 'returnsToDecide'])
def test_getWaitingOnYou_aCountStopsAtTheCap(clean, firebase, count):
    wipeCollections(['Returns'])
    try:
        store, customerId = _storeAndCustomer(firebase)
        _seedWaiting(count, store['_id'], customerId, WAITING_COUNT_CAP + 2)

        res = api.getWaitingOnYou(ADMIN)

        assert res[count] == WAITING_COUNT_CAP
        assert all(value == 0 for key, value in res.items() if key != count)
    finally:
        wipeCollections(['Returns'])


def test_getWaitingOnYou_refusesACustomer_andAnAdminWithoutAStore(
        clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    createTestUser(OTHER_ADMIN)

    with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
        api.getWaitingOnYou(customerId)
    with pytest.raises(ValueError, match=exactly(NO_STORE)):
        api.getWaitingOnYou(OTHER_ADMIN)


# ------------- getStoreOrders: filters, sorts and the range cap ------------- #


def test_getStoreOrders_fulfillmentFilter_anOldOrderIsAPickup(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    pickup = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=_minute(0))['_id']
    delivery = addOrder(store['_id'], customerId, ITEMS,
                        createdAt=_minute(1), fulfillment='delivery',
                        deliveryAddress='1 Main St', deliveryLatitude=14.5,
                        deliveryLongitude=121.0)['_id']
    legacy = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=_minute(2))['_id']
    db.db['Orders'].update_one({'_id': legacy},
                               {'$unset': {'fulfillment': ''}})

    assert orderIds(tools.getStoreOrders(ADMIN, fulfillment='pickup')) == [
        legacy, pickup]
    assert orderIds(tools.getStoreOrders(ADMIN, fulfillment='delivery')) == [
        delivery]
    for none in (None, ''):
        assert len(orderIds(tools.getStoreOrders(
            ADMIN, fulfillment=none))) == 3


def test_getStoreOrders_paymentMethodFilter_anOldEwalletIsAnEwallet(
        clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    ids = {
        method: addOrder(store['_id'], customerId, ITEMS, 'paid',
                         _minute(i), paymentMethod=method)['_id']
        for i, method in enumerate(['cash', 'card', 'eWallet', 'gcash',
                                    'bankTransfer', 'check', 'crypto'])
    }
    unchosen = addOrder(store['_id'], customerId, ITEMS,
                        createdAt=_minute(9))['_id']

    def found(method):
        return orderIds(tools.getStoreOrders(ADMIN, paymentMethod=method))

    assert found('eWallet') == [ids['gcash'], ids['eWallet']]
    for method in ('cash', 'card', 'bankTransfer', 'check', 'crypto'):
        assert found(method) == [ids[method]]
    assert unchosen in found(None)


def test_getStoreOrders_filtersCombine(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    keep = addOrder(store['_id'], customerId, ITEMS, 'paid', _minute(0),
                    paymentMethod='cash')['_id']
    addOrder(store['_id'], customerId, ITEMS, 'completed', _minute(1),
             paymentMethod='cash')
    addOrder(store['_id'], customerId, ITEMS, 'paid', _minute(2),
             paymentMethod='card')
    addOrder(store['_id'], customerId, ITEMS, 'paid', _minute(3),
             paymentMethod='cash', fulfillment='delivery',
             deliveryAddress='1 Main St', deliveryLatitude=14.5,
             deliveryLongitude=121.0)

    assert orderIds(tools.getStoreOrders(
        ADMIN, status='paid', paymentMethod='cash', fulfillment='pickup',
        startDate='2026-01-15', endDate='2026-01-15')) == [keep]


def test_getStoreOrders_sorts_tiedOnTheNewest(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)

    def placed(minute: int, price: float) -> str:
        return addOrder(store['_id'], customerId,
                        [{**ITEMS[0], 'price': price, 'quantity': 1}],
                        ['paid', 'pending', 'completed'][minute % 3],
                        _minute(minute))['_id']

    small, big, tieOld, tieNew = (placed(0, 5), placed(1, 900),
                                  placed(2, 50), placed(3, 50))

    def ids(**kwargs):
        return orderIds(tools.getStoreOrders(ADMIN, **kwargs))

    assert ids() == ids(sortBy='newest') == ids(sortBy='') == [
        tieNew, tieOld, big, small]
    assert ids(sortBy='oldest') == [small, big, tieOld, tieNew]
    assert ids(sortBy='total') == [big, tieNew, tieOld, small]
    # The whole store is sorted before the limit cuts it.
    assert ids(sortBy='total', limit=2) == [big, tieNew]


def test_getStoreOrders_aRangeOfAtMost366Days(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    first = addOrder(store['_id'], customerId, ITEMS,
                     createdAt=appTime(2028, 1, 1))['_id']
    last = addOrder(store['_id'], customerId, ITEMS,
                    createdAt=appTime(2028, 12, 31, 23, 59, 59, 999))['_id']

    # 2028 is a leap year: its 366 days are one range.
    assert set(orderIds(tools.getStoreOrders(
        ADMIN, startDate='2028-01-01', endDate='2028-12-31'))) == {first,
                                                                    last}
    for start, end in (('2028-01-01', '2029-01-01'),
                       ('2026-03-10', '2027-03-11')):
        with pytest.raises(ValueError, match=exactly(RANGE_TOO_LONG)):
            tools.getStoreOrders(ADMIN, startDate=start, endDate=end)
    tools.getStoreOrders(ADMIN, startDate='2026-03-10', endDate='2027-03-10')
    # An open end is no range to cap: the limit bounds the list.
    assert first in orderIds(tools.getStoreOrders(ADMIN,
                                                  startDate='2020-01-01'))


@pytest.mark.parametrize('kwargs, message', [
    ({'fulfillment': 'Pickup'}, 'fulfillment is one of pickup, delivery'),
    ({'fulfillment': SENTINEL}, 'fulfillment is one of pickup, delivery'),
    ({'fulfillment': ['pickup']}, 'fulfillment is one of pickup, delivery'),
    ({'paymentMethod': 'gcash'},
     'paymentMethod is one of cash, card, eWallet, bankTransfer, check, '
     'crypto'),
    ({'paymentMethod': {'$ne': None}},
     'paymentMethod is one of cash, card, eWallet, bankTransfer, check, '
     'crypto'),
    ({'sortBy': 'createdAt'}, 'sortBy is one of newest, oldest, total'),
    ({'sortBy': {'total': -1}}, 'sortBy is one of newest, oldest, total'),
    ({'sortBy': SENTINEL}, 'sortBy is one of newest, oldest, total'),
])
def test_getStoreOrders_refusesABadFilterOrSort_neverQuotingIt(
        clean, kwargs, message):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        tools.getStoreOrders(ADMIN, **kwargs)

    assert SENTINEL not in str(error.value)


def test_getStoreOrders_filtersNeverReachAnotherStore(clean, firebase):
    store, customerId = _storeAndCustomer(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    addOrder(other['_id'], customerId, ITEMS, 'paid', _minute(0),
             paymentMethod='cash')

    assert tools.getStoreOrders(ADMIN, status='paid', paymentMethod='cash',
                                fulfillment='pickup',
                                sortBy='total') == {'orders': []}


@pytest.mark.parametrize('sortBy', ['newest', 'oldest', 'total'])
@pytest.mark.parametrize('status', [None, 'paid'])
def test_getStoreOrders_eachSort_isServedByAnIndex(clean, firebase,
                                                   sentPipelines, sortBy,
                                                   status):
    """The plan the local mongod picks for the real pipeline: the sort's
    index read in order (no sort in memory) with the filters on. With a
    date range too, the planner may sort the range's orders instead (a
    range is at most 366 days), but it still scans an index, never the
    collection. More orders than the limit, as a real store has: with a
    handful, every plan ties and the planner may pick any."""
    from actions.adminQueries import ADMIN_QUERY_MAX_TIME_MS
    db.ensureIndexes()
    store, customerId = _storeAndCustomer(firebase)
    for i, orderStatus in enumerate(ORDER_STATUSES * 8):
        addOrder(store['_id'], customerId,
                 [{**ITEMS[0], 'price': i + 1}], orderStatus, _minute(i),
                 paymentMethod='cash')

    tools.getStoreOrders(ADMIN, status=status, sortBy=sortBy,
                         fulfillment='pickup', paymentMethod='cash')
    tools.getStoreOrders(ADMIN, status=status, sortBy=sortBy,
                         startDate='2026-01-01', endDate='2026-12-31')

    [(collection, pipeline, maxTimeMS), (_, ranged, _)] = sentPipelines
    stages, indexes = planStages(collection, pipeline)
    assert 'SORT' not in stages and 'COLLSCAN' not in stages
    assert indexes and all(index[:2] == [('storeId', 1), ('status', 1)]
                           for index in indexes)
    assert maxTimeMS == ADMIN_QUERY_MAX_TIME_MS
    stages, indexes = planStages(collection, ranged)
    assert 'COLLSCAN' not in stages
    assert indexes and all(index[:2] == [('storeId', 1), ('status', 1)]
                           for index in indexes)


def test_getStoreOrders_aReadPastItsBudget_isTooBroad(clean, monkeypatch):
    from pymongo.errors import ExecutionTimeout
    from actions.adminQueries import TOO_BROAD
    createTestStore(ADMIN)

    def timesOut(*args, **kwargs):
        raise ExecutionTimeout('operation exceeded time limit', 50)

    monkeypatch.setattr(db, 'aggregate', timesOut)

    with pytest.raises(ValueError, match=exactly(TOO_BROAD)):
        tools.getStoreOrders(ADMIN, sortBy='total')


# ------------------------- the calendar periods ------------------------- #

# Each clock (an app-timezone wall time) and the first and last day of each
# calendar period on it. 2026-03-09 is a Monday, 2026-03-15 a Sunday,
# 2026-01-01 a Thursday, 2028-03-01 a Wednesday of a leap year.
CALENDAR_PERIODS = [
    (appTime(2026, 3, 9, 0, 30), {
        'thisWeek': ('2026-03-09', '2026-03-09'),
        'lastWeek': ('2026-03-02', '2026-03-08'),
        'thisMonth': ('2026-03-01', '2026-03-09'),
        'lastMonth': ('2026-02-01', '2026-02-28'),
    }),
    (appTime(2026, 3, 15, 23, 59, 59), {
        'thisWeek': ('2026-03-09', '2026-03-15'),
        'lastWeek': ('2026-03-02', '2026-03-08'),
        'thisMonth': ('2026-03-01', '2026-03-15'),
        'lastMonth': ('2026-02-01', '2026-02-28'),
    }),
    (appTime(2026, 1, 1, 0, 30), {
        'thisWeek': ('2025-12-29', '2026-01-01'),
        'lastWeek': ('2025-12-22', '2025-12-28'),
        'thisMonth': ('2026-01-01', '2026-01-01'),
        'lastMonth': ('2025-12-01', '2025-12-31'),
    }),
    (appTime(2028, 3, 1, 12), {
        'thisWeek': ('2028-02-28', '2028-03-01'),
        'lastWeek': ('2028-02-21', '2028-02-27'),
        'thisMonth': ('2028-03-01', '2028-03-01'),
        'lastMonth': ('2028-02-01', '2028-02-29'),
    }),
]


@pytest.mark.parametrize('clock, periods', CALENDAR_PERIODS,
                         ids=['monday0030', 'sunday235959', 'january1',
                              'leapMarch1'])
def test_calendarPeriods_areWholeUtcPlus8DaysOnTheServersClock(
        monkeypatch, clock, periods):
    """thisWeek runs Monday to today, lastWeek the whole Monday-to-Sunday
    week before, thisMonth the 1st to today, lastMonth the whole month
    before, each from the first instant of its first UTC+8 day to the last
    of its last."""
    freezeOrderClock(monkeypatch, clock)

    for period, (first, last) in periods.items():
        # appDayBounds: first's midnight to last's final microsecond, UTC+8.
        assert orderActions._periodBounds(period) == appDayBounds(
            first, last), period


def test_calendarPeriods_filterTheOrdersList_atAMondayMidnight(
        clean, firebase, monkeypatch):
    """On a Monday at 00:30 local, thisWeek holds only that half hour's
    orders, and lastWeek ends at the Sunday's last millisecond."""
    store, customerId = _storeAndCustomer(firebase)
    sunday = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=appTime(2026, 3, 8, 23, 59, 59, 999))['_id']
    monday = addOrder(store['_id'], customerId, ITEMS,
                      createdAt=appTime(2026, 3, 9, 0, 10))['_id']
    lastMonday = addOrder(store['_id'], customerId, ITEMS,
                          createdAt=appTime(2026, 3, 2, 0, 0))['_id']
    addOrder(store['_id'], customerId, ITEMS,
             createdAt=appTime(2026, 3, 1, 23, 59, 59, 999))
    freezeOrderClock(monkeypatch, appTime(2026, 3, 9, 0, 30))

    assert orderIds(tools.getStoreOrders(ADMIN, period='thisWeek')) == [
        monday]
    assert orderIds(tools.getStoreOrders(ADMIN, period='lastWeek')) == [
        sunday, lastMonday]

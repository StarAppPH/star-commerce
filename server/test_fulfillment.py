"""Order fulfillment, called directly: completeOrder, cancelStoreOrder,
cancelMyOrder (FulfillmentActions) and the refund (payments.py).

Time: like test_checkout.py, every test that depends on it freezes
OrderActions.datetime (the one clock of the order routes, FulfillmentActions'
included, which imports _now) at T0 and seeds relative to T0 with
test_checkout's helpers. An order is payable until its expiresAt,
exclusive; a paid order is never expired.

Races are made deterministic with a hook, never threads: a wrapped
FulfillmentActions._moveOrder runs the competing move once, just before the
route's own. The one threaded test (racing completes) and the one that needs
a transaction to roll back (a failing product write) need a replica set. On
a standalone mongod there is no transaction: a failure between the status
flip and the product writes leaves the units reserved, never moved twice.
"""

import asyncio
import datetime
import threading
from types import SimpleNamespace

import pytest

import actions.FulfillmentActions as fulfillmentActions
import mcpServer
import payments
from actions.FulfillmentActions import ORDER_CHANGED, _refund
from actions.OrderActions import (NOT_MOVABLE, ORDER_EXPIRED,
                                  ORDER_NOT_FOUND, ORDER_STATUSES)
from ApiRequests import ApiRequests
from conftest import (addOrder, addProduct, assertExactShape, createTestStore,
                      verifiedGuestAt)
from mongoDb import db
from objects import (MAX_PENDING_ORDERS_PER_STORE, REFUND_REFERENCE_PREFIX,
                     MyOrderRow, StoreOrderRow, projection_of)
from test_checkout import (ADMIN, GUEST, MS, NOT_CUSTOMER, OTHER_ADMIN,
                           OTHER_GUEST, SENTINEL, T0, TOO_MANY_UNPAID, WINDOW,
                           _expired, _fresh, _heldOrder, _items, _order,
                           _paid, _place, _product, _products, _shop, clean,
                           clock, exactly)
from test_mcp_server import _auth, _text, callTool

NOT_ADMIN = 'Only a store admin can do this'
UTC = datetime.timezone.utc
ADMIN_ROUTES = ['completeOrder', 'cancelStoreOrder']
ALL_ROUTES = [*ADMIN_ROUTES, 'cancelMyOrder']
# What a hostile orderId looks like: the route refuses it without quoting it.
BAD_ORDER_IDS = ['', 123, {'$ne': None}, [SENTINEL], None]

api = ApiRequests()


# ----------------------------- fixtures ----------------------------- #


@pytest.fixture
def refunds(monkeypatch) -> SimpleNamespace:
    """A spy on the demo refund: `calls` holds its arguments; set `error`
    to make it raise."""
    spy = SimpleNamespace(calls=[], error=None)

    def refund(*args):
        spy.calls.append(args)
        if spy.error is not None:
            raise spy.error

    monkeypatch.setattr(fulfillmentActions, 'refundDemoPayment', refund)
    return spy


def _beforeTheMove(monkeypatch, action) -> None:
    """Run `action()` once inside FulfillmentActions, just before its first
    _moveOrder (the flip), so the competing move comes first."""
    real = fulfillmentActions._moveOrder
    ran: list[bool] = []

    def hooked(*args, **kwargs):
        if not ran:
            ran.append(True)
            action()
        return real(*args, **kwargs)

    monkeypatch.setattr(fulfillmentActions, '_moveOrder', hooked)


# ----------------------------- seeding ----------------------------- #


def _orderIn(status: str, store: dict, customerId: str, lines: list) -> str:
    """An order in `status`: pending and paid hold their units (as
    createOrder and payOrder leave them), completed and cancelled hold
    none. The paid one has expired at T0 (payment is never expired)."""
    if status == 'pending':
        return _fresh(store, customerId, lines)
    if status == 'paid':
        return _paid(store, customerId, lines)
    paidFields = dict(paymentMethod='card', paymentReference='DEMO-seed',
                      paidAt=T0 - WINDOW)
    if status == 'completed':
        return _heldOrder(store, customerId, lines, T0 - WINDOW, status,
                          hold=False, completedAt=T0, **paidFields)
    return _heldOrder(store, customerId, lines, T0 - WINDOW, status,
                      hold=False, cancelReason='byStore', cancelledAt=T0)


def _run(route: str, store: dict, customerId: str, orderId):
    """A fulfillment route as its caller: the store's admin, or the
    customer for cancelMyOrder."""
    if route == 'cancelMyOrder':
        return api.cancelMyOrder(customerId, store['_id'], orderId)
    return getattr(api, route)(ADMIN, orderId)


def _unchanged(store: dict, orderId: str):
    """A check that the order and every product of the store are as they
    are now."""
    order, products = _order(orderId), _products(store)

    def check() -> None:
        assert _order(orderId) == order
        assert _products(store) == products

    return check


# ----------------------------- completeOrder ----------------------------- #


def test_completeOrder_aPaidOrderBecomesCompleted(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    before = _order(orderId)

    res = api.completeOrder(ADMIN, orderId)

    assertExactShape(StoreOrderRow, res)
    stored = _order(orderId)
    assert res['_id'] == orderId
    assert (res['status'], res['completedAt']) == ('completed', T0)
    assert stored['status'] == 'completed'
    assert stored['completedAt'] == T0
    assert stored['completedAt'].utcoffset() == datetime.timedelta(0)
    assert stored['_version'] == before['_version'] + 1
    assert (stored['cancelledAt'], stored['cancelReason'],
            stored['refundedAt']) == (None, None, None)
    for key in ('items', 'total', 'paidAt', 'paymentReference'):
        assert stored[key] == before[key]


@pytest.mark.parametrize('lines', [
    [('Pandesal', 2)],
    [('Pandesal', 2), ('Ube Cake', 3)],
])
def test_completeOrder_countsTheUnitsAsSold_andKeepsInventory(
        clean, firebase, clock, lines):
    store, customerId, products = _shop(firebase)
    # Another open order holds units of the same product.
    otherId = _fresh(store, customerId, [(products['Pandesal'], 1)])
    orderId = _paid(store, customerId, [(products[n], q) for n, q in lines])
    before = _products(store)

    api.completeOrder(ADMIN, orderId)

    after = _products(store)
    bought = dict(lines)
    for name, product in after.items():
        old = before[name]
        quantity = bought.get(name, 0)
        assert product['soldCount'] == old['soldCount'] + quantity
        assert product['inventory'] == old['inventory']
        assert product['_version'] == old['_version'] + (1 if quantity else 0)
        assert product['reservedInventory'] == [
            r for r in old['reservedInventory'] if r['orderId'] != orderId]
    assert after['Pandesal']['reservedInventory'] == [
        {'orderId': otherId, 'quantity': 1}]


@pytest.mark.parametrize('status', ['pending', 'completed', 'cancelled'])
def test_completeOrder_refusesAnOrderThatIsNotPaid(
        clean, firebase, clock, status):
    store, customerId, products = _shop(firebase)
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE[status])):
        api.completeOrder(ADMIN, orderId)

    check()


def test_notMovable_coversEveryStatus():
    assert set(NOT_MOVABLE) == set(ORDER_STATUSES)


# ----------------------------- cancelStoreOrder ----------------------------- #


def test_cancelStoreOrder_aPendingOrderIsCancelled_andItsUnitsReleased(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    orderId = _fresh(store, customerId, [(products['Pandesal'], 2),
                                         (products['Ube Cake'], 3)])
    before, productsBefore = _order(orderId), _products(store)

    res = api.cancelStoreOrder(ADMIN, orderId)

    assertExactShape(StoreOrderRow, res)
    stored = _order(orderId)
    assert (res['status'], res['cancelReason'], res['cancelledAt']) == (
        'cancelled', 'byStore', T0)
    assert (stored['status'], stored['cancelReason'],
            stored['cancelledAt']) == ('cancelled', 'byStore', T0)
    assert stored['_version'] == before['_version'] + 1
    assert (stored['refundedAt'], stored['refundReference']) == (None, None)
    assert refunds.calls == []
    for name, product in _products(store).items():
        old = productsBefore[name]
        quantity = {'Pandesal': 2, 'Ube Cake': 3}.get(name, 0)
        assert product['inventory'] == old['inventory'] + quantity
        assert product['soldCount'] == old['soldCount']
        assert product['reservedInventory'] == [
            r for r in old['reservedInventory'] if r['orderId'] != orderId]
        assert product['_version'] == old['_version'] + (1 if quantity else 0)


def test_cancelStoreOrder_aPaidOrderIsCancelledAndRefunded(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    orderId = _paid(store, customerId, [(products['Ensaymada'], 4)])
    before = _order(orderId)
    productBefore = _product(products['Ensaymada']['_id'])

    res = api.cancelStoreOrder(ADMIN, orderId)

    assertExactShape(StoreOrderRow, res)
    stored = _order(orderId)
    [call] = refunds.calls
    assert call == (orderId, 'DEMO-seed', stored['refundReference'],
                    before['total'], 'PHP')
    assert stored['refundReference'].startswith(REFUND_REFERENCE_PREFIX)
    assert stored['refundedAt'] == T0
    assert (stored['status'], stored['cancelReason']) == (
        'cancelled', 'byStore')
    assert stored['_version'] == before['_version'] + 2
    assert (res['refundedAt'], res['refundReference']) == (
        T0, stored['refundReference'])
    after = _product(products['Ensaymada']['_id'])
    assert after['inventory'] == productBefore['inventory'] + 4
    assert after['reservedInventory'] == []


def test_theDemoRefund_returnsNothing_andMakesADemoReference():
    references = {payments.newRefundReference() for _ in range(20)}

    assert len(references) == 20
    assert all(r.startswith(REFUND_REFERENCE_PREFIX) for r in references)
    assert payments.refundDemoPayment('o1', 'DEMO-x', 'DEMO-R-x', 10.0,
                                      'PHP') is None


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_cancelStoreOrder_refusesACompletedOrAnAlreadyCancelledOne(
        clean, firebase, clock, refunds, status):
    store, customerId, products = _shop(firebase)
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE[status])):
        api.cancelStoreOrder(ADMIN, orderId)

    check()
    assert refunds.calls == []


def test_cancelStoreOrder_anExpiredPendingOrder_isCancelledAsExpired_once(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _expired(store, customerId, [(pandesal, 2)])

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['cancelled'])):
        api.cancelStoreOrder(ADMIN, orderId)

    stored = _order(orderId)
    assert (stored['status'], stored['cancelReason']) == (
        'cancelled', 'expired')
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert refunds.calls == []


def test_cancelStoreOrder_aRefundThatFails_leavesItCancelledWithoutRefundedAt(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _paid(store, customerId, [(pandesal, 2)])
    refunds.error = RuntimeError('gateway-secret-detail')

    with pytest.raises(RuntimeError):
        api.cancelStoreOrder(ADMIN, orderId)

    stored = _order(orderId)
    assert (stored['status'], stored['cancelReason']) == (
        'cancelled', 'byStore')
    assert (stored['refundedAt'], stored['refundReference']) == (None, None)
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert len(refunds.calls) == 1


def test_e2e_cancelStoreOrder_aFailedRefund_isAToolError_withoutItsDetail(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    refunds.error = RuntimeError('gateway-secret-detail')

    result = callTool(_auth(ADMIN), 'cancelStoreOrder', {'orderId': orderId},
                      audience='admin')

    assert result.is_error
    assert 'gateway-secret-detail' not in _text(result)
    assert _order(orderId)['status'] == 'cancelled'


def test_refund_onAnOrderAlreadyRefunded_recordsNothingTwice(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    orderId = _heldOrder(store, customerId, [(products['Pandesal'], 1)],
                         T0 - WINDOW, 'cancelled', hold=False,
                         cancelReason='byStore', cancelledAt=T0,
                         paymentMethod='card', paymentReference='DEMO-seed',
                         paidAt=T0 - WINDOW,
                         refundedAt=T0 - datetime.timedelta(minutes=1),
                         refundReference=REFUND_REFERENCE_PREFIX + 'old')
    stored = _order(orderId)

    res = _refund(stored, projection_of(StoreOrderRow))

    assert res['refundReference'] == REFUND_REFERENCE_PREFIX + 'old'
    assert res['refundedAt'] == stored['refundedAt']
    assert _order(orderId) == stored


# ----------------------------- cancelMyOrder ----------------------------- #


def test_cancelMyOrder_aPendingOrderIsCancelled_andItsUnitsReleased(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 2),
                   (products['Ube Cake'], 3))
    productsBefore, placed = _products(store), _order(order['_id'])

    res = api.cancelMyOrder(customerId, store['_id'], order['_id'])

    assertExactShape(MyOrderRow, res)
    stored = _order(order['_id'])
    assert (res['status'], res['cancelReason'], res['cancelledAt']) == (
        'cancelled', 'byCustomer', T0)
    assert (stored['status'], stored['cancelReason'],
            stored['cancelledAt']) == ('cancelled', 'byCustomer', T0)
    assert stored['refundedAt'] is None
    assert stored['_version'] == placed['_version'] + 1
    after = _products(store)
    assert after['Pandesal']['inventory'] == (
        productsBefore['Pandesal']['inventory'] + 2)
    assert after['Ube Cake']['inventory'] == (
        productsBefore['Ube Cake']['inventory'] + 3)
    assert all(p['reservedInventory'] == [] for p in after.values())


@pytest.mark.parametrize('status', ['paid', 'completed', 'cancelled'])
def test_cancelMyOrder_refusesAnOrderThatIsNotPending(
        clean, firebase, clock, status):
    store, customerId, products = _shop(firebase)
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE[status])):
        api.cancelMyOrder(customerId, store['_id'], orderId)

    check()


def test_cancelMyOrder_aPaidOrder_tellsTheCustomerToAskTheStore(
        clean, firebase, clock):
    assert NOT_MOVABLE['paid'] == (
        'This order is paid: ask the store to cancel it')


def test_cancelMyOrder_anExpiredOrder_saysItExpired_andReleasesItsUnitsOnce(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _expired(store, customerId, [(pandesal, 2)])

    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.cancelMyOrder(customerId, store['_id'], orderId)

    stored = _order(orderId)
    assert (stored['status'], stored['cancelReason']) == (
        'cancelled', 'expired')
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    # Already cancelled as expired: the same answer, nothing more released.
    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.cancelMyOrder(customerId, store['_id'], orderId)
    assert _product(pandesal['_id']) == after


@pytest.mark.parametrize('expiresIn, cancels', [(MS, True),
                                                (datetime.timedelta(0), False)])
def test_cancelMyOrder_atTheDeadline_isExpired_oneMillisecondBefore_cancels(
        clean, firebase, clock, expiresIn, cancels):
    store, customerId, products = _shop(firebase)
    orderId = _heldOrder(store, customerId, [(products['Pandesal'], 1)],
                         T0 - WINDOW + expiresIn)

    if cancels:
        res = api.cancelMyOrder(customerId, store['_id'], orderId)
        assert (res['status'], res['cancelReason']) == (
            'cancelled', 'byCustomer')
    else:
        with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
            api.cancelMyOrder(customerId, store['_id'], orderId)
        assert _order(orderId)['cancelReason'] == 'expired'


def test_cancelMyOrder_anotherCustomersOrder_andAnotherStoresOrder_areNotFound(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    otherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    others = _fresh(store, otherCustomer, [(products['Pandesal'], 1)])
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    otherStoreCustomer = verifiedGuestAt(firebase, other, 'ck-third-guest')
    elsewhere = _fresh(other, otherStoreCustomer, [(latte, 1)])
    checks = [_unchanged(store, others), _unchanged(other, elsewhere)]

    for orderId in (others, elsewhere, 'no-such-order'):
        with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
            api.cancelMyOrder(customerId, store['_id'], orderId)

    for check in checks:
        check()


def test_cancelMyOrder_afterCancelling_theCustomerCanPlaceAnotherOrder(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orders = [_place(customerId, store, (pandesal, 1))
              for _ in range(MAX_PENDING_ORDERS_PER_STORE)]
    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (pandesal, 1))

    api.cancelMyOrder(customerId, store['_id'], orders[0]['_id'])

    _place(customerId, store, (pandesal, 1))
    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (pandesal, 1))


def _paidCancellation(rule: str, hours: int | None = None) -> None:
    """The store's paidOrderCancellation answer (and its hours)."""
    api.updateMyStorePolicy(ADMIN, paidOrderCancellation=rule,
                            paidOrderCancellationHours=hours)


def _paidAt(store: dict, customerId: str, lines: list,
            paidAt: datetime.datetime) -> str:
    """A paid order, paid at `paidAt`."""
    return _heldOrder(store, customerId, lines, paidAt - WINDOW, 'paid',
                      paymentMethod='card', paymentReference='DEMO-seed',
                      paidAt=paidAt)


@pytest.mark.parametrize('rule, hours', [('beforeHandover', None),
                                         ('withinHours', 24)])
def test_cancelMyOrder_aPaidOrder_theStoreLets_isCancelledAndRefunded(
        clean, firebase, clock, refunds, rule, hours):
    store, customerId, products = _shop(firebase)
    _paidCancellation(rule, hours)
    pandesal = products['Pandesal']
    orderId = _paidAt(store, customerId, [(pandesal, 2)],
                      T0 - datetime.timedelta(hours=1))
    reserved, placed = _product(pandesal['_id']), _order(orderId)

    res = api.cancelMyOrder(customerId, store['_id'], orderId)

    assertExactShape(MyOrderRow, res)
    assert (res['status'], res['cancelReason'], res['cancelledAt'],
            res['refundedAt']) == ('cancelled', 'byCustomer', T0, T0)
    stored = _order(orderId)
    assert stored['refundReference'].startswith(REFUND_REFERENCE_PREFIX)
    assert stored['_version'] == placed['_version'] + 2
    assert refunds.calls == [(orderId, 'DEMO-seed', stored['refundReference'],
                              placed['total'], placed['currency'])]
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (
        reserved['inventory'] + 2, [])


@pytest.mark.parametrize('paidBefore, cancels', [
    (datetime.timedelta(hours=24) - MS, True),
    (datetime.timedelta(hours=24), False)])
def test_cancelMyOrder_withinHours_endsTheHoursAfterPayment(
        clean, firebase, clock, refunds, paidBefore, cancels):
    store, customerId, products = _shop(firebase)
    _paidCancellation('withinHours', 24)
    orderId = _paidAt(store, customerId, [(products['Pandesal'], 1)],
                      T0 - paidBefore)

    if cancels:
        res = api.cancelMyOrder(customerId, store['_id'], orderId)
        assert (res['status'], res['refundedAt']) == ('cancelled', T0)
    else:
        check = _unchanged(store, orderId)
        with pytest.raises(ValueError, match=exactly(
                'This store lets a paid order be cancelled only within 24 '
                'hours of payment: ask the store to cancel it')):
            api.cancelMyOrder(customerId, store['_id'], orderId)
        check()
        assert refunds.calls == []


@pytest.mark.parametrize('rule, hours', [(None, None), ('notAllowed', None)])
def test_cancelMyOrder_aPaidOrder_theStoreDoesNotLet_isRefused(
        clean, firebase, clock, refunds, rule, hours):
    store, customerId, products = _shop(firebase)
    if rule is not None:
        _paidCancellation(rule, hours)
    orderId = _paidAt(store, customerId, [(products['Pandesal'], 1)], T0)
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['paid'])):
        api.cancelMyOrder(customerId, store['_id'], orderId)

    check()
    assert refunds.calls == []


@pytest.mark.parametrize('rule, hours', [('beforeHandover', None),
                                         ('withinHours', 24)])
@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_cancelMyOrder_aHandedOverOrCancelledOrder_isRefused_whateverThePolicy(
        clean, firebase, clock, refunds, rule, hours, status):
    store, customerId, products = _shop(firebase)
    _paidCancellation(rule, hours)
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 1)])
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE[status])):
        api.cancelMyOrder(customerId, store['_id'], orderId)

    check()
    assert refunds.calls == []


def test_cancelMyOrderAgainstCompleteOrder_aHandoverThatWins_isNotCancelled(
        clean, firebase, clock, refunds, monkeypatch):
    store, customerId, products = _shop(firebase)
    _paidCancellation('beforeHandover')
    orderId = _paidAt(store, customerId, [(products['Pandesal'], 1)], T0)
    _beforeTheMove(monkeypatch, lambda: api.completeOrder(ADMIN, orderId))

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['completed'])):
        api.cancelMyOrder(customerId, store['_id'], orderId)

    assert _order(orderId)['status'] == 'completed'
    assert refunds.calls == []


# ----------------------------- shared refusals ----------------------------- #


@pytest.mark.parametrize('route', ALL_ROUTES)
def test_secondCall_isRefused_andMovesTheUnitsOnce(
        clean, firebase, clock, refunds, route):
    store, customerId, products = _shop(firebase)
    status = 'paid' if route == 'completeOrder' else 'pending'
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 2)])
    _run(route, store, customerId, orderId)
    check = _unchanged(store, orderId)

    second = 'completed' if route == 'completeOrder' else 'cancelled'
    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE[second])):
        _run(route, store, customerId, orderId)

    check()


def test_cancelStoreOrder_aPaidOrderTwice_refundsOnce(
        clean, firebase, clock, refunds):
    store, customerId, products = _shop(firebase)
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    api.cancelStoreOrder(ADMIN, orderId)
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['cancelled'])):
        api.cancelStoreOrder(ADMIN, orderId)

    check()
    assert len(refunds.calls) == 1


@pytest.mark.parametrize('route', ADMIN_ROUTES)
def test_adminRoutes_anotherStoresOrder_isNotFound(
        clean, firebase, clock, refunds, route):
    store, customerId, products = _shop(firebase)
    createTestStore(OTHER_ADMIN, 'Coffee B')
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        getattr(api, route)(OTHER_ADMIN, orderId)

    check()
    assert refunds.calls == []


@pytest.mark.parametrize('route', ADMIN_ROUTES)
@pytest.mark.parametrize('caller', ['customer', 'ck-nobody'])
def test_adminRoutes_refuseACustomer_andAnUnknownUser(
        clean, firebase, clock, refunds, route, caller):
    store, customerId, products = _shop(firebase)
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
        getattr(api, route)(customerId if caller == 'customer' else caller,
                            orderId)

    check()
    assert refunds.calls == []


@pytest.mark.parametrize('caller', [ADMIN, 'ck-nobody'])
def test_cancelMyOrder_refusesAnAdmin_andAnUnknownUser(
        clean, firebase, clock, caller):
    store, customerId, products = _shop(firebase)
    orderId = _fresh(store, customerId, [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
        api.cancelMyOrder(caller, store['_id'], orderId)

    check()


@pytest.mark.parametrize('route', ALL_ROUTES)
@pytest.mark.parametrize('orderId', BAD_ORDER_IDS)
def test_fulfillmentRoutes_refuseABadOrderId_withoutQuotingIt(
        clean, firebase, clock, refunds, route, orderId):
    store, customerId, products = _shop(firebase)
    held = _paid(store, customerId, [(products['Pandesal'], 2)])
    check = _unchanged(store, held)

    with pytest.raises(Exception) as error:
        _run(route, store, customerId, orderId)

    assert SENTINEL not in str(error.value)
    check()


@pytest.mark.parametrize('route', ALL_ROUTES)
def test_aPaidOrderPastItsDeadline_isNeverTreatedAsExpired(
        clean, firebase, clock, refunds, route):
    store, customerId, products = _shop(firebase)
    # _paid's deadline is T0: past at T0, yet a paid order is not expired.
    orderId = _paid(store, customerId, [(products['Pandesal'], 2)])
    assert _order(orderId)['expiresAt'] <= T0

    if route == 'cancelMyOrder':
        with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['paid'])):
            _run(route, store, customerId, orderId)
        assert _order(orderId)['status'] == 'paid'
    else:
        res = _run(route, store, customerId, orderId)
        assert res['status'] == (
            'completed' if route == 'completeOrder' else 'cancelled')
        assert (res['refundedAt'] == T0) is (route == 'cancelStoreOrder')


# ----------------------------- the races ----------------------------- #


@pytest.mark.parametrize('winner', ['complete', 'cancel'])
def test_completeAgainstCancel_firstFlipWins_theOtherIsRefused(
        clean, firebase, clock, refunds, monkeypatch, winner):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _paid(store, customerId, [(pandesal, 3)])
    reserved = _product(pandesal['_id'])
    if winner == 'cancel':
        _beforeTheMove(monkeypatch, lambda: api.cancelStoreOrder(ADMIN, orderId))
        loser, refusal = 'completeOrder', NOT_MOVABLE['cancelled']
    else:
        _beforeTheMove(monkeypatch, lambda: api.completeOrder(ADMIN, orderId))
        loser, refusal = 'cancelStoreOrder', NOT_MOVABLE['completed']

    with pytest.raises(ValueError, match=exactly(refusal)):
        getattr(api, loser)(ADMIN, orderId)

    after = _product(pandesal['_id'])
    assert after['reservedInventory'] == []
    if winner == 'cancel':
        assert after['inventory'] == reserved['inventory'] + 3
        assert after['soldCount'] == reserved['soldCount']
        assert _order(orderId)['status'] == 'cancelled'
        assert len(refunds.calls) == 1
    else:
        assert after['inventory'] == reserved['inventory']
        assert after['soldCount'] == reserved['soldCount'] + 3
        assert _order(orderId)['status'] == 'completed'
        assert refunds.calls == []


def test_cancelMyOrderAgainstPayOrder_aPaymentThatWins_isNotCancelled(
        clean, firebase, clock, monkeypatch):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 2))
    reserved = _product(pandesal['_id'])
    _beforeTheMove(monkeypatch, lambda: api.payOrder(
        customerId, store['_id'], order['_id'], 'card'))

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['paid'])):
        api.cancelMyOrder(customerId, store['_id'], order['_id'])

    assert _order(order['_id'])['status'] == 'paid'
    assert _product(pandesal['_id']) == reserved


def test_cancelStoreOrderAgainstCancelMyOrder_releasesTheUnitsOnce(
        clean, firebase, clock, refunds, monkeypatch):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 2))
    _beforeTheMove(monkeypatch, lambda: api.cancelMyOrder(
        customerId, store['_id'], order['_id']))

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['cancelled'])):
        api.cancelStoreOrder(ADMIN, order['_id'])

    stored = _order(order['_id'])
    assert (stored['status'], stored['cancelReason']) == (
        'cancelled', 'byCustomer')
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert refunds.calls == []


def test_aMoveThatMissesAnOrderInAnAcceptedStatus_saysItChanged(
        clean, firebase, clock, refunds, monkeypatch):
    store, customerId, products = _shop(firebase)
    orderId = _fresh(store, customerId, [(products['Pandesal'], 2)])
    monkeypatch.setattr(fulfillmentActions, '_moveOrder',
                        lambda *args, **kwargs: None)
    check = _unchanged(store, orderId)

    with pytest.raises(ValueError, match=exactly(ORDER_CHANGED)):
        api.cancelStoreOrder(ADMIN, orderId)

    check()


@pytest.mark.skipif(
    not db.replicaSetEnabled,
    reason='needs a replica set: racing completes are told apart by the '
    'status flip, and the product writes of the loser must not run. Never '
    'runs locally.')
def test_completeOrder_racingCompletes_countTheUnitsOnce(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _paid(store, customerId, [(pandesal, 3)])
    racers = 4
    barrier = threading.Barrier(racers)
    outcomes: list = [None] * racers

    def race(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = api.completeOrder(ADMIN, orderId)
        except Exception as error:  # noqa: BLE001 - asserted below
            outcomes[index] = error

    threads = [threading.Thread(target=race, args=(i,))
               for i in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    refused = [o for o in outcomes if isinstance(o, Exception)]
    assert len(outcomes) - len(refused) == 1
    assert all(isinstance(e, ValueError) for e in refused)
    assert _product(pandesal['_id'])['soldCount'] == (
        pandesal['soldCount'] + 3)


@pytest.mark.skipif(
    not db.replicaSetEnabled,
    reason='needs a replica set: on a standalone mongod, createTransaction '
    'opens no transaction, so a failing product write cannot roll back the '
    'status flip. Never runs locally.')
@pytest.mark.parametrize('route', ALL_ROUTES)
def test_aFailingProductWrite_rollsBackTheFlip(
        clean, firebase, clock, refunds, monkeypatch, route):
    store, customerId, products = _shop(firebase)
    status = 'paid' if route == 'completeOrder' else 'pending'
    orderId = _orderIn(status, store, customerId,
                       [(products['Pandesal'], 2)])
    check = _unchanged(store, orderId)

    def broken(*args, **kwargs):
        raise RuntimeError('write failed')

    monkeypatch.setattr(db, 'bulkUpdate', broken)
    with pytest.raises(RuntimeError):
        _run(route, store, customerId, orderId)
    monkeypatch.undo()

    check()
    assert refunds.calls == []


# ----------------------------- MCP ----------------------------- #


def test_e2e_theAdminCompletesAnOrder_afterListingTheStoresOrders(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _paid(store, customerId, [(pandesal, 2)])

    listed = callTool(_auth(ADMIN), 'getStoreOrders', {'status': 'paid'},
                      audience='admin')
    done = callTool(_auth(ADMIN), 'completeOrder', {'orderId': orderId},
                    audience='admin')

    assert [o['_id'] for o in listed.structured_content['orders']] == [orderId]
    assert not done.is_error, _text(done)
    assert done.structured_content['status'] == 'completed'
    assert _order(orderId)['status'] == 'completed'
    assert _product(pandesal['_id'])['soldCount'] == pandesal['soldCount'] + 2


def test_e2e_theCustomerPlacesThenCancelsAnOrder(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    headers = _auth(customerId, store['_id'])

    placed = callTool(headers, 'createOrder',
                      {'items': _items((pandesal, 2))})
    orderId = placed.structured_content['order']['_id']
    cancelled = callTool(headers, 'cancelMyOrder', {'orderId': orderId})

    assert not placed.is_error, _text(placed)
    assert not cancelled.is_error, _text(cancelled)
    assert cancelled.structured_content['status'] == 'cancelled'
    assert cancelled.structured_content['cancelReason'] == 'byCustomer'
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])


@pytest.mark.parametrize('audience, tool', [
    ('admin', 'completeOrder'),
    ('admin', 'cancelStoreOrder'),
    ('customer', 'cancelMyOrder'),
])
def test_e2e_aRefusal_doesNotQuoteTheOrderId(
        clean, firebase, clock, audience, tool):
    store, customerId, products = _shop(firebase)
    headers = (_auth(ADMIN) if audience == 'admin'
               else _auth(customerId, store['_id']))

    result = callTool(headers, tool, {'orderId': SENTINEL}, audience=audience)

    assert result.is_error
    assert SENTINEL not in _text(result)


def test_theFulfillmentToolsArguments_leaveOutTheInjectedParams():
    async def tools(audience):
        return {t.name: t
                for t in await mcpServer.servers[audience].list_tools()}

    admin, customer = asyncio.run(tools('admin')), asyncio.run(
        tools('customer'))

    for tool in (admin['completeOrder'], admin['cancelStoreOrder'],
                 customer['cancelMyOrder']):
        assert set(tool.input_schema['properties']) == {'orderId'}
        assert set(tool.input_schema['required']) == {'orderId'}


def test_theFulfillmentToolDescriptions_askToConfirmFirst():
    async def descriptions():
        return {
            audience: {t.name: t.description
                       for t in await mcpServer.servers[audience].list_tools()}
            for audience in ('admin', 'customer')
        }

    described = asyncio.run(descriptions())

    assert 'handed over' in described['admin']['completeOrder']
    assert 'demo' in described['admin']['cancelStoreOrder']
    for description in (described['admin']['completeOrder'],
                        described['admin']['cancelStoreOrder'],
                        described['customer']['cancelMyOrder']):
        assert 'confirm' in description
    assert 'ask the store' in described['customer']['cancelMyOrder']

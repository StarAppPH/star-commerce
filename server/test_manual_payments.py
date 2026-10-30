"""Payments made by hand to the store, called directly: payOrder with an
option that has no provider (awaitingPayment), addPaymentReference,
confirmOrderPayment, and how cancelling such an order refunds nothing
through the provider.

Time and seeding are test_checkout.py's (its clock at T0, its _shop, whose
store takes every way to pay: conftest's ALL_PAYMENT_METHODS).
"""

import asyncio
import datetime

import pytest

import actions.OrderActions as orderActions
import mcpServer
from actions.FulfillmentActions import _refund
from actions.OrderActions import NOT_MOVABLE, ORDER_NOT_FOUND, TOO_MANY_UNPAID
from actions.PaymentActions import (MANUAL_PAYMENT_REFERENCE_LABEL,
                                    NO_REFERENCE_NEEDED, NOT_CONFIRMABLE,
                                    NOT_PAYABLE)
from ApiRequests import ApiRequests
from conftest import (assertExactShape, createTestStore, takePayments,
                      verifiedGuestAt)
from objects import (MANUAL_PAYMENT_REFERENCE_MAX_LENGTH,
                     MAX_PENDING_ORDERS_PER_STORE, MyOrderRow, StoreOrderRow,
                     projection_of)
from test_checkout import (ADMIN, OTHER_ADMIN, OTHER_GUEST, SENTINEL, T0,
                           _fresh, _order, _place, _product, _shop,
                           charges, clean, clock, exactly)
from test_fulfillment import refunds

api = ApiRequests()


def _awaiting(firebase, method: str = 'bankTransfer', quantity: int = 2):
    """An order the customer chose to pay by hand with `method`:
    (store, customerId, the order as payOrder answered it, its product)."""
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, quantity))
    paid = api.payOrder(customerId, store['_id'], order['_id'], method)
    return store, customerId, paid, pandesal


# ----------------------------- payOrder by hand ----------------------------- #


def test_payOrder_byHand_awaitsTheStore_holdingTheUnits_withoutACharge(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 2))
    reserved = _product(pandesal['_id'])
    placed = _order(order['_id'])

    res = api.payOrder(customerId, store['_id'], order['_id'],
                       'bankTransfer', None)

    assertExactShape(MyOrderRow, res)
    stored = _order(order['_id'])
    assert res['status'] == stored['status'] == 'awaitingPayment'
    assert (stored['paymentMethod'], stored['paymentProvider'],
            stored['paymentReference'], stored['paidAt']) == (
                'bankTransfer', None, None, None)
    assert stored['_version'] == placed['_version'] + 1
    assert charges.calls == []
    assert _product(pandesal['_id']) == reserved


def test_payOrder_anOrderAwaitingPayment_cannotBePaidAgain(
        clean, firebase, clock, charges):
    store, customerId, paid, _ = _awaiting(firebase)
    before = _order(paid['_id'])

    with pytest.raises(ValueError,
                       match=exactly(NOT_PAYABLE['awaitingPayment'])):
        api.payOrder(customerId, store['_id'], paid['_id'], 'card', 'demo')

    assert _order(paid['_id']) == before
    assert charges.calls == []


def test_anOrderAwaitingPayment_neverExpires_andCountsAsUnpaid(
        clean, firebase, clock):
    store, customerId, paid, pandesal = _awaiting(firebase)
    for _ in range(MAX_PENDING_ORDERS_PER_STORE - 1):
        _place(customerId, store, (pandesal, 1))
    clock.advance(days=30)

    assert orderActions.cancelExpiredOrders() == (
        MAX_PENDING_ORDERS_PER_STORE - 1)
    assert _order(paid['_id'])['status'] == 'awaitingPayment'
    # The pending ones expired; the one awaiting payment still counts.
    for _ in range(MAX_PENDING_ORDERS_PER_STORE - 1):
        _place(customerId, store, (pandesal, 1))
    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (pandesal, 1))


# --------------------------- addPaymentReference --------------------------- #


def test_addPaymentReference_savesIt_stripped_andALaterOneReplacesIt(
        clean, firebase, clock):
    store, customerId, paid, _ = _awaiting(firebase)

    res = api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                  '  BDO-REF-123  ')
    assertExactShape(MyOrderRow, res)
    assert res['manualPaymentReference'] == 'BDO-REF-123'
    res = api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                  'BDO-REF-456')

    stored = _order(paid['_id'])
    assert stored['manualPaymentReference'] == res[
        'manualPaymentReference'] == 'BDO-REF-456'
    assert stored['status'] == 'awaitingPayment'


@pytest.mark.parametrize('reference, message', [
    ('', f'{MANUAL_PAYMENT_REFERENCE_LABEL} is 1 to '
     f'{MANUAL_PAYMENT_REFERENCE_MAX_LENGTH} characters'),
    ('x' * (MANUAL_PAYMENT_REFERENCE_MAX_LENGTH + 1),
     f'{MANUAL_PAYMENT_REFERENCE_LABEL} is 1 to '
     f'{MANUAL_PAYMENT_REFERENCE_MAX_LENGTH} characters'),
    (f'{SENTINEL}\x00', f'{MANUAL_PAYMENT_REFERENCE_LABEL} cannot contain '
     'control characters'),
    (5, f'{MANUAL_PAYMENT_REFERENCE_LABEL} must be text'),
])
def test_addPaymentReference_refusesABadReference_withoutQuotingIt(
        clean, firebase, clock, reference, message):
    store, customerId, paid, _ = _awaiting(firebase)
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(message)):
        api.addPaymentReference(customerId, store['_id'], paid['_id'],
                                reference)

    assert _order(paid['_id']) == before


def test_addPaymentReference_aPendingOrder_mustChooseHowToPayFirst(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))

    with pytest.raises(ValueError,
                       match=exactly(NO_REFERENCE_NEEDED['pending'])):
        api.addPaymentReference(customerId, store['_id'], order['_id'],
                                'REF-1')


def test_addPaymentReference_anotherCustomersOrder_isNotFound(
        clean, firebase, clock):
    store, customerId, paid, _ = _awaiting(firebase)
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.addPaymentReference(anotherCustomer, store['_id'], paid['_id'],
                                'REF-1')

    assert _order(paid['_id']) == before


# --------------------------- confirmOrderPayment --------------------------- #


def test_confirmOrderPayment_makesItPaid_thenItCanBeCompleted(
        clean, firebase, clock):
    store, customerId, paid, pandesal = _awaiting(firebase)
    api.addPaymentReference(customerId, store['_id'], paid['_id'], 'REF-9')
    reserved = _product(pandesal['_id'])
    clock.advance(hours=5)

    res = api.confirmOrderPayment(ADMIN, paid['_id'])

    assertExactShape(StoreOrderRow, res)
    stored = _order(paid['_id'])
    assert res['status'] == stored['status'] == 'paid'
    assert res['paidAt'] == stored['paidAt'] == T0 + datetime.timedelta(
        hours=5)
    assert (res['paymentMethod'], res['paymentProvider'],
            res['manualPaymentReference'], res['paymentReference']) == (
                'bankTransfer', None, 'REF-9', None)
    assert _product(pandesal['_id']) == reserved
    completed = api.completeOrder(ADMIN, paid['_id'])
    assert completed['status'] == 'completed'


@pytest.mark.parametrize('status', ['pending', 'paid'])
def test_confirmOrderPayment_refusesAnOrderNotAwaitingPayment(
        clean, firebase, clock, status):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    if status == 'paid':
        api.payOrder(customerId, store['_id'], order['_id'], 'card', 'demo')
    before = _order(order['_id'])

    with pytest.raises(ValueError, match=exactly(NOT_CONFIRMABLE[status])):
        api.confirmOrderPayment(ADMIN, order['_id'])

    assert _order(order['_id']) == before


def test_confirmOrderPayment_anotherStoresOrder_isNotFound(
        clean, firebase, clock):
    _, _, paid, _ = _awaiting(firebase)
    takePayments(createTestStore(OTHER_ADMIN, 'Coffee B'))
    before = _order(paid['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.confirmOrderPayment(OTHER_ADMIN, paid['_id'])

    assert _order(paid['_id']) == before


def test_completeOrder_refusesAnOrderAwaitingPayment(clean, firebase, clock):
    _, _, paid, _ = _awaiting(firebase, 'cash')

    with pytest.raises(ValueError,
                       match=exactly(NOT_MOVABLE['awaitingPayment'])):
        api.completeOrder(ADMIN, paid['_id'])


# ------------------------------- cancelling ------------------------------- #


def test_cancelMyOrder_anOrderAwaitingPayment_isCancelled_withoutARefund(
        clean, firebase, clock, refunds):
    store, customerId, paid, pandesal = _awaiting(firebase)

    res = api.cancelMyOrder(customerId, store['_id'], paid['_id'])

    assert (res['status'], res['cancelReason'], res['refundedAt']) == (
        'cancelled', 'byCustomer', None)
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert refunds.calls == []


def test_cancelStoreOrder_aConfirmedPaymentByHand_isNotRefundedByTheApp(
        clean, firebase, clock, refunds):
    _, _, paid, pandesal = _awaiting(firebase, 'check')
    api.confirmOrderPayment(ADMIN, paid['_id'])

    res = api.cancelStoreOrder(ADMIN, paid['_id'])

    assert (res['status'], res['refundedAt'], res['refundReference']) == (
        'cancelled', None, None)
    assert refunds.calls == []
    assert _product(pandesal['_id'])['reservedInventory'] == []


def test_refund_ofAnOrderPaidByHand_movesNothing(clean, firebase, clock,
                                                 refunds):
    store, customerId, products = _shop(firebase)
    orderId = _fresh(store, customerId, [(products['Pandesal'], 1)],
                     status='cancelled', paymentMethod='cash', paidAt=T0)
    order = _order(orderId)

    assert _refund(order, projection_of(StoreOrderRow)) == order
    assert refunds.calls == []


# ------------------------------ the MCP tools ------------------------------ #


def test_theManualPaymentTools_areOnTheirSidesServers():
    async def names(side):
        return {tool.name for tool in await mcpServer.servers[side]
                .list_tools()}

    assert 'addPaymentReference' in asyncio.run(names('customer'))
    assert 'confirmOrderPayment' in asyncio.run(names('admin'))
    assert 'confirmOrderPayment' not in asyncio.run(names('customer'))

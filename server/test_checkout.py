"""Checkout, the demo payment and the 30 minute expiry, called directly:
createOrder, payOrder, cancelExpiredOrders (the sweep the order lists,
createOrder and payOrder run, and job_cancelExpiredOrders.py's main).

Time: every test that depends on it freezes OrderActions.datetime (the one
clock of the order routes, payOrder's included) at T0, a whole second
because Mongo keeps milliseconds only, and seeds everything relative to T0.
An order is payable until its expiresAt, exclusive: at expiresAt it is
expired, one millisecond before it is not.

Races are made deterministic with hooks, never threads: a wrapped
newPaymentReference runs between payOrder's expiry check and its claim,
a wrapped _cancelExpiredOrder runs between the sweep's read and its
cancel. The one threaded test (the cap's lock) needs a replica set. On a
standalone mongod there is no transaction and no lock document: the cap
then holds for sequential creates only, and a refused item leaves the
earlier items reserved (an accepted limit, like addStoreProducts').
"""

import asyncio
import datetime
import json
import re
import threading
from types import SimpleNamespace

import pytest

import actions.OrderActions as orderActions
import actions.PaymentActions as paymentActions
import job_cancelExpiredOrders
import mcpServer
from actions.OrderActions import (ALREADY_IN_ORDER, ITEMS_COUNT, NOT_IN_STOCK,
                                  ORDER_STATUSES, PAYMENT_METHODS,
                                  PHONE_REQUIRED, PRODUCT_ID_NOT_TEXT,
                                  QUANTITY_RANGE, STORE_ID_NOT_TEXT,
                                  TOO_MANY_UNPAID)
from actions.PaymentActions import (INVALID_PAYMENT_METHOD, NOT_PAYABLE,
                                    ORDER_EXPIRED, ORDER_NOT_FOUND,
                                    PAYMENT_FAILED)
from ApiRequests import ApiRequests
from conftest import (addOrder, addProduct, assertExactShape, createTestStore,
                      guestAt, storeWithProducts,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (MAX_PENDING_ORDERS_PER_STORE, ORDER_ITEMS_MAX,
                     ORDER_PAYMENT_MINUTES, PAYMENT_REFERENCE_PREFIX,
                     PRODUCT_INVENTORY_MAX, CreatedOrderResponse,
                     MyOrderRow, NewOrderItem)
from utils import generateRandomString

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Customers', 'Products', 'Orders',
    'OrderLocks'
]
ADMIN = 'ck-admin'
OTHER_ADMIN = 'ck-other-admin'
GUEST = 'ck-guest-uid'
OTHER_GUEST = 'ck-other-guest-uid'
SENTINEL = 'ZQ-secret-7'
NOT_CUSTOMER = 'Only a customer can do this'
UTC = datetime.timezone.utc
# A whole second: Mongo truncates to milliseconds.
T0 = datetime.datetime(2026, 3, 10, 4, 0, 0, tzinfo=UTC)
WINDOW = datetime.timedelta(minutes=ORDER_PAYMENT_MINUTES)
MS = datetime.timedelta(milliseconds=1)

api = ApiRequests()


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def atItem(position: int, message: str) -> str:
    """A batch refusal as _atPosition words it."""
    return f'Item {position}: {message[:1].lower()}{message[1:]}'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


# ----------------------------- the clock ----------------------------- #


class Clock:

    def __init__(self, at: datetime.datetime) -> None:
        self.at = at

    def advance(self, **delta) -> None:
        self.at += datetime.timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """OrderActions' (and so payOrder's) clock, frozen at T0 until the
    test advances it."""
    frozen = Clock(T0)

    class Frozen:

        @staticmethod
        def now(tz):
            return frozen.at.astimezone(tz)

    monkeypatch.setattr(
        orderActions, 'datetime',
        SimpleNamespace(datetime=Frozen, timedelta=datetime.timedelta))
    return frozen


@pytest.fixture
def charges(monkeypatch) -> SimpleNamespace:
    """A spy on the payment charge: `calls` holds its arguments; set
    `error` to make it raise."""
    spy = SimpleNamespace(calls=[], error=None)

    def charge(*args):
        spy.calls.append(args)
        if spy.error is not None:
            raise spy.error

    monkeypatch.setattr(paymentActions, 'chargeDemoPayment', charge)
    return spy


# ----------------------------- seeding ----------------------------- #


def _products(store: dict) -> dict:
    return {
        p['name']: p
        for p in db.read({'storeId': store['_id']}, 'Products')
    }


def _product(productId: str) -> dict:
    return db.read({'_id': productId}, 'Products', findOne=True)


def _order(orderId: str) -> dict:
    return db.read({'_id': orderId}, 'Orders', findOne=True)


def _shop(firebase, currency: str = 'PHP'):
    """A store with its products, and a customer who verified their phone:
    (store, customerId, products by name)."""
    store = storeWithProducts(ADMIN, currency=currency)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    return store, customerId, _products(store)


def _items(*lines) -> list[dict]:
    return [{'productId': p['_id'], 'quantity': q} for p, q in lines]


def _place(customerId: str, store: dict, *lines) -> dict:
    """createOrder's order row."""
    return api.createOrder(customerId, store['_id'],
                           _items(*lines))['order']


def _heldOrder(store: dict,
               customerId: str,
               lines: list,
               createdAt: datetime.datetime,
               status: str = 'pending',
               hold: bool = True,
               **fields) -> str:
    """An order seeded as createOrder leaves it: the order, and each
    product's units moved into its reservedInventory under the order's id.
    `lines` is [(product, quantity)]; `createdAt` fixes expiresAt."""
    orderId = generateRandomString()
    items = [{'productId': p['_id'], 'name': p['name'], 'price': p['price'],
              'quantity': q} for p, q in lines]
    addOrder(store['_id'], customerId, items, status, createdAt,
             _id=orderId, **fields)
    if hold:
        for product, quantity in lines:
            db.db['Products'].update_one(
                {'_id': product['_id']},
                {'$push': {'reservedInventory': {'orderId': orderId,
                                                 'quantity': quantity}},
                 '$inc': {'inventory': -quantity, '_version': 1}})
    return orderId


def _expired(store, customerId, lines, **fields) -> str:
    """A pending order whose expiresAt is T0 - 1 second."""
    return _heldOrder(store, customerId, lines,
                      T0 - WINDOW - datetime.timedelta(seconds=1), **fields)


def _fresh(store, customerId, lines, **fields) -> str:
    """A pending order with a minute of its window left at T0."""
    return _heldOrder(store, customerId, lines,
                      T0 - WINDOW + datetime.timedelta(minutes=1), **fields)


def _paid(store, customerId, lines, **fields) -> str:
    return _heldOrder(store, customerId, lines, T0 - WINDOW, 'paid',
                      paymentMethod='card', paymentReference='DEMO-seed',
                      paidAt=T0 - WINDOW, **fields)


# ----------------------------- createOrder ----------------------------- #


def test_createOrder_reservesExactly_andSnapshotsTheItems(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    ube, pandesal, ensaymada = (products['Ube Cake'], products['Pandesal'],
                                products['Ensaymada'])

    res = api.createOrder(customerId, store['_id'],
                          _items((ube, 3), (pandesal, 2)))

    orderId = res['order']['_id']
    stored = _order(orderId)
    assert stored['status'] == 'pending'
    assert stored['_version'] == 0
    assert [(i['productId'], i['name'], i['price'], i['quantity'])
            for i in stored['items']] == [
                (ube['_id'], 'Ube Cake', 650, 3),
                (pandesal['_id'], 'Pandesal', 5, 2)]
    assert stored['total'] == 3 * 650 + 2 * 5
    after = _product(ube['_id'])
    assert after['inventory'] == 7
    assert after['reservedInventory'] == [{'orderId': orderId, 'quantity': 3}]
    assert after['_version'] == ube['_version'] + 1
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['_version']) == (8, 1)
    assert after['reservedInventory'] == [{'orderId': orderId, 'quantity': 2}]
    untouched = _product(ensaymada['_id'])
    assert untouched == ensaymada


def test_createOrder_totalIsRoundedToTheCent(clean, firebase, clock):
    store, customerId, _ = _shop(firebase)
    mint = addProduct(store['_id'], 'Mint', 0.1)

    order = _place(customerId, store, (mint, 3))

    assert order['total'] == 0.3
    assert _order(order['_id'])['total'] == 0.3


def test_createOrder_copiesTheStoresCurrency_theVerifiedPhone_andTheWindow(
        clean, firebase, clock):
    store = storeWithProducts(ADMIN, currency='USD')
    customerId = verifiedGuestAt(firebase, store, GUEST, '+639181112222')
    pandesal = _products(store)['Pandesal']

    res = api.createOrder(customerId, store['_id'], _items((pandesal, 1)))

    stored = _order(res['order']['_id'])
    assert stored['currency'] == res['order']['currency'] == 'USD'
    assert stored['contactNumber'] == '+639181112222'
    assert stored['customerId'] == customerId
    assert stored['createdAt'] == res['order']['createdAt'] == T0
    assert stored['expiresAt'] == res['order']['expiresAt'] == T0 + WINDOW


def test_createOrder_answersTheOrderAndEveryPaymentMethod(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)

    res = api.createOrder(customerId, store['_id'],
                          _items((products['Pandesal'], 1)))

    assertExactShape(CreatedOrderResponse, res)
    assert res['paymentMethods'] == list(PAYMENT_METHODS)
    assert len(PAYMENT_METHODS) == 5
    assert res['order']['status'] == 'pending'
    # The cap's lock document is no part of any answer.
    assert f"{customerId}:{store['_id']}" not in json.dumps(res, default=str)


def test_createOrder_takesDictItemsAndModelItems(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal, ensaymada = products['Pandesal'], products['Ensaymada']

    api.createOrder(customerId, store['_id'],
                    [{'productId': pandesal['_id'], 'quantity': 1}])
    api.createOrder(customerId, store['_id'], [
        NewOrderItem(productId=ensaymada['_id'], quantity=2)])

    assert _product(pandesal['_id'])['inventory'] == 9
    assert _product(ensaymada['_id'])['inventory'] == 8


def test_createOrder_exactStockSucceeds_thenTheProductIsSoldOut(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']

    _place(customerId, store, (pandesal, 10))

    assert _product(pandesal['_id'])['inventory'] == 0
    with pytest.raises(ValueError, match=exactly(atItem(1, NOT_IN_STOCK))):
        _place(customerId, store, (pandesal, 1))


def test_createOrder_refusesAnUnverifiedPhone(clean, firebase, clock):
    store = storeWithProducts(ADMIN)
    customerId = guestAt(firebase, store, GUEST)
    pandesal = _products(store)['Pandesal']

    with pytest.raises(PermissionError, match=exactly(PHONE_REQUIRED)):
        _place(customerId, store, (pandesal, 1))

    assert _product(pandesal['_id']) == pandesal
    assert db.read({}, 'Orders') == []


def test_createOrder_anUnknownStore_isRefused(clean, firebase, clock):
    store, customerId, products = _shop(firebase)

    with pytest.raises(ValueError, match=exactly('Store not found')):
        api.createOrder(customerId, SENTINEL,
                        _items((products['Pandesal'], 1)))


def test_createOrder_aStoreIdThatIsNoText_isRefused(clean, firebase, clock):
    store, customerId, products = _shop(firebase)

    with pytest.raises(ValueError, match=exactly(STORE_ID_NOT_TEXT)):
        api.createOrder(customerId, {'$ne': None},
                        _items((products['Pandesal'], 1)))


@pytest.mark.parametrize('count', [0, ORDER_ITEMS_MAX + 1])
def test_createOrder_refusesABadItemCount(clean, firebase, clock, count):
    store, customerId, products = _shop(firebase)
    item = {'productId': products['Pandesal']['_id'], 'quantity': 1}

    with pytest.raises(ValueError, match=exactly(ITEMS_COUNT)):
        api.createOrder(customerId, store['_id'], [item] * count)
    with pytest.raises(ValueError, match=exactly(ITEMS_COUNT)):
        api.createOrder(customerId, store['_id'], SENTINEL)

    assert db.read({}, 'Orders') == []


def test_createOrder_refusesADuplicateProduct_namingTheItem(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal, ensaymada = products['Pandesal'], products['Ensaymada']

    with pytest.raises(ValueError,
                       match=exactly(atItem(3, ALREADY_IN_ORDER))):
        _place(customerId, store, (pandesal, 1), (ensaymada, 1),
               (pandesal, 1))

    assert db.read({}, 'Orders') == []


@pytest.mark.parametrize(
    'quantity',
    [0, -1, PRODUCT_INVENTORY_MAX + 1, True, '2', 1.5, None, SENTINEL])
def test_createOrder_refusesABadQuantity_withoutQuotingIt(
        clean, firebase, clock, quantity):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']

    with pytest.raises(ValueError) as error:
        api.createOrder(customerId, store['_id'],
                        [{'productId': pandesal['_id'],
                          'quantity': quantity}])

    assert str(error.value) == atItem(1, QUANTITY_RANGE)
    assert SENTINEL not in str(error.value)
    assert _product(pandesal['_id']) == pandesal


@pytest.mark.parametrize('productId', [None, 5, ''])
def test_createOrder_refusesABadProductId(clean, firebase, clock, productId):
    store, customerId, _ = _shop(firebase)

    with pytest.raises(ValueError,
                       match=exactly(atItem(1, PRODUCT_ID_NOT_TEXT))):
        api.createOrder(customerId, store['_id'],
                        [{'productId': productId, 'quantity': 1}])


@pytest.mark.parametrize('which',
                         ['otherStore', 'hidden', 'unknown', 'short'])
def test_createOrder_aProductNoOneCanFill_getsOneMessage(
        clean, firebase, clock, which):
    """Not sold, not this store's, missing or short of stock: the same
    refusal, so it tells nothing about which. Single item, so the stock
    check is exact."""
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    othersProduct = addProduct(other['_id'], 'Latte', 150)
    target = {
        'otherStore': othersProduct,
        'hidden': products['Old Recipe'],
        'unknown': {'_id': SENTINEL},
        'short': products['Pandesal'],
    }[which]
    quantity = 11 if which == 'short' else 1

    with pytest.raises(ValueError) as error:
        api.createOrder(customerId, store['_id'],
                        [{'productId': target['_id'], 'quantity': quantity}])

    assert str(error.value) == atItem(1, NOT_IN_STOCK)
    assert SENTINEL not in str(error.value)
    for product in (*products.values(), othersProduct):
        assert _product(product['_id']) == product
    assert db.read({}, 'Orders') == []


def test_createOrder_aLaterItemThatFails_namesItsPosition(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)

    with pytest.raises(ValueError, match=exactly(atItem(2, NOT_IN_STOCK))):
        _place(customerId, store, (products['Pandesal'], 1),
               (products['Ensaymada'], 11))


@pytest.mark.skipif(
    not db.replicaSetEnabled,
    reason='needs a replica set: on a standalone mongod, createTransaction '
    'opens no transaction, so a failing second item cannot roll back the '
    'first reservation. This test never runs locally.')
def test_createOrder_aFailingSecondItem_rollsBackTheFirstReservation(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal, ensaymada = products['Pandesal'], products['Ensaymada']

    with pytest.raises(ValueError, match=exactly(atItem(2, NOT_IN_STOCK))):
        _place(customerId, store, (pandesal, 1), (ensaymada, 11))

    assert _product(pandesal['_id']) == pandesal
    assert db.read({}, 'Orders') == []


@pytest.mark.parametrize('route', ['createOrder', 'payOrder'])
@pytest.mark.parametrize('caller', [ADMIN, 'ck-nobody'])
def test_checkoutRoutes_refuseAnAdmin_andAnUnknownUser(
        clean, firebase, clock, route, caller):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 1))
    before = _product(pandesal['_id'])

    with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
        if route == 'createOrder':
            api.createOrder(caller, store['_id'], _items((pandesal, 1)))
        else:
            api.payOrder(caller, store['_id'], order['_id'], 'card')

    assert _product(pandesal['_id']) == before
    assert _order(order['_id'])['status'] == 'pending'


@pytest.mark.parametrize('call', [
    lambda api, c, s, o: api.createOrder(
        c, s, [{'productId': SENTINEL, 'quantity': 1}]),
    lambda api, c, s, o: api.createOrder(
        c, s, [{'productId': o, 'quantity': SENTINEL}]),
    lambda api, c, s, o: api.createOrder(c, SENTINEL, [{
        'productId': o, 'quantity': 1}]),
    lambda api, c, s, o: api.payOrder(c, s, SENTINEL, 'card'),
    lambda api, c, s, o: api.payOrder(c, s, o, SENTINEL),
    lambda api, c, s, o: api.payOrder(c, SENTINEL, o, 'card'),
])
def test_checkoutRefusals_neverQuoteWhatWasSent(clean, firebase, clock, call):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))

    with pytest.raises(ValueError) as error:
        call(api, customerId, store['_id'], order['_id'])

    for sent in (SENTINEL, store['_id'], order['_id'],
                 products['Pandesal']['_id']):
        assert sent not in str(error.value)


# ----------------------------- the cap ----------------------------- #


def test_createOrder_refusesAFourthUnpaidOrder(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    for _ in range(MAX_PENDING_ORDERS_PER_STORE):
        _place(customerId, store, (pandesal, 1))

    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (pandesal, 1))

    after = _product(pandesal['_id'])
    assert after['inventory'] == 10 - MAX_PENDING_ORDERS_PER_STORE
    assert len(after['reservedInventory']) == MAX_PENDING_ORDERS_PER_STORE
    assert len(db.read({}, 'Orders')) == MAX_PENDING_ORDERS_PER_STORE


def test_createOrder_theCapIsPerCustomerAndPerStore(clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    for _ in range(MAX_PENDING_ORDERS_PER_STORE):
        _place(customerId, store, (products['Pandesal'], 1))

    _place(anotherCustomer, store, (products['Ensaymada'], 1))
    _place(customerId, other, (latte, 1))

    assert len(db.read({'storeId': store['_id']}, 'Orders')) == (
        MAX_PENDING_ORDERS_PER_STORE + 1)
    assert len(db.read({'storeId': other['_id']}, 'Orders')) == 1


def test_createOrder_theCapIgnoresExpiredPaidAndOthersOrders(
        clean, firebase, clock, noExpirySweep):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    bulk = addProduct(store['_id'], 'Bulk', 1, inventory=100)
    line = [(bulk, 1)]
    for _ in range(MAX_PENDING_ORDERS_PER_STORE):
        _expired(store, customerId, line)
        _paid(store, customerId, line)
        _fresh(store, anotherCustomer, line)
        _fresh(other, customerId, line)

    order = _place(customerId, store, (bulk, 1))

    assert _order(order['_id'])['status'] == 'pending'


def test_createOrder_anOrderExpiringNow_isNoLongerCounted(
        clean, firebase, clock, noExpirySweep):
    store, customerId, products = _shop(firebase)
    line = [(products['Pandesal'], 1)]
    _fresh(store, customerId, line)
    _fresh(store, customerId, line)
    # expiresAt == now: expired, so not counted.
    _heldOrder(store, customerId, line, T0 - WINDOW)

    order = _place(customerId, store, (products['Pandesal'], 1))

    assert _order(order['_id'])['status'] == 'pending'


def test_createOrder_anOrderExpiringInOneMillisecond_stillCounts(
        clean, firebase, clock, noExpirySweep):
    store, customerId, products = _shop(firebase)
    line = [(products['Pandesal'], 1)]
    _fresh(store, customerId, line)
    _fresh(store, customerId, line)
    _heldOrder(store, customerId, line, T0 - WINDOW + MS)

    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        _place(customerId, store, (products['Pandesal'], 1))


def test_createOrder_usesTheUnitsAndTheSlotsExpiredOrdersHeld(
        clean, firebase, clock):
    """No patch: createOrder sweeps first, so three expired orders free
    both the cap and the stock."""
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    expired = [
        _expired(store, customerId, [(pandesal, quantity)])
        for quantity in (4, 3, 3)
    ]
    assert _product(pandesal['_id'])['inventory'] == 0

    order = _place(customerId, store, (pandesal, 10))

    assert {_order(o)['status'] for o in expired} == {'cancelled'}
    after = _product(pandesal['_id'])
    assert after['inventory'] == 0
    assert after['reservedInventory'] == [{'orderId': order['_id'],
                                           'quantity': 10}]


def test_createOrder_standaloneAndReplicaSet_lockDocumentOnlyWithATransaction(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    _place(customerId, store, (products['Pandesal'], 1))
    _place(customerId, store, (products['Pandesal'], 1))
    _place(customerId, other, (latte, 1))

    lockIds = {lock['_id'] for lock in db.read({}, 'OrderLocks')}

    # No transaction on a standalone mongod, so no lock to conflict in.
    assert lockIds == ({f"{customerId}:{store['_id']}",
                        f"{customerId}:{other['_id']}"}
                       if db.replicaSetEnabled else set())


@pytest.mark.skipif(
    not db.replicaSetEnabled,
    reason='needs a replica set: the cap\'s lock document is written inside '
    'a transaction, so racing creates conflict. Never runs locally.')
def test_createOrder_racingCreates_neverPassTheCap(clean, firebase, clock):
    store, customerId, _ = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    racers = 6
    stock = [addProduct(store['_id'], f'Racer {i}', 1) for i in range(racers)]
    barrier = threading.Barrier(racers)
    outcomes: list = [None] * racers

    def race(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = api.createOrder(
                customerId, store['_id'], _items((stock[index], 1)))
        except Exception as error:  # noqa: BLE001 - asserted below
            outcomes[index] = error

    threads = [threading.Thread(target=race, args=(i,))
               for i in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    refused = [o for o in outcomes if isinstance(o, Exception)]
    assert len(outcomes) - len(refused) == MAX_PENDING_ORDERS_PER_STORE
    assert all(isinstance(e, ValueError) and str(e) == TOO_MANY_UNPAID
               for e in refused)
    assert len(db.read({'customerId': customerId}, 'Orders')) == (
        MAX_PENDING_ORDERS_PER_STORE)
    for product in stock:
        after = _product(product['_id'])
        assert after['inventory'] + sum(
            r['quantity'] for r in after['reservedInventory']) == 10
    locks = db.read({}, 'OrderLocks')
    assert [lock['_id'] for lock in locks] == [f"{customerId}:{store['_id']}"]
    # Another customer and another store are not blocked by it.
    _place(anotherCustomer, store, (stock[0], 1))
    _place(customerId, other, (latte, 1))


# ----------------------------- payOrder ----------------------------- #


def test_payOrder_marksThePendingOrderPaid_andChargesOnce(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    order = _place(customerId, store, (ube, 2))
    reserved = _product(ube['_id'])
    clock.advance(minutes=5)

    res = api.payOrder(customerId, store['_id'], order['_id'], 'gcash')

    stored = _order(order['_id'])
    assertExactShape(MyOrderRow, res)
    assert 'paymentReference' not in res
    assert res['status'] == stored['status'] == 'paid'
    assert res['paymentMethod'] == stored['paymentMethod'] == 'gcash'
    assert res['paidAt'] == stored['paidAt'] == T0 + datetime.timedelta(
        minutes=5)
    assert stored['paymentReference'].startswith(PAYMENT_REFERENCE_PREFIX)
    assert stored['_version'] == 1
    assert (stored['total'], stored['expiresAt']) == (1300, T0 + WINDOW)
    assert [(i['name'], i['price'], i['quantity'])
            for i in stored['items']] == [('Ube Cake', 650, 2)]
    # The charge got the order's id, the stored reference, the total, the
    # currency and the method; once.
    assert charges.calls == [(order['_id'], stored['paymentReference'], 1300,
                              'PHP', 'gcash')]
    # The units stay reserved until the order is completed.
    assert _product(ube['_id']) == reserved


@pytest.mark.parametrize('method', PAYMENT_METHODS)
def test_payOrder_everyMethodWorks(clean, firebase, clock, method):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))

    res = api.payOrder(customerId, store['_id'], order['_id'], method)

    assert (res['status'], res['paymentMethod']) == ('paid', method)


@pytest.mark.parametrize(
    'method', [None, '', 'cash', 'CARD', 5, ['card'], {'a': 1}, SENTINEL])
def test_payOrder_refusesABadMethod_withoutQuotingIt(
        clean, firebase, clock, charges, method):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    before = _order(order['_id'])

    with pytest.raises(ValueError, match=exactly(INVALID_PAYMENT_METHOD)):
        api.payOrder(customerId, store['_id'], order['_id'], method)

    assert _order(order['_id']) == before
    assert charges.calls == []


def test_payOrder_pinsTheMethodsItNames():
    assert INVALID_PAYMENT_METHOD == 'Pick one of ' + ', '.join(
        PAYMENT_METHODS)


@pytest.mark.parametrize(
    'target', ['anotherCustomer', 'anotherStore', 'unknown', 'empty',
               'object', 'number', 'none'])
def test_payOrder_anOrderThatIsNotTheCallers_isNotFound(
        clean, firebase, clock, charges, target):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    anotherCustomer = verifiedGuestAt(firebase, store, OTHER_GUEST)
    order = _place(customerId, store, (products['Pandesal'], 1))
    caller, storeId, orderId = customerId, store['_id'], order['_id']
    if target == 'anotherCustomer':
        caller = anotherCustomer
    elif target == 'anotherStore':
        storeId = other['_id']
    else:
        orderId = {'unknown': SENTINEL, 'empty': '', 'object': {'$ne': None},
                   'number': 5, 'none': None}[target]
    before = _order(order['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_NOT_FOUND)):
        api.payOrder(caller, storeId, orderId, 'card')

    assert _order(order['_id']) == before
    assert charges.calls == []


def test_notPayable_coversEveryStatusButPending():
    assert set(NOT_PAYABLE) == set(ORDER_STATUSES) - {'pending'}
    assert 'confirmed' not in ORDER_STATUSES


@pytest.mark.parametrize('status',
                         [s for s in ORDER_STATUSES if s != 'pending'])
def test_payOrder_refusesAnOrderThatIsNotPending(
        clean, firebase, clock, charges, status):
    store, customerId, products = _shop(firebase)
    fields = {}
    if status == 'paid':
        fields = {'paymentMethod': 'card', 'paymentReference': 'DEMO-seed',
                  'paidAt': T0}
    orderId = _heldOrder(store, customerId, [(products['Pandesal'], 1)],
                         T0, status, hold=False, **fields)
    before = _order(orderId)

    with pytest.raises(ValueError, match=exactly(NOT_PAYABLE[status])):
        api.payOrder(customerId, store['_id'], orderId, 'card')

    assert _order(orderId) == before
    assert charges.calls == []


def test_payOrder_aCancelledExpiredOrder_saysItExpired(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    orderId = _heldOrder(store, customerId, [(products['Pandesal'], 1)],
                         T0 - WINDOW, 'cancelled', hold=False,
                         cancelReason='expired', cancelledAt=T0)

    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.payOrder(customerId, store['_id'], orderId, 'card')

    assert charges.calls == []


def test_payOrder_payingTwice_refusesTheSecond_andKeepsTheFirst(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    api.payOrder(customerId, store['_id'], order['_id'], 'card')
    paid = _order(order['_id'])
    clock.advance(minutes=1)

    with pytest.raises(ValueError, match=exactly(NOT_PAYABLE['paid'])):
        api.payOrder(customerId, store['_id'], order['_id'], 'maya')

    assert _order(order['_id']) == paid
    assert len(charges.calls) == 1


# ------------------------- a failed charge ------------------------- #


def test_payOrder_aFailedCharge_putsTheOrderBackToPending(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    order = _place(customerId, store, (ube, 2))
    placed, reserved = _order(order['_id']), _product(ube['_id'])
    charges.error = RuntimeError('gateway-secret-detail')
    clock.advance(minutes=1)

    with pytest.raises(ValueError) as error:
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    assert str(error.value) == PAYMENT_FAILED
    assert 'gateway-secret-detail' not in str(error.value)
    stored = _order(order['_id'])
    assert (stored['status'], stored['paymentMethod'],
            stored['paymentReference'], stored['paidAt']) == (
                'pending', None, None, None)
    # Claim and revert: two writes. Everything else, the deadline included,
    # is as placed.
    assert stored['_version'] == placed['_version'] + 2
    for key in ('items', 'total', 'currency', 'createdAt', 'expiresAt'):
        assert stored[key] == placed[key]
    assert _product(ube['_id']) == reserved


def test_payOrder_aFailedCharge_canBeRetried_orExpires_once(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    order = _place(customerId, store, (ube, 2))
    charges.error = RuntimeError('boom')
    with pytest.raises(ValueError, match=exactly(PAYMENT_FAILED)):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    charges.error = None
    paid = api.payOrder(customerId, store['_id'], order['_id'], 'card')
    assert paid['status'] == 'paid'

    second = _place(customerId, store, (ube, 1))
    charges.error = RuntimeError('boom')
    with pytest.raises(ValueError, match=exactly(PAYMENT_FAILED)):
        api.payOrder(customerId, store['_id'], second['_id'], 'card')
    # The reverted order keeps its original deadline: one sweep after it
    # cancels it exactly once, and the paid one stays.
    clock.advance(minutes=ORDER_PAYMENT_MINUTES)
    assert orderActions.cancelExpiredOrders() == 1
    assert orderActions.cancelExpiredOrders() == 0
    assert _order(second['_id'])['status'] == 'cancelled'
    assert _order(order['_id'])['status'] == 'paid'
    assert [r['orderId'] for r in _product(ube['_id'])[
        'reservedInventory']] == [order['_id']]
    assert _product(ube['_id'])['inventory'] == 8


def test_payOrder_aFailedCharge_andAFailedRevert_leavesTheOrderPaid(
        clean, firebase, clock, charges, monkeypatch):
    """The charge raises and so does the write that undoes the claim: the
    raw error propagates, the order stays paid under its reference, its
    units stay reserved and nothing is released. Intended."""
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    order = _place(customerId, store, (ube, 2))
    reserved = _product(ube['_id'])
    charges.error = RuntimeError('charge-boom')
    writes = []

    class FlakyDb:

        def __getattr__(self, name):
            return getattr(db, name)

        def findOneAndUpdate(self, *args, **kwargs):
            writes.append(args)
            if len(writes) > 1:
                raise RuntimeError('revert-boom')
            return db.findOneAndUpdate(*args, **kwargs)

    monkeypatch.setattr(paymentActions, 'db', FlakyDb())

    with pytest.raises(RuntimeError, match='revert-boom'):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    stored = _order(order['_id'])
    assert stored['status'] == 'paid'
    assert stored['paymentReference'].startswith(PAYMENT_REFERENCE_PREFIX)
    assert _product(ube['_id']) == reserved


# ----------------------------- expiry ----------------------------- #


def test_payOrder_untilTheLastSecond_pays(clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    order = _place(customerId, store, (products['Pandesal'], 1))
    clock.advance(minutes=29, seconds=59)

    res = api.payOrder(customerId, store['_id'], order['_id'], 'card')

    assert res['status'] == 'paid'


def test_payOrder_anOrderExpiringInOneMillisecond_stillPays(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    orderId = _heldOrder(store, customerId, [(products['Pandesal'], 1)],
                         T0 - WINDOW + MS)

    res = api.payOrder(customerId, store['_id'], orderId, 'card')

    assert res['status'] == 'paid'


def test_payOrder_atTheDeadline_refusesExpired_cancels_andReleasesTheUnits(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 4))
    clock.advance(minutes=ORDER_PAYMENT_MINUTES)

    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    stored = _order(order['_id'])
    assert stored['status'] == 'cancelled'
    assert stored['cancelReason'] == 'expired'
    assert stored['cancelledAt'] == T0 + WINDOW
    assert stored['_version'] == 1
    after = _product(pandesal['_id'])
    assert after['inventory'] == 10
    assert after['reservedInventory'] == []
    assert after['_version'] == pandesal['_version'] + 2
    assert charges.calls == []


def test_cancelExpiredOrders_cancelsOnlyWhatIsExpiredAndPending(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    line = [(products['Pandesal'], 1)]
    expired = _expired(store, customerId, line)
    atDeadline = _heldOrder(store, customerId, line, T0 - WINDOW)
    justInside = _heldOrder(store, customerId, line, T0 - WINDOW + MS)
    fresh = _fresh(store, customerId, line)
    paid = _paid(store, customerId, line)
    completed = _heldOrder(store, customerId, line, T0 - WINDOW,
                           'completed', hold=False)
    cancelled = _heldOrder(store, customerId, line, T0 - WINDOW,
                           'cancelled', hold=False)
    kept = [_order(o) for o in (justInside, fresh, paid, completed,
                                cancelled)]

    assert orderActions.cancelExpiredOrders() == 2

    assert [_order(o)['status'] for o in (expired, atDeadline)] == [
        'cancelled', 'cancelled']
    assert [_order(o) for o in (justInside, fresh, paid, completed,
                                cancelled)] == kept
    pandesal = _product(products['Pandesal']['_id'])
    assert sorted(r['orderId'] for r in pandesal['reservedInventory']) == (
        sorted([justInside, fresh, paid]))


def test_cancelExpiredOrders_aStoreSweep_leavesAnotherStore(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    mine = _expired(store, customerId, [(products['Pandesal'], 1)])
    theirs = _expired(other, customerId, [(latte, 1)])

    assert orderActions.cancelExpiredOrders(store['_id']) == 1

    assert _order(mine)['status'] == 'cancelled'
    assert _order(theirs)['status'] == 'pending'
    assert _product(latte['_id'])['inventory'] == 9
    assert orderActions.cancelExpiredOrders() == 1
    assert _order(theirs)['status'] == 'cancelled'


def test_cancelExpiredOrders_releasesEveryProductOnce_andOnlyItsOwnUnits(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    ube, pandesal = products['Ube Cake'], products['Pandesal']
    expired = _expired(store, customerId, [(ube, 2), (pandesal, 3)])
    stays = _fresh(store, customerId, [(ube, 4)])

    assert orderActions.cancelExpiredOrders() == 1
    assert orderActions.cancelExpiredOrders() == 0

    after = _product(ube['_id'])
    assert after['inventory'] == 10 - 4
    assert after['reservedInventory'] == [{'orderId': stays, 'quantity': 4}]
    # Seeded: one version for the reserve, one for the release.
    assert after['_version'] == 3
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert after['_version'] == 2
    assert _order(expired)['_version'] == 1


def test_cancelExpiredOrders_neverReturnsUnitsTheOrderDoesNotHold(
        clean, firebase, clock):
    """The release is filtered on the order's reservation: with none left
    (released already, say), inventory is not raised again."""
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    orderId = _expired(store, customerId, [(pandesal, 3)], hold=False)

    assert orderActions.cancelExpiredOrders() == 1

    assert _order(orderId)['status'] == 'cancelled'
    assert _product(pandesal['_id']) == pandesal


def test_cancelExpiredOrders_readsBatchesUntilNoneIsLeft(
        clean, firebase, clock, monkeypatch):
    store, customerId, products = _shop(firebase)
    monkeypatch.setattr(orderActions, 'EXPIRED_SWEEP_BATCH', 2)
    for _ in range(5):
        _expired(store, customerId, [(products['Pandesal'], 1)])

    assert orderActions.cancelExpiredOrders() == 5

    assert _product(products['Pandesal']['_id'])['inventory'] == 10


def test_cancelExpiredOrders_aFullBatchThatLostEveryRace_ends(
        clean, firebase, clock, monkeypatch):
    store, customerId, products = _shop(firebase)
    monkeypatch.setattr(orderActions, 'EXPIRED_SWEEP_BATCH', 2)
    monkeypatch.setattr(orderActions, '_cancelExpiredOrder',
                        lambda orderId, now: False)
    for _ in range(3):
        _expired(store, customerId, [(products['Pandesal'], 1)])

    assert orderActions.cancelExpiredOrders() == 0


def test_orderLists_cancelTheirOwnStoresExpiredOrders_only(
        clean, firebase, clock):
    store, customerId, products = _shop(firebase)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 150)
    mine = _expired(store, customerId, [(products['Pandesal'], 1)])
    theirs = _expired(other, customerId, [(latte, 1)])

    res = api.getMyOrders(customerId, store['_id'])

    assert {o['_id']: o['status'] for o in res['orders']} == {
        mine: 'cancelled'}
    assert _product(products['Pandesal']['_id'])['inventory'] == 10
    assert _order(theirs)['status'] == 'pending'
    assert _product(latte['_id'])['inventory'] == 9

    res = api.getStoreOrders(OTHER_ADMIN)

    assert {o['_id']: o['status'] for o in res['orders']} == {
        theirs: 'cancelled'}
    assert _product(latte['_id'])['inventory'] == 10


# ------------------------- the races ------------------------- #


def _betweenCheckAndClaim(monkeypatch, action) -> None:
    """Run `action()` inside payOrder, after its expiry check and before
    its claim (newPaymentReference runs there)."""
    real = paymentActions.newPaymentReference

    def hooked():
        action()
        return real()

    monkeypatch.setattr(paymentActions, 'newPaymentReference', hooked)


def test_payOrder_anotherPaymentWinsTheClaim_isNotChargedAndNotOverwritten(
        clean, firebase, clock, charges, monkeypatch):
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    order = _place(customerId, store, (ube, 2))
    reserved = _product(ube['_id'])

    def otherPays():
        db.db['Orders'].update_one(
            {'_id': order['_id']},
            {'$set': {'status': 'paid', 'paymentMethod': 'gcash',
                      'paymentReference': 'DEMO-other', 'paidAt': T0},
             '$inc': {'_version': 1}})

    _betweenCheckAndClaim(monkeypatch, otherPays)

    with pytest.raises(ValueError, match=exactly(NOT_PAYABLE['paid'])):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    stored = _order(order['_id'])
    assert (stored['paymentMethod'], stored['paymentReference'],
            stored['paidAt'], stored['_version']) == (
                'gcash', 'DEMO-other', T0, 1)
    assert charges.calls == []
    assert _product(ube['_id']) == reserved


def test_payOrder_anExpiryWinsTheClaim_isNotChargedAndReleasedOnce(
        clean, firebase, clock, charges, monkeypatch):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 4))
    later = T0 + WINDOW + datetime.timedelta(minutes=1)

    _betweenCheckAndClaim(
        monkeypatch,
        lambda: orderActions._cancelExpiredOrder(order['_id'], later))

    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    stored = _order(order['_id'])
    assert (stored['status'], stored['cancelReason'],
            stored['cancelledAt'], stored['_version']) == (
                'cancelled', 'expired', later, 1)
    assert charges.calls == []
    after = _product(pandesal['_id'])
    assert (after['inventory'], after['reservedInventory']) == (10, [])
    assert after['_version'] == pandesal['_version'] + 2
    clock.at = later
    assert orderActions.cancelExpiredOrders() == 0
    assert _product(pandesal['_id']) == after


def test_cancelExpiredOrders_anOrderPaidBetweenTheReadAndTheCancel_staysPaid(
        clean, firebase, clock, monkeypatch):
    store, customerId, products = _shop(firebase)
    ube = products['Ube Cake']
    orderId = _expired(store, customerId, [(ube, 2)])
    reserved = _product(ube['_id'])
    real = orderActions._cancelExpiredOrder

    def paidFirst(orderId, now):
        db.db['Orders'].update_one(
            {'_id': orderId},
            {'$set': {'status': 'paid', 'paymentMethod': 'card',
                      'paymentReference': 'DEMO-race', 'paidAt': T0},
             '$inc': {'_version': 1}})
        return real(orderId, now)

    monkeypatch.setattr(orderActions, '_cancelExpiredOrder', paidFirst)

    assert orderActions.cancelExpiredOrders() == 0

    stored = _order(orderId)
    assert (stored['status'], stored['paymentReference'],
            stored['_version']) == ('paid', 'DEMO-race', 1)
    assert 'cancelledAt' not in stored or stored['cancelledAt'] is None
    assert _product(ube['_id']) == reserved


def test_expiry_payOrderThenTheSweep_releasesOnce(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 4))
    clock.advance(minutes=ORDER_PAYMENT_MINUTES)
    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')
    released = _product(pandesal['_id'])

    assert orderActions.cancelExpiredOrders() == 0

    assert _product(pandesal['_id']) == released
    assert released['inventory'] == 10


def test_expiry_theSweepThenPayOrder_releasesOnce(
        clean, firebase, clock, charges):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    order = _place(customerId, store, (pandesal, 4))
    clock.advance(minutes=ORDER_PAYMENT_MINUTES)
    assert orderActions.cancelExpiredOrders() == 1
    released = _product(pandesal['_id'])

    with pytest.raises(ValueError, match=exactly(ORDER_EXPIRED)):
        api.payOrder(customerId, store['_id'], order['_id'], 'card')

    assert _product(pandesal['_id']) == released
    assert released['inventory'] == 10
    assert charges.calls == []


# ------------------------- the job, index, descriptions ------------------------- #


def test_job_main_returnsTheCancelledCount_andLogsNoValues(
        clean, firebase, clock, capsys):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    secret = addProduct(store['_id'], SENTINEL, 1)
    ids = [
        _expired(store, customerId, [(secret, 1)]),
        _expired(store, customerId, [(secret, 1)]),
        _fresh(store, customerId, [(secret, 1)]),
    ]
    capsys.readouterr()

    cancelled = job_cancelExpiredOrders.main()

    out = capsys.readouterr().out
    assert cancelled == 2
    assert out.strip().splitlines()[-1] == 'Cancelled 2 expired orders.'
    for value in (SENTINEL, customerId, store['_id'], *ids):
        assert value not in out
    assert job_cancelExpiredOrders.main() == 0


def test_ordersCollection_hasTheExpiryIndexes(clean):
    db.ensureIndexes()

    keys = [index['key'] for index in db.db['Orders'].index_information(
    ).values()]

    assert [('status', 1), ('expiresAt', 1)] in keys
    assert [('storeId', 1), ('status', 1), ('expiresAt', 1)] in keys


def test_theToolDescriptions_nameTheWindow_theCap_andEveryMethod():
    async def descriptions():
        tools = await mcpServer.servers['customer'].list_tools()
        return {t.name: t.description for t in tools}

    described = asyncio.run(descriptions())

    window = f'{ORDER_PAYMENT_MINUTES} minutes'
    assert window in described['createOrder']
    assert window in described['payOrder']
    assert f'{MAX_PENDING_ORDERS_PER_STORE} unpaid' in described['createOrder']
    for method in PAYMENT_METHODS:
        assert method in described['payOrder']
        assert method in described['createOrder']

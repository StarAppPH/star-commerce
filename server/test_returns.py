"""Customer returns (ReturnActions), called directly: a smoke test of every
route, then the rules: partial returns, the policy windows, who may ask or
decide, input checks, every move from every status, the refund and the
restock, and the races (made deterministic with hooks, never threads)."""

import contextlib
import datetime

import pytest

import actions.ReturnActions as returnActions
from actions.ReturnActions import (ALREADY_IN_RETURN, HANDOVER_UNKNOWN,
                                   INVALID_REASON, ITEMS_COUNT,
                                   NOT_CANCELLABLE, NOT_MOVABLE,
                                   NOT_RETURNABLE, PAST_STOCK_LIMIT,
                                   RESTOCK_NOT_BOOL, RETURN_CHANGED,
                                   RETURN_NOT_FOUND, TOO_MANY_REQUESTS)
from actions.OrderActions import ORDER_NOT_FOUND
from actions.StoreActions import NO_STORE_YET
from ApiRequests import ApiRequests
from conftest import (addOrder, addProduct, assertExactShape, createTestStore,
                      createTestUser, verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (ORDER_ITEMS_MAX, PRODUCT_INVENTORY_MAX,
                     REFUND_REFERENCE_PREFIX,
                     RETURN_REQUESTS_PER_ORDER_MAX, MyReturnListResponse,
                     MyReturnRow, NewReturnItem, StoreReturnListResponse,
                     StoreReturnRow)
from test_checkout import (ADMIN, COLLECTIONS, NOT_CUSTOMER, OTHER_ADMIN,
                           OTHER_GUEST, SENTINEL, T0, UTC, WINDOW, _heldOrder,
                           _order, _products, _shop, atItem, clock, exactly)
from test_fulfillment import NOT_ADMIN, _orderIn, refunds

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


def test_receiveReturn_aProductChangedAtTheSameMoment_isNotRestocked(
        clean, firebase, clock, monkeypatch):
    """A product that fills up between the limit check and the write is
    skipped: the return is still received and refunded, restocked
    false."""
    store, customerId, products = _shop(firebase)
    first, second = list(products.values())[:2]
    orderId = _orderIn('completed', store, customerId,
                       [(first, 1), (second, 1)])
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    returnId = _approved(store, customerId, orderId,
                         [{'item': 1, 'quantity': 1},
                          {'item': 2, 'quantity': 1}])
    real = returnActions._restockable

    def thenFilled(*args):
        restockable = real(*args)
        db.db['Products'].update_one(
            {'_id': second['_id']},
            {'$set': {'inventory': PRODUCT_INVENTORY_MAX}})
        return restockable

    monkeypatch.setattr(returnActions, '_restockable', thenFilled)

    received = api.receiveReturn(ADMIN, returnId, True)

    assertExactShape(StoreReturnRow, received)
    assert (received['status'], received['restocked']) == ('received', False)
    assert received['refundedAt'] == T0
    assert _product(first['_id'])['inventory'] == first['inventory'] + 1
    assert _product(second['_id'])['inventory'] == PRODUCT_INVENTORY_MAX


# ----------------------------- helpers ----------------------------- #

PHONE_B = '+639179876543'
PH = datetime.timezone(datetime.timedelta(hours=8))
SECOND = datetime.timedelta(seconds=1)
ONE = [{'item': 1, 'quantity': 1}]

NO_SAY_RETURNS = ("This store hasn't said whether it takes returns: ask the "
                  'store')
NO_RETURNS = "This store doesn't take returns of items that aren't defective"
NO_RETURN_WINDOW = ("This store hasn't said how long you have to return an "
                    'item: ask the store')
NO_DEFECT_WINDOW = ("This store hasn't said how long you have to report a "
                    'defective or wrong item: ask the store')


def _lateMindChanged(days: int) -> str:
    return f'The return window of {days} days after handover has passed'


def _lateDefect(days: int) -> str:
    return f'You had {days} days after handover to report this'


@contextlib.contextmanager
def _case(label):
    """Names the case of a loop in the failure that comes out of it."""
    try:
        yield
    except BaseException as error:
        if hasattr(error, 'add_note'):
            error.add_note(f'case: {label}')
        raise


def _shopWithPolicy(firebase):
    """_shop, its store reporting defects for 7 days."""
    store, customerId, products = _shop(firebase)
    api.updateMyStorePolicy(ADMIN, defectReportDays=7)
    return store, customerId, products


def _completed(store: dict, customerId: str, products: dict, quantity: int = 3,
               name: str = 'Pandesal') -> str:
    return _orderIn('completed', store, customerId,
                    [(products[name], quantity)])


def _ask(store: dict, customerId: str, orderId: str, items=ONE,
         reason: str = 'defective', note: str | None = None) -> dict:
    return api.requestReturn(customerId, store['_id'], orderId, items, reason,
                             note)


def _doc(returnId: str) -> dict:
    return db.read({'_id': returnId}, 'Returns', findOne=True)


def _count(orderId: str) -> int:
    return db.read({'orderId': orderId}, 'Returns', countOnly=True)


def _returnIn(status: str, store: dict, customerId: str,
              products: dict) -> str:
    """A return of a fresh completed order, in `status`."""
    returnId = _ask(store, customerId,
                    _completed(store, customerId, products))['_id']
    if status == 'cancelled':
        api.cancelMyReturn(customerId, store['_id'], returnId)
    elif status == 'rejected':
        api.rejectReturn(ADMIN, returnId, 'Not what we sold')
    elif status in ('approved', 'received'):
        api.approveReturn(ADMIN, returnId)
        if status == 'received':
            api.receiveReturn(ADMIN, returnId, True)
    return returnId


def _setPolicy(store: dict, answers: dict | None) -> None:
    """The store's policy as `answers` alone say; None is no document."""
    db.delete({'storeId': store['_id']}, 'Policies')
    if answers:
        # The route refuses accepted returns with no window, which a
        # stored policy can still hold (older data): written directly.
        noWindow = 'returnWindowDays' in answers and (
            answers['returnWindowDays'] is None)
        api.updateMyStorePolicy(ADMIN, **{
            **answers, **({'returnWindowDays': 1} if noWindow else {})})
        if noWindow:
            db.db['Policies'].update_one({'storeId': store['_id']},
                                         {'$set': {'returnWindowDays': None}})


def _deadline(handedOver: datetime.datetime, days: int) -> datetime.datetime:
    """The window's end, worked out from the app days (UTC+8): midnight
    after the `days`-th day that follows the handover's day."""
    day = handedOver.astimezone(PH).date() + datetime.timedelta(days=days + 1)
    return datetime.datetime.combine(day, datetime.time(),
                                     tzinfo=PH).astimezone(UTC)


# ----------------------------- the flow ----------------------------- #


def test_happyPath_requestApproveReceive_hasExactEffects(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    orderId = _completed(store, customerId, products)
    seeded = _order(orderId)

    requested = _ask(store, customerId, orderId, note=' Burnt crust ')

    assertExactShape(MyReturnRow, requested)
    assert not {'contactNumber', 'restocked', 'refundReference'
                } & set(requested)
    assert requested['items'] == [{'name': 'Pandesal',
                                   'price': pandesal['price'],
                                   'quantity': 1}]
    assert (requested['orderId'], requested['status'], requested['reason'],
            requested['note'], requested['currency']) == (
        orderId, 'requested', 'defective', 'Burnt crust', 'PHP')
    assert requested['refundAmount'] == pandesal['price']
    assert (requested['createdAt'], requested['decidedAt'],
            requested['receivedAt'], requested['refundedAt']) == (
        T0, None, None, None)
    returnId = requested['_id']
    stored = _doc(returnId)
    assert (stored['storeId'], stored['customerId'], stored['_version']) == (
        store['_id'], customerId, 0)
    assert (stored['restocked'], stored['refundReference'],
            stored['contactNumber']) == (None, None, '+639171234567')
    assert stored['items'][0]['productId'] == pandesal['_id']
    # Requesting writes the order once, to serialize an order's requests.
    assert _order(orderId) == {**seeded, '_version': seeded['_version'] + 1}

    clock.advance(hours=1)
    approved = api.approveReturn(ADMIN, returnId)
    assertExactShape(StoreReturnRow, approved)
    assert (approved['status'], approved['decidedAt']) == ('approved',
                                                           clock.at)
    assert _doc(returnId)['_version'] == 1

    clock.advance(hours=1)
    received = api.receiveReturn(ADMIN, returnId, True)

    assertExactShape(StoreReturnRow, received)
    assert received['contactNumber'] == '+639171234567'
    assert (received['status'], received['restocked']) == ('received', True)
    assert received['receivedAt'] == clock.at
    assert received['refundedAt'] == clock.at
    assert received['refundReference'].startswith(REFUND_REFERENCE_PREFIX)
    stored = _doc(returnId)
    # request 0, approve 1, the receive's flip 2, the refund's record 3.
    assert stored['_version'] == 3
    assert stored['refundReference'] == received['refundReference']
    assert stored['refundedAt'] == received['refundedAt']
    assert len(refunds.calls) == 1
    assert refunds.calls[0] == (orderId, 'DEMO-seed',
                                stored['refundReference'],
                                pandesal['price'], 'PHP')
    product = _product(pandesal['_id'])
    assert product['inventory'] == pandesal['inventory'] + 1
    assert product['_version'] == pandesal['_version'] + 1
    assert _order(orderId) == {**seeded, '_version': seeded['_version'] + 1}


def test_returnLists_showEachSideItsOwn_newestFirst(
        clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    secondId = verifiedGuestAt(firebase, store, OTHER_GUEST, PHONE_B)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    first = _ask(store, customerId,
                 _completed(store, customerId, products))['_id']
    clock.advance(minutes=1)
    second = _ask(store, customerId,
                  _completed(store, customerId, products))['_id']
    clock.advance(minutes=1)
    third = _ask(store, secondId,
                 _completed(store, secondId, products))['_id']

    mine = api.getMyReturns(customerId, store['_id'])
    assertExactShape(MyReturnListResponse, mine)
    assert [r['_id'] for r in mine['returns']] == [second, first]
    assert [r['_id'] for r in api.getMyReturns(secondId, store['_id'])[
        'returns']] == [third]
    assert api.getMyReturns(customerId, other['_id']) == {'returns': []}
    assert len(api.getMyReturns(customerId, store['_id'], 1)['returns']) == 1
    everything = api.getStoreReturns(ADMIN)
    assertExactShape(StoreReturnListResponse, everything)
    assert [r['_id'] for r in everything['returns']] == [third, second, first]
    api.approveReturn(ADMIN, first)
    assert [r['_id'] for r in api.getStoreReturns(ADMIN, 'approved')[
        'returns']] == [first]
    assert [r['_id'] for r in api.getStoreReturns(ADMIN, None, 1)[
        'returns']] == [third]
    with pytest.raises(ValueError):
        api.getStoreReturns(ADMIN, 'bogus')
    assert api.getStoreReturns(OTHER_ADMIN) == {'returns': []}


# ----------------------------- quantities ----------------------------- #


def test_requestReturn_partialReturns_countWhatIsLeft(clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    orderId = _completed(store, customerId, products)

    _ask(store, customerId, orderId, [{'item': 1, 'quantity': 2}])
    with pytest.raises(ValueError,
                       match=exactly('Order item 1: only 1 left to return')):
        _ask(store, customerId, orderId, [{'item': 1, 'quantity': 2}])
    assert _count(orderId) == 1
    _ask(store, customerId, orderId)
    with pytest.raises(ValueError,
                       match=exactly('Order item 1: only 0 left to return')):
        _ask(store, customerId, orderId)
    assert _count(orderId) == 2


def test_requestReturn_rejectedAndCancelledReturns_freeQuantityAndRefundCap(
        clean, firebase, clock, refunds):
    """3 units at 0.336 cost 1.01. A received, requested or approved return
    holds its units and its refund; a rejected or cancelled one frees
    both."""
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = {**products['Pandesal'], 'price': 0.336}
    orderId = _orderIn('completed', store, customerId, [(pandesal, 3)])
    everyUnit = [{'item': 1, 'quantity': 3}]

    whole = _ask(store, customerId, orderId, everyUnit)
    assert whole['refundAmount'] == 1.01
    api.rejectReturn(ADMIN, whole['_id'], 'No')
    whole = _ask(store, customerId, orderId, everyUnit)
    assert whole['refundAmount'] == 1.01
    api.cancelMyReturn(customerId, store['_id'], whole['_id'])

    first = _ask(store, customerId, orderId)
    api.approveReturn(ADMIN, first['_id'])
    api.receiveReturn(ADMIN, first['_id'], False)
    second = _ask(store, customerId, orderId)
    third = _ask(store, customerId, orderId)
    assert [r['refundAmount'] for r in (first, second, third)] == [
        0.34, 0.34, 0.33]
    with pytest.raises(ValueError,
                       match=exactly('Order item 1: only 0 left to return')):
        _ask(store, customerId, orderId)

    api.cancelMyReturn(customerId, store['_id'], second['_id'])
    again = _ask(store, customerId, orderId)
    assert again['refundAmount'] == 0.34
    api.rejectReturn(ADMIN, again['_id'], 'No')
    assert _ask(store, customerId, orderId)['refundAmount'] == 0.34
    assert _count(orderId) == 7


# ----------------------------- the policy ----------------------------- #


def test_requestReturn_policyWindows_byAnswerReasonAndAppDay(
        clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    # (label, answers, reason, handover, window, refusal): with a `window`
    # in days the request passes one second before the window's end and
    # fails at it, with `refusal`; with none it fails at any time.
    other = {'sendsMarketingMessages': True}
    open7 = {'acceptsReturns': True, 'returnWindowDays': 7}
    noWindow = {'acceptsReturns': True, 'returnWindowDays': None}
    both = {**open7, 'defectReportDays': 3}
    midnight = datetime.datetime(2026, 3, 10, 16, 0, 0, tzinfo=UTC)
    cases = [
        ('no document, changedMind', None, 'changedMind', T0, None,
         NO_SAY_RETURNS),
        ('no document, defective', None, 'defective', T0, None,
         NO_DEFECT_WINDOW),
        ('no document, wrongItem', None, 'wrongItem', T0, None,
         NO_DEFECT_WINDOW),
        ('no returns answers, changedMind', other, 'changedMind', T0, None,
         NO_SAY_RETURNS),
        ('no returns answers, defective', other, 'defective', T0, None,
         NO_DEFECT_WINDOW),
        ('refuses returns, changedMind', {'acceptsReturns': False},
         'changedMind', T0, None, NO_RETURNS),
        ('refuses returns, defective', {'acceptsReturns': False},
         'defective', T0, None, NO_DEFECT_WINDOW),
        ('refuses returns, defect days 3',
         {'acceptsReturns': False, 'defectReportDays': 3}, 'defective', T0, 3,
         _lateDefect(3)),
        ('accepts, no window, changedMind', noWindow, 'changedMind', T0,
         None, NO_RETURN_WINDOW),
        ('accepts, no window, wrongItem', noWindow, 'wrongItem', T0, None,
         NO_DEFECT_WINDOW),
        ('window 7, changedMind', open7, 'changedMind', T0, 7,
         _lateMindChanged(7)),
        ('window 7, defective falls back', open7, 'defective', T0, 7,
         _lateDefect(7)),
        ('window 7, wrongItem falls back', open7, 'wrongItem', T0, 7,
         _lateDefect(7)),
        ('window 7 and defect 3, defective', both, 'defective', T0, 3,
         _lateDefect(3)),
        ('window 7 and defect 3, changedMind', both, 'changedMind', T0, 7,
         _lateMindChanged(7)),
        ('defect days alone, defective', {'defectReportDays': 3},
         'defective', T0, 3, _lateDefect(3)),
        ('defect days alone, changedMind', {'defectReportDays': 3},
         'changedMind', T0, None, NO_SAY_RETURNS),
        ('handed over at 23:59:59 UTC+8', open7, 'changedMind',
         midnight - SECOND, 7, _lateMindChanged(7)),
        ('handed over at 00:00:00 UTC+8', open7, 'changedMind', midnight, 7,
         _lateMindChanged(7)),
        ('handed over at 00:00:01 UTC+8', open7, 'changedMind',
         midnight + SECOND, 7, _lateMindChanged(7)),
    ]
    # 12:00 UTC+8 on the 10th, 7 days: the window ends as the 17th does.
    assert _deadline(T0, 7) == datetime.datetime(2026, 3, 17, 16, 0, 0,
                                                 tzinfo=UTC)

    for label, answers, reason, handover, window, refusal in cases:
        with _case(label):
            _setPolicy(store, answers)
            orderId = _heldOrder(store, customerId, [(pandesal, 3)],
                                 T0 - WINDOW, 'completed', hold=False,
                                 completedAt=handover, paymentMethod='card',
                                 paymentReference='DEMO-seed',
                                 paidAt=T0 - WINDOW)
            if window is None:
                clock.at = handover + SECOND
                with pytest.raises(ValueError, match=exactly(refusal)):
                    _ask(store, customerId, orderId, reason=reason)
                assert _count(orderId) == 0
                continue
            end = _deadline(handover, window)
            clock.at = end - SECOND
            assert _ask(store, customerId, orderId,
                        reason=reason)['status'] == 'requested'
            clock.at = end
            with pytest.raises(ValueError, match=exactly(refusal)):
                _ask(store, customerId, orderId, reason=reason)
            assert _count(orderId) == 1


# ------------------------- who and which order ------------------------- #


def test_requestReturn_refusesOrdersThatAreNotCompletedOrNotTheCallers(
        clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    secondId = verifiedGuestAt(firebase, store, OTHER_GUEST, PHONE_B)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    latte = addProduct(other['_id'], 'Latte', 5)
    otherCustomer = verifiedGuestAt(firebase, other, 'ck-guest-b',
                                    '+639170000001')
    orders = {status: _orderIn(status, store, customerId, [(pandesal, 3)])
              for status in ('pending', 'paid', 'cancelled')}
    before = {status: _order(orderId) for status, orderId in orders.items()}
    stock = _products(store)

    for status, orderId in orders.items():
        with pytest.raises(ValueError,
                           match=exactly(NOT_RETURNABLE[status])):
            _ask(store, customerId, orderId)
        assert _order(orderId) == before[status]
        assert _count(orderId) == 0
    assert _products(store) == stock

    otherOrder = _orderIn('completed', other, otherCustomer, [(latte, 3)])
    hidden = {
        "another customer's order": (store, _completed(store, secondId,
                                                       products)),
        "another store's order": (store, otherOrder),
        "another store's order, with its store id": (other, otherOrder),
        'a missing order': (store, 'no-such-order'),
    }
    for hostile in ('', 123, {'$ne': None}, [SENTINEL], None):
        hidden[f'order id {hostile!r}'] = (store, hostile)
    for label, (atStore, orderId) in hidden.items():
        with _case(label):
            with pytest.raises(ValueError,
                               match=exactly(ORDER_NOT_FOUND)) as refused:
                _ask(atStore, customerId, orderId)
            assert SENTINEL not in str(refused.value)
    assert db.read({}, 'Returns', countOnly=True) == 0


def test_returnRoutes_refuseTheWrongCallers(clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    secondId = verifiedGuestAt(firebase, store, OTHER_GUEST, PHONE_B)
    other = createTestStore(OTHER_ADMIN, 'Coffee B')
    createTestUser('ck-nostore')
    returnId = _returnIn('approved', store, customerId, products)
    forAdmin = {
        'approveReturn': lambda who, r: api.approveReturn(who, r),
        'rejectReturn': lambda who, r: api.rejectReturn(who, r, 'No'),
        'receiveReturn': lambda who, r: api.receiveReturn(who, r, True),
    }
    before = _doc(returnId)
    stock = _products(store)

    # An admin of another store: the return is not found.
    for name, call in forAdmin.items():
        with _case(name):
            with pytest.raises(ValueError, match=exactly(RETURN_NOT_FOUND)):
                call(OTHER_ADMIN, returnId)
    assert api.getStoreReturns(OTHER_ADMIN) == {'returns': []}
    # A customer or an unknown user is no admin; an admin with no store.
    for name, call in forAdmin.items():
        with _case(name):
            for caller in (customerId, 'ck-nobody'):
                with pytest.raises(PermissionError,
                                   match=exactly(NOT_ADMIN)):
                    call(caller, returnId)
            with pytest.raises(ValueError, match=exactly(NO_STORE_YET)):
                call('ck-nostore', returnId)
    for caller in (customerId, 'ck-nobody'):
        with pytest.raises(PermissionError, match=exactly(NOT_ADMIN)):
            api.getStoreReturns(caller)
    with pytest.raises(ValueError, match=exactly(NO_STORE_YET)):
        api.getStoreReturns('ck-nostore')
    # Hostile return ids, on every route that takes one.
    for hostile in ('', 123, {'$ne': None}, [SENTINEL], None, 'no-such'):
        calls = [lambda: api.approveReturn(ADMIN, hostile),
                 lambda: api.rejectReturn(ADMIN, hostile, 'No'),
                 lambda: api.receiveReturn(ADMIN, hostile, True),
                 lambda: api.cancelMyReturn(customerId, store['_id'],
                                            hostile)]
        for call in calls:
            with _case(repr(hostile)):
                with pytest.raises(ValueError,
                                   match=exactly(RETURN_NOT_FOUND)) as e:
                    call()
                assert SENTINEL not in str(e.value)
    # The customer's side: only the caller's own return, at its own store.
    requested = _returnIn('requested', store, customerId, products)
    for caller, atStore in ((secondId, store['_id']),
                            (customerId, other['_id'])):
        with pytest.raises(ValueError, match=exactly(RETURN_NOT_FOUND)):
            api.cancelMyReturn(caller, atStore, requested)
    for caller in (OTHER_ADMIN, ADMIN, 'ck-nobody'):
        with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
            api.cancelMyReturn(caller, store['_id'], requested)
        with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
            api.getMyReturns(caller, store['_id'])
    with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
        _ask(store, ADMIN, 'x')

    assert _doc(returnId) == before
    assert _doc(requested)['status'] == 'requested'
    assert _products(store) == stock
    assert refunds.calls == []


# ----------------------------- input checks ----------------------------- #


def test_requestReturn_badInput_isRefusedAndWritesNothing(
        clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    orderId = _orderIn('completed', store, customerId,
                       [(products['Pandesal'], 3), (products['Ube Cake'], 1)])
    seeded = _order(orderId)
    position = ('item is a whole number from 1 to 2, its position in the '
                'order')
    quantity = 'the quantity is a whole number from 1 to'
    note = 'The note'
    already = ALREADY_IN_RETURN[0].lower() + ALREADY_IN_RETURN[1:]
    cases = [
        ([{'item': 0, 'quantity': 1}], 'defective', None,
         f'Return item 1: {position}'),
        ([{'item': 3, 'quantity': 1}], 'defective', None,
         f'Return item 1: {position}'),
        ([{'item': True, 'quantity': 1}], 'defective', None,
         f'Return item 1: {position}'),
        ([{'item': '1', 'quantity': 1}], 'defective', None,
         f'Return item 1: {position}'),
        ([{'quantity': 1}], 'defective', None, f'Return item 1: {position}'),
        ([{'item': 1}], 'defective', None, f'Return item 1: {quantity} 3'),
        ([{'item': 1, 'quantity': 0}], 'defective', None,
         f'Return item 1: {quantity} 3'),
        ([{'item': 1, 'quantity': True}], 'defective', None,
         f'Return item 1: {quantity} 3'),
        ([{'item': 1, 'quantity': 1.0}], 'defective', None,
         f'Return item 1: {quantity} 3'),
        ([{'item': 1, 'quantity': '1'}], 'defective', None,
         f'Return item 1: {quantity} 3'),
        ([{'item': 1, 'quantity': 4}], 'defective', None,
         f'Return item 1: {quantity} 3'),
        ([{'item': 2, 'quantity': 2}], 'defective', None,
         f'Return item 1: {quantity} 1'),
        (ONE + [{'item': 2, 'quantity': 2}], 'defective', None,
         f'Return item 2: {quantity} 1'),
        (ONE + ONE, 'defective', None, f'Return item 2: {already}'),
        (['x'], 'defective', None, 'Return item 1: send it as an object'),
        (ONE + [5], 'defective', None, 'Return item 2: send it as an object'),
        ([], 'defective', None, ITEMS_COUNT),
        (ONE * (ORDER_ITEMS_MAX + 1), 'defective', None, ITEMS_COUNT),
        (None, 'defective', None, ITEMS_COUNT),
        ({'item': 1, 'quantity': 1}, 'defective', None, ITEMS_COUNT),
        ('abc', 'defective', None, ITEMS_COUNT),
        (ONE, 'broken', None, INVALID_REASON),
        (ONE, '', None, INVALID_REASON),
        (ONE, None, None, INVALID_REASON),
        (ONE, 5, None, INVALID_REASON),
        (ONE, {'$ne': None}, None, INVALID_REASON),
        (ONE, 'defective', 'x' * 301, f'{note} is at most 300 characters'),
        (ONE, 'defective', 5, f'{note} must be text'),
        (ONE, 'defective', {'$ne': 1}, f'{note} must be text'),
        (ONE, 'defective', 'a\x00b',
         f'{note} cannot contain control characters'),
    ]

    for items, reason, text, refusal in cases:
        with _case((items, reason, text)):
            with pytest.raises(ValueError, match=exactly(refusal)):
                _ask(store, customerId, orderId, items, reason, text)
            assert _count(orderId) == 0
            assert _order(orderId) == seeded
    # The MCP path sends NewReturnItem models: the same checks and words.
    with pytest.raises(ValueError,
                       match=exactly(f'Return item 1: {position}')):
        _ask(store, customerId, orderId, [NewReturnItem(item=0, quantity=1)])
    assert _count(orderId) == 0
    sent = _ask(store, customerId, orderId,
                [NewReturnItem(item=2, quantity=1)])
    assert sent['items'][0]['name'] == 'Ube Cake'

    # The longest note passes; an empty or blank one is stored as None.
    longOrder = _completed(store, customerId, products, quantity=5)
    texts = {'x' * 300: 'x' * 300, ' hi ': 'hi', '': None, '  \n ': None}
    for given, stored in texts.items():
        asked = _ask(store, customerId, longOrder, ONE, 'defective', given)
        assert asked['note'] == stored
        assert _doc(asked['_id'])['note'] == stored


def test_requestReturn_theEleventhRequestOnOneOrder_isRefused(
        clean, firebase, clock):
    store, customerId, products = _shopWithPolicy(firebase)
    orderId = _completed(store, customerId, products, quantity=20)

    ids = [_ask(store, customerId, orderId)['_id']
           for _ in range(RETURN_REQUESTS_PER_ORDER_MAX)]
    # Cancelled and rejected requests count toward the limit too.
    for returnId in ids[:3]:
        api.cancelMyReturn(customerId, store['_id'], returnId)
    for returnId in ids[3:6]:
        api.rejectReturn(ADMIN, returnId, 'No')

    with pytest.raises(ValueError, match=exactly(TOO_MANY_REQUESTS)):
        _ask(store, customerId, orderId)
    assert _count(orderId) == RETURN_REQUESTS_PER_ORDER_MAX
    # Another order is not affected.
    _ask(store, customerId, _completed(store, customerId, products))


def test_rejectReturn_andReceiveReturn_badInput_changeNothing(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    requested = _returnIn('requested', store, customerId, products)
    approved = _returnIn('approved', store, customerId, products)
    seeded = {returnId: _doc(returnId) for returnId in (requested, approved)}
    reason = 'The reason'
    for given, refusal in [
            ('', f'{reason} is 1 to 300 characters'),
            ('  \n', f'{reason} is 1 to 300 characters'),
            ('x' * 301, f'{reason} is 1 to 300 characters'),
            (5, f'{reason} must be text'),
            (None, f'{reason} must be text'),
            ('a\x07b', f'{reason} cannot contain control characters')]:
        # The reason is checked before the status: an approved return gets
        # the same refusal.
        for returnId in (requested, approved):
            with _case((given, returnId)):
                with pytest.raises(ValueError, match=exactly(refusal)):
                    api.rejectReturn(ADMIN, returnId, given)
                assert _doc(returnId) == seeded[returnId]

    stock = _products(store)
    for restock in (1, 0, 'true', None, [True]):
        with pytest.raises(ValueError, match=exactly(RESTOCK_NOT_BOOL)):
            api.receiveReturn(ADMIN, approved, restock)
        assert _doc(approved) == seeded[approved]
    assert _products(store) == stock
    assert refunds.calls == []

    rejected = api.rejectReturn(ADMIN, requested, ' ' + 'x' * 300 + ' ')
    assert rejected['rejectReason'] == 'x' * 300
    assert _doc(requested)['rejectReason'] == 'x' * 300


# ----------------------------- the moves ----------------------------- #


def test_moves_setTheirTimestamps_andBumpTheVersion(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    approveId = _ask(store, customerId,
                     _completed(store, customerId, products))['_id']
    clock.advance(minutes=5)
    approved = api.approveReturn(ADMIN, approveId)
    assert (approved['status'], approved['decidedAt'],
            approved['rejectReason']) == ('approved', clock.at, None)
    rejectId = _ask(store, customerId,
                    _completed(store, customerId, products))['_id']
    clock.advance(minutes=5)
    rejected = api.rejectReturn(ADMIN, rejectId, 'Not what we sold')
    assertExactShape(StoreReturnRow, rejected)
    assert (rejected['status'], rejected['decidedAt'],
            rejected['rejectReason']) == ('rejected', clock.at,
                                          'Not what we sold')
    cancelId = _ask(store, customerId,
                    _completed(store, customerId, products))['_id']
    clock.advance(minutes=5)
    cancelled = api.cancelMyReturn(customerId, store['_id'], cancelId)
    assertExactShape(MyReturnRow, cancelled)
    assert (cancelled['status'], cancelled['cancelledAt'],
            cancelled['decidedAt']) == ('cancelled', clock.at, None)
    for returnId in (approveId, rejectId, cancelId):
        assert _doc(returnId)['_version'] == 1
    assert refunds.calls == []


def test_moves_fromEveryWrongStatus_changeNothing(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    everyDecided = ['approved', 'rejected', 'received', 'cancelled']
    notApproved = ['requested', 'rejected', 'received', 'cancelled']
    moves = {
        'approve': (lambda r: api.approveReturn(ADMIN, r), everyDecided,
                    NOT_MOVABLE),
        'reject': (lambda r: api.rejectReturn(ADMIN, r, 'No'), everyDecided,
                   NOT_MOVABLE),
        'receive': (lambda r: api.receiveReturn(ADMIN, r, True), notApproved,
                    NOT_MOVABLE),
        'receive, no restock': (lambda r: api.receiveReturn(ADMIN, r, False),
                                notApproved, NOT_MOVABLE),
        'cancel': (lambda r: api.cancelMyReturn(customerId, store['_id'], r),
                   everyDecided, NOT_CANCELLABLE),
    }

    for name, (move, statuses, messages) in moves.items():
        for status in statuses:
            with _case((name, status)):
                returnId = _returnIn(status, store, customerId, products)
                before, stock = _doc(returnId), _products(store)
                refundsBefore = len(refunds.calls)
                with pytest.raises(ValueError,
                                   match=exactly(messages[status])):
                    move(returnId)
                assert _doc(returnId) == before
                assert _products(store) == stock
                assert len(refunds.calls) == refundsBefore
    assert NOT_CANCELLABLE['approved'] == ('This return is already approved: '
                                           'ask the store')


def test_receiveReturn_twice_refundsAndRestocksOnce(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    returnId = _returnIn('approved', store, customerId, products)
    api.receiveReturn(ADMIN, returnId, True)
    received = _doc(returnId)

    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['received'])):
        api.receiveReturn(ADMIN, returnId, True)

    assert _doc(returnId) == received
    assert len(refunds.calls) == 1
    assert _product(pandesal['_id'])['inventory'] == pandesal['inventory'] + 1


def test_receiveReturn_withoutRestock_leavesStockButRefunds(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    returnId = _returnIn('approved', store, customerId, products)

    received = api.receiveReturn(ADMIN, returnId, False)

    assertExactShape(StoreReturnRow, received)
    assert (received['status'], received['restocked'],
            received['refundedAt']) == ('received', False, T0)
    assert received['refundReference'].startswith(REFUND_REFERENCE_PREFIX)
    assert _product(pandesal['_id']) == pandesal
    assert len(refunds.calls) == 1
    assert refunds.calls[0][3:] == (pandesal['price'], 'PHP')
    assert _doc(returnId)['_version'] == 3


def test_receiveReturn_aRefundThatRaises_leavesItReceivedUnrefunded(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    pandesal = products['Pandesal']
    returnId = _returnIn('approved', store, customerId, products)
    refunds.error = RuntimeError('gateway down')

    with pytest.raises(RuntimeError, match='gateway down'):
        api.receiveReturn(ADMIN, returnId, True)

    stored = _doc(returnId)
    assert (stored['status'], stored['restocked'], stored['refundedAt'],
            stored['refundReference']) == ('received', True, None, None)
    assert _product(pandesal['_id'])['inventory'] == pandesal['inventory'] + 1
    assert len(refunds.calls) == 1
    refunds.error = None
    with pytest.raises(ValueError, match=exactly(NOT_MOVABLE['received'])):
        api.receiveReturn(ADMIN, returnId, True)
    assert _doc(returnId) == stored
    assert len(refunds.calls) == 1
    assert api.getStoreReturns(ADMIN)['returns'][0]['refundedAt'] is None


def test_receiveReturn_restockPastTheLimit_changesNothingOfAnyItem(
        clean, firebase, clock, refunds):
    store, customerId, products = _shopWithPolicy(firebase)
    first, second = products['Pandesal'], products['Ensaymada']
    orderId = _orderIn('completed', store, customerId,
                       [(first, 1), (second, 1)])
    returnId = _ask(store, customerId, orderId,
                    [{'item': 2, 'quantity': 1}, {'item': 1, 'quantity': 1}]
                    )['_id']
    api.approveReturn(ADMIN, returnId)
    db.db['Products'].update_one(
        {'_id': second['_id']}, {'$set': {'inventory': PRODUCT_INVENTORY_MAX}})
    stock, before = _products(store), _doc(returnId)

    with pytest.raises(ValueError,
                       match=exactly(atItem(2, PAST_STOCK_LIMIT).replace(
                           'Item', 'Order item', 1))):
        api.receiveReturn(ADMIN, returnId, True)

    assert _products(store) == stock
    assert _doc(returnId) == before
    assert refunds.calls == []


# ----------------------------- races ----------------------------- #


@contextlib.contextmanager
def _competeFirst(monkeypatch, method: str, competitor, applies=None):
    """Run `competitor()` once, just before the first db.`method` call that
    `applies` to (any call by default), so the competing move lands first.
    The hook is undone on exit."""
    real = getattr(db, method)
    ran: list[bool] = []

    def hooked(*args, **kwargs):
        if not ran and (applies is None or applies(*args, **kwargs)):
            ran.append(True)
            competitor()
        return real(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(db, method, hooked)
        yield ran


def test_returnMoves_losingARace_getTheWinnersStatusMessage(
        clean, firebase, clock, refunds, monkeypatch):
    store, customerId, products = _shopWithPolicy(firebase)
    approve = lambda r: api.approveReturn(ADMIN, r)
    reject = lambda r: api.rejectReturn(ADMIN, r, 'No')
    cancel = lambda r: api.cancelMyReturn(customerId, store['_id'], r)
    inReturns = lambda *args, **kwargs: args[2] == 'Returns'

    for winner, loser, expected, final in [
            (reject, approve, NOT_MOVABLE['rejected'], 'rejected'),
            (approve, cancel, NOT_CANCELLABLE['approved'], 'approved'),
            (cancel, approve, NOT_MOVABLE['cancelled'], 'cancelled'),
            (approve, reject, NOT_MOVABLE['approved'], 'approved')]:
        with _case(expected):
            returnId = _returnIn('requested', store, customerId, products)
            with _competeFirst(monkeypatch, 'findOneAndUpdate',
                               lambda: winner(returnId), inReturns) as ran:
                with pytest.raises(ValueError, match=exactly(expected)):
                    loser(returnId)
            assert ran
            assert _doc(returnId)['status'] == final
            assert _doc(returnId)['_version'] == 1

    # Two receives: the loser refunds and restocks nothing.
    pandesal = _product(products['Pandesal']['_id'])
    returnId = _returnIn('approved', store, customerId, products)
    stocked = _product(pandesal['_id'])
    calls = len(refunds.calls)
    with _competeFirst(monkeypatch, 'createTransaction',
                       lambda: api.receiveReturn(ADMIN, returnId, True)):
        with pytest.raises(ValueError,
                           match=exactly(NOT_MOVABLE['received'])):
            api.receiveReturn(ADMIN, returnId, True)
    assert len(refunds.calls) == calls + 1
    assert _product(pandesal['_id'])['inventory'] == stocked['inventory'] + 1
    assert _doc(returnId)['status'] == 'received'


def test_returnMove_thatMissesWhileTheReturnIsStillRequested_saysItChanged(
        clean, firebase, clock, monkeypatch):
    store, customerId, products = _shopWithPolicy(firebase)
    returnId = _returnIn('requested', store, customerId, products)
    real = db.findOneAndUpdate
    missed: list[bool] = []

    def missesOnce(filter, update, collection, *args, **kwargs):
        if collection == 'Returns' and not missed:
            missed.append(True)
            return None
        return real(filter, update, collection, *args, **kwargs)

    before = _doc(returnId)
    with monkeypatch.context() as patched:
        patched.setattr(db, 'findOneAndUpdate', missesOnce)
        with pytest.raises(ValueError, match=exactly(RETURN_CHANGED)):
            api.approveReturn(ADMIN, returnId)

    assert missed
    assert _doc(returnId) == before
    assert api.approveReturn(ADMIN, returnId)['status'] == 'approved'

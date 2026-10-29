"""Delivery on orders: createOrder's fulfillment (pickup or delivery, as the
store's policy offers it), the delivery fee in the order's total, the
address, pin and notes a delivery keeps, orders stored before delivery
existed (read as pickups), and the chat's fulfillment form
(showFulfillmentForm and its answer).

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import re

import pytest

import chatForms
from actions.OrderActions import (CHOOSE_FULFILLMENT, DELIVERY_FOR_PICKUP,
                                  DELIVERY_NOT_AN_OBJECT, DELIVERY_REQUIRED,
                                  INVALID_FULFILLMENT, NO_DELIVERY_FEE_YET,
                                  NOT_OFFERED)
from ApiRequests import ApiRequests
from conftest import (STORE_DETAILS, addOrder, addProduct, assertExactShape,
                      storeWithProducts, verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (DELIVERY_ADDRESS_MAX_LENGTH, DELIVERY_NOTES_MAX_LENGTH,
                     ChatReplyResponse, CreatedOrderResponse,
                     MyOrderListResponse, NewOrderDelivery,
                     StoreOrderListResponse)
from test_chat import calls, clock, frozenNow, model, offline, reply
from test_chat_components import formAnswerDetails

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'OrderLocks', 'Conversations', 'ChatTurns', 'ChatUsage'
]
ADMIN = 'dl-admin'
GUEST = 'dl-guest-uid'
SENTINEL = 'ZQ-secret-7'
DELIVERS = {'deliveryOptions': ['pickup', 'delivery'], 'deliveryFee': 50,
            'freeDeliveryMinimum': 1000}
HOME = {'address': '7 Mabini St, Makati', 'latitude': 14.5547,
        'longitude': 121.0244, 'notes': 'Blue gate'}
PICKUP_FIELDS = {'fulfillment': 'pickup', 'deliveryFee': 0,
                 'deliveryAddress': None, 'deliveryLatitude': None,
                 'deliveryLongitude': None, 'deliveryNotes': None}

api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def shop(firebase, **policy):
    """A store with products, its policy set to `policy`, and a verified
    guest: (store, customerId, products by name)."""
    store = storeWithProducts(ADMIN)
    if policy:
        api.updateMyStorePolicy(ADMIN, **policy)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    products = {p['name']: p
                for p in db.read({'storeId': store['_id']}, 'Products')}
    return store, customerId, products


def items(product: dict, quantity: int = 1) -> list[dict]:
    return [{'productId': product['_id'], 'quantity': quantity}]


def stored(orderId: str) -> dict:
    return db.read({'_id': orderId}, 'Orders', findOne=True)


def deliveryOf(order: dict) -> dict:
    return {key: order[key] for key in PICKUP_FIELDS}


# ----------------------------- A. createOrder ----------------------------- #


def test_aStoreThatHasNotSaid_takesPickupOrdersOnly(clean, firebase):
    store, customerId, products = shop(firebase)

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal'], 2))

    assertExactShape(CreatedOrderResponse, res)
    assert deliveryOf(res['order']) == PICKUP_FIELDS
    assert res['order']['total'] == 10
    assert deliveryOf(stored(res['order']['_id'])) == PICKUP_FIELDS
    with pytest.raises(ValueError, match=exactly(NOT_OFFERED['delivery'])):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        'delivery', HOME)


def test_aDelivery_addsTheFeeToTheTotal_andKeepsWhereItGoes(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal'], 2), 'delivery',
                          {**HOME, 'address': '  7 Mabini St, Makati  '})

    assertExactShape(CreatedOrderResponse, res)
    expected = {'fulfillment': 'delivery', 'deliveryFee': 50,
                'deliveryAddress': HOME['address'],
                'deliveryLatitude': HOME['latitude'],
                'deliveryLongitude': HOME['longitude'],
                'deliveryNotes': HOME['notes']}
    assert deliveryOf(res['order']) == expected
    # The fee is in the total: what payOrder charges.
    assert res['order']['total'] == 2 * 5 + 50
    order = stored(res['order']['_id'])
    assert deliveryOf(order) == expected
    assert order['total'] == 60


def test_fromTheFreeDeliveryMinimum_deliveryIsFree(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    box = addProduct(store['_id'], 'Gift Box', 1000)

    under = api.createOrder(customerId, store['_id'],
                            items(products['Ube Cake']), 'delivery', HOME)
    exact = api.createOrder(customerId, store['_id'], items(box), 'delivery',
                            HOME)

    assert (under['order']['deliveryFee'], under['order']['total']) == (50,
                                                                         700)
    assert (exact['order']['deliveryFee'], exact['order']['total']) == (0,
                                                                         1000)


def test_aPickup_atAStoreThatDelivers_costsNoFee(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), 'pickup')

    assert deliveryOf(res['order']) == PICKUP_FIELDS
    assert res['order']['total'] == 5


def test_aStoreOfferingBoth_needsTheCustomersChoice(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    pandesal = products['Pandesal']

    with pytest.raises(ValueError, match=exactly(CHOOSE_FULFILLMENT)):
        api.createOrder(customerId, store['_id'], items(pandesal))
    with pytest.raises(ValueError, match=exactly(DELIVERY_REQUIRED)):
        api.createOrder(customerId, store['_id'], items(pandesal), 'delivery')
    with pytest.raises(ValueError, match=exactly(DELIVERY_FOR_PICKUP)):
        api.createOrder(customerId, store['_id'], items(pandesal), 'pickup',
                        HOME)
    # A refused order reserves nothing.
    assert db.read({'_id': pandesal['_id']}, 'Products',
                   findOne=True)['inventory'] == pandesal['inventory']
    assert db.read({}, 'Orders') == []


def test_aStoreThatOnlyDelivers_choosesDeliveryForTheCustomer(clean,
                                                              firebase):
    store, customerId, products = shop(firebase, deliveryOptions=['delivery'],
                                       deliveryFee=30)

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), delivery=HOME)

    assert res['order']['fulfillment'] == 'delivery'
    assert res['order']['total'] == 35
    with pytest.raises(ValueError, match=exactly(NOT_OFFERED['pickup'])):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        'pickup')
    with pytest.raises(ValueError, match=exactly(DELIVERY_REQUIRED)):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']))


def test_aStoreThatOnlyOffersPickup_refusesADelivery(clean, firebase):
    store, customerId, products = shop(firebase, deliveryOptions=['pickup'])

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']))

    assert deliveryOf(res['order']) == PICKUP_FIELDS
    with pytest.raises(ValueError, match=exactly(NOT_OFFERED['delivery'])):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        'delivery', HOME)


def test_aDeliveryTheStoreHasNotPriced_isRefused(clean, firebase):
    store, customerId, products = shop(
        firebase, deliveryOptions=['pickup', 'delivery'])
    pandesal = products['Pandesal']

    with pytest.raises(ValueError, match=exactly(NO_DELIVERY_FEE_YET)):
        api.createOrder(customerId, store['_id'], items(pandesal), 'delivery',
                        HOME)
    # Pickup is the one way left to book: it is chosen.
    res = api.createOrder(customerId, store['_id'], items(pandesal))
    assert res['order']['fulfillment'] == 'pickup'

    api.updateMyStorePolicy(ADMIN, deliveryOptions=['delivery'])
    for fulfillment in (None, 'delivery'):
        with pytest.raises(ValueError, match=exactly(NO_DELIVERY_FEE_YET)):
            api.createOrder(customerId, store['_id'], items(pandesal),
                            fulfillment, HOME)


@pytest.mark.parametrize('fulfillment, delivery, message', [
    ('courier', HOME, INVALID_FULFILLMENT),
    (7, HOME, INVALID_FULFILLMENT),
    ('delivery', SENTINEL, DELIVERY_NOT_AN_OBJECT),
    ('delivery', [SENTINEL], DELIVERY_NOT_AN_OBJECT),
    ('delivery', {**HOME, 'address': '   '},
     f'The delivery address is 1 to {DELIVERY_ADDRESS_MAX_LENGTH} characters'),
    ('delivery', {**HOME, 'address': SENTINEL * 40},
     f'The delivery address is 1 to {DELIVERY_ADDRESS_MAX_LENGTH} characters'),
    ('delivery', {**HOME, 'address': SENTINEL + '\x07'},
     'The delivery address cannot contain control characters'),
    ('delivery', {**HOME, 'address': 7}, 'The delivery address must be text'),
    ('delivery', {k: v for k, v in HOME.items() if k != 'address'},
     'The delivery address must be text'),
    ('delivery', {**HOME, 'latitude': 91},
     'The delivery latitude is a number from -90 to 90'),
    ('delivery', {**HOME, 'latitude': True},
     'The delivery latitude is a number from -90 to 90'),
    ('delivery', {**HOME, 'longitude': SENTINEL},
     'The delivery longitude is a number from -180 to 180'),
    ('delivery', {**HOME, 'notes': SENTINEL * 50},
     f'The delivery notes is at most {DELIVERY_NOTES_MAX_LENGTH} characters'),
])
def test_aDeliveryRefusal_namesTheField_neverTheValue(clean, firebase,
                                                      fulfillment, delivery,
                                                      message):
    store, customerId, products = shop(firebase, **DELIVERS)

    with pytest.raises(ValueError, match=exactly(message)) as error:
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        fulfillment, delivery)

    assert SENTINEL not in str(error.value)
    assert db.read({}, 'Orders') == []


def test_emptyNotes_areNoNotes_andAModelDeliveryIsTaken(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)

    blank = api.createOrder(customerId, store['_id'],
                            items(products['Pandesal']), 'delivery',
                            {**HOME, 'notes': '   '})
    # The MCP tool passes the model, HTTP a JSON object.
    modelled = api.createOrder(customerId, store['_id'],
                               items(products['Ensaymada']), 'delivery',
                               NewOrderDelivery(**{**HOME, 'notes': None}))

    assert blank['order']['deliveryNotes'] is None
    assert stored(blank['order']['_id'])['deliveryNotes'] is None
    assert modelled['order']['deliveryAddress'] == HOME['address']
    assert modelled['order']['deliveryNotes'] is None


def test_anOrderStoredBeforeDelivery_readsAsAPickup(clean, firebase):
    store, customerId, _ = shop(firebase)
    old = addOrder(store['_id'], customerId,
                   [{'productId': 'p1', 'name': 'Pandesal', 'price': 5,
                     'quantity': 2}], 'paid', paymentMethod='gcash')
    db.db['Orders'].update_one({'_id': old['_id']},
                               {'$unset': {key: '' for key in PICKUP_FIELDS}})
    assert not set(PICKUP_FIELDS) & set(stored(old['_id']))

    mine = api.getMyOrders(customerId, store['_id'])
    theirs = api.getStoreOrders(ADMIN)

    assertExactShape(MyOrderListResponse, mine)
    assertExactShape(StoreOrderListResponse, theirs)
    assert deliveryOf(mine['orders'][0]) == PICKUP_FIELDS
    assert deliveryOf(theirs['orders'][0]) == PICKUP_FIELDS


def test_payOrder_chargesTheTotalWithTheFee(clean, firebase, monkeypatch):
    import actions.PaymentActions as paymentActions
    charged = []
    monkeypatch.setattr(paymentActions, 'chargeDemoPayment',
                        lambda *args: charged.append(args))
    store, customerId, products = shop(firebase, **DELIVERS)
    order = api.createOrder(customerId, store['_id'],
                            items(products['Pandesal']), 'delivery',
                            HOME)['order']

    paid = api.payOrder(customerId, store['_id'], order['_id'], 'gcash')

    assert paid['total'] == 55 and paid['deliveryFee'] == 50
    assert paid['deliveryAddress'] == HOME['address']
    [(orderId, _, amount, currency, *_)] = charged
    assert (orderId, amount, currency) == (order['_id'], 55, 'PHP')


# ----------------------------- B. the fulfillment form ----------------------------- #


def customerSays(customerId, storeId, text, conversationId=None,
                 form=None) -> dict:
    return api.sendStoreChatMessage(customerId, storeId, text,
                                    conversationId, form)


def openForm(model, customerId: str, store: dict) -> dict:
    model(calls(('c1', 'showFulfillmentForm', '{}')),
          reply('Pickup or delivery?'))
    return customerSays(customerId, store['_id'], 'Two pandesal please')


def test_theFulfillmentForm_showsWhatTheStoreOffers(clean, firebase, model):
    store, customerId, _ = shop(firebase, **DELIVERS,
                                deliveryAreas='Makati and Taguig',
                                handoverDays=2)
    api.setUserStoreLocation(ADMIN, 14.55, 121.02)

    res = openForm(model, customerId, store)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form['kind'] == 'fulfillmentForm' and form['formId']
    assert {key: value for key, value in form.items()
            if key not in ('kind', 'formId')} == {
        'options': ['pickup', 'delivery'],
        'currency': 'PHP',
        'storeAddress': STORE_DETAILS['address'],
        'storeLocation': {'latitude': 14.55, 'longitude': 121.02},
        'deliveryAreas': 'Makati and Taguig',
        'deliveryFee': 50,
        'freeDeliveryMinimum': 1000,
        'handoverDays': 2,
    }


@pytest.mark.parametrize('policy, refusal', [
    ({}, chatForms.PICKUP_ONLY),
    ({'deliveryOptions': ['pickup', 'delivery']}, chatForms.PICKUP_ONLY),
    ({'deliveryOptions': ['delivery']}, NO_DELIVERY_FEE_YET),
])
def test_theFulfillmentForm_opensOnlyWhenThereIsAChoiceOrAnAddress(
        clean, firebase, model, policy, refusal):
    store, customerId, _ = shop(firebase, **policy)
    fake = model(calls(('c1', 'showFulfillmentForm', '{}')),
                 reply('Pickup it is.'))

    res = customerSays(customerId, store['_id'], 'Two pandesal please')

    assert res['components'] == []
    toolMessage = fake.requests[1]['messages'][-1]
    assert toolMessage['role'] == 'tool'
    assert json.loads(toolMessage['content']) == {'error': refusal}


def test_aStoreThatOnlyDelivers_opensTheFormForTheAddress(clean, firebase,
                                                         model):
    store, customerId, _ = shop(firebase, deliveryOptions=['delivery'],
                                deliveryFee=30)

    [form] = openForm(model, customerId, store)['components']

    assert form['options'] == ['delivery']
    assert form['storeLocation'] is None


def test_aDeliveryAnswer_isTheTurn_andTheModelPlacesTheOrder(clean, firebase,
                                                             model):
    store, customerId, products = shop(firebase, **DELIVERS)
    first = openForm(model, customerId, store)
    [form] = first['components']
    picked = items(products['Pandesal'], 2)
    fake = model(calls(('c2', 'createOrder', json.dumps({
        'items': picked, 'fulfillment': 'delivery', 'delivery': HOME}))),
        reply('Placed: pay below.'))

    second = customerSays(customerId, store['_id'], None,
                          first['conversationId'],
                          {'kind': 'fulfillment', 'formId': form['formId'],
                           'fulfillment': 'delivery',
                           'delivery': {**HOME, 'notes': '  Blue gate  '}})

    content = fake.requests[0]['messages'][-1]['content']
    assert second['userText'] == 'Deliver it to 7 Mabini St, Makati. Notes: Blue gate'
    assert formAnswerDetails(content) == {
        'kind': 'fulfillment', 'fulfillment': 'delivery', 'delivery': HOME}
    [checkout] = second['components']
    assert checkout['kind'] == 'checkout'
    order = checkout['order']
    assert (order['fulfillment'], order['deliveryFee'], order['total']) == (
        'delivery', 50, 60)
    assert order['deliveryAddress'] == HOME['address']
    assert (order['deliveryLatitude'], order['deliveryLongitude']) == (
        HOME['latitude'], HOME['longitude'])


def test_aPickupAnswer_carriesNoAddress(clean, firebase, model):
    store, customerId, _ = shop(firebase, **DELIVERS)
    first = openForm(model, customerId, store)
    [form] = first['components']
    fake = model(reply('Pickup it is.'))

    second = customerSays(customerId, store['_id'], None,
                          first['conversationId'],
                          {'kind': 'fulfillment', 'formId': form['formId'],
                           'fulfillment': 'pickup'})

    assert second['userText'] == 'I’ll pick it up at the store.'
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'fulfillment', 'fulfillment': 'pickup', 'delivery': None}


def test_aFulfillmentAnswer_isCheckedAgainstItsForm(clean, firebase, model):
    store, customerId, _ = shop(firebase, deliveryOptions=['delivery'],
                                deliveryFee=30)
    first = openForm(model, customerId, store)
    [form] = first['components']
    model()

    for answer, message in (
            ({'fulfillment': 'pickup'}, chatForms.NOT_ON_THE_CARD),
            ({'fulfillment': 'delivery'}, DELIVERY_REQUIRED)):
        with pytest.raises(ValueError, match=exactly(message)):
            customerSays(customerId, store['_id'], None,
                         first['conversationId'],
                         {'kind': 'fulfillment', 'formId': form['formId'],
                          **answer})
    with pytest.raises(ValueError, match='^That form answer is not valid'):
        customerSays(customerId, store['_id'], None, first['conversationId'],
                     {'kind': 'fulfillment', 'formId': form['formId'],
                      'fulfillment': 'delivery',
                      'delivery': {**HOME, 'address': 'x' * (
                          DELIVERY_ADDRESS_MAX_LENGTH + 1)}})


def test_aPickupAnswerWithAnAddress_isRefused(clean, firebase, model):
    store, customerId, _ = shop(firebase, **DELIVERS)
    first = openForm(model, customerId, store)
    [form] = first['components']
    model()

    with pytest.raises(ValueError, match=exactly(DELIVERY_FOR_PICKUP)):
        customerSays(customerId, store['_id'], None, first['conversationId'],
                     {'kind': 'fulfillment', 'formId': form['formId'],
                      'fulfillment': 'pickup', 'delivery': HOME})

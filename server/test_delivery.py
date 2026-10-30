"""Delivery on orders: createOrder's fulfillment (pickup or delivery, as the
store's policy offers it), the delivery fee (the delivery zone's under the
pin: test_delivery_zones.py tests the zones themselves) in the order's
total, the
address, pin and notes a delivery keeps, orders stored before delivery
existed (read as pickups), and the chat's delivery: pickup or delivery
asked in chat, the pin-only map card (showDeliveryPinForm and its answer),
then the address asked in chat and the order placed by the model.

No real Meta call: test_chat.py's FakeModel plays scripted completions (its
fixtures are imported here: the clock, the offline guard and the frozen now
are autouse)."""

import json
import re

import pytest

import chatForms
from actions.OrderActions import (CHOOSE_FULFILLMENT, DELIVERY_FOR_PICKUP,
                                  DELIVERY_NOT_AN_OBJECT, DELIVERY_REQUIRED,
                                  INVALID_FULFILLMENT, NO_DELIVERY_ZONES_YET,
                                  NOT_OFFERED)
from ApiRequests import ApiRequests
from conftest import (addOrder, addProduct, assertExactShape,
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
DELIVERS = {'deliveryOptions': ['pickup', 'delivery']}
# The store's pin, in Quezon City.
STORE_PIN = {'latitude': 14.6507, 'longitude': 121.03}
HOME = {'address': '7 Mabini St, Makati', 'latitude': 14.5547,
        'longitude': 121.0244, 'notes': 'Blue gate'}
HOME_FEE = 93.09
# The delivery zone around HOME that shop() draws for a store that
# delivers: its flat fee, free from 1000.
HOME_ZONE = {'name': 'Makati',
             'points': [{'latitude': 14.54, 'longitude': 121.01},
                        {'latitude': 14.54, 'longitude': 121.04},
                        {'latitude': 14.57, 'longitude': 121.04},
                        {'latitude': 14.57, 'longitude': 121.01}],
             'deliveryFee': HOME_FEE, 'deliveryDays': 1,
             'minimumOrder': None, 'freeDeliveryFrom': 1000}
PICKUP_FIELDS = {'fulfillment': 'pickup', 'deliveryFee': 0,
                 'deliveryDistanceKm': None,
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


def shop(firebase, pinned: bool = True, zones: list[dict] | None = None,
         **policy):
    """A store with products, pinned at STORE_PIN unless not `pinned`, its
    policy set to `policy`, its delivery zones `zones` (by default HOME_ZONE
    when it delivers), and a verified guest: (store, customerId, products
    by name)."""
    store = storeWithProducts(ADMIN)
    if pinned:
        api.setUserStoreLocation(ADMIN, STORE_PIN['latitude'],
                                 STORE_PIN['longitude'])
    if policy:
        api.updateMyStorePolicy(ADMIN, **policy)
    if zones is None and 'delivery' in policy.get('deliveryOptions', []):
        zones = [HOME_ZONE]
    if zones:
        api.setMyDeliveryZones(ADMIN, zones)
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
    expected = {'fulfillment': 'delivery', 'deliveryFee': HOME_FEE,
                'deliveryDistanceKm': None,
                'deliveryAddress': HOME['address'],
                'deliveryLatitude': HOME['latitude'],
                'deliveryLongitude': HOME['longitude'],
                'deliveryNotes': HOME['notes']}
    assert deliveryOf(res['order']) == expected
    # The fee is in the total: what payOrder charges.
    assert res['order']['total'] == 2 * 5 + 93.09
    order = stored(res['order']['_id'])
    assert deliveryOf(order) == expected
    assert order['total'] == 103.09
    assert res['order']['deliveryZone'] == order['deliveryZone'] == 'Makati'


def test_fromTheFreeDeliveryMinimum_deliveryIsFree(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    box = addProduct(store['_id'], 'Gift Box', 1000)

    under = api.createOrder(customerId, store['_id'],
                            items(products['Ube Cake']), 'delivery', HOME)
    exact = api.createOrder(customerId, store['_id'], items(box), 'delivery',
                            HOME)

    assert (under['order']['deliveryFee'], under['order']['total']) == (
        93.09, 743.09)
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
    store, customerId, products = shop(
        firebase, deliveryOptions=['delivery'],
        zones=[{**HOME_ZONE, 'freeDeliveryFrom': None}])

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']), delivery=HOME)

    assert res['order']['fulfillment'] == 'delivery'
    assert res['order']['total'] == 98.09
    with pytest.raises(ValueError, match=exactly(NOT_OFFERED['pickup'])):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        'pickup')
    with pytest.raises(ValueError, match=exactly(DELIVERY_REQUIRED)):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']))


def test_theChatsCreateOrder_needsNoFulfillment():
    """The model may leave fulfillment out (createOrder then picks the one
    way that can be booked), and delivery with it."""
    import chatTools
    [tool] = [t for t in chatTools.TOOLS['customer']
              if t['function']['name'] == 'createOrder']
    required = tool['function']['parameters'].get('required', [])
    assert 'fulfillment' not in required and 'delivery' not in required


def test_aStoreThatOnlyOffersPickup_refusesADelivery(clean, firebase):
    store, customerId, products = shop(firebase, deliveryOptions=['pickup'])

    res = api.createOrder(customerId, store['_id'],
                          items(products['Pandesal']))

    assert deliveryOf(res['order']) == PICKUP_FIELDS
    with pytest.raises(ValueError, match=exactly(NOT_OFFERED['delivery'])):
        api.createOrder(customerId, store['_id'], items(products['Pandesal']),
                        'delivery', HOME)


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
    ('delivery', {**HOME, 'latitude': float('nan')},
     'The delivery latitude is a number from -90 to 90'),
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

    assert paid['total'] == 98.09 and paid['deliveryFee'] == HOME_FEE
    assert paid['deliveryAddress'] == HOME['address']
    [(orderId, _, amount, currency, *_)] = charged
    assert (orderId, amount, currency) == (order['_id'], 98.09, 'PHP')


# ----------------------------- B. the delivery pin ----------------------------- #


def customerSays(customerId, storeId, text, conversationId=None,
                 form=None) -> dict:
    return api.sendStoreChatMessage(customerId, storeId, text,
                                    conversationId, form)


def openPin(model, customerId: str, store: dict) -> dict:
    model(calls(('c1', 'showDeliveryPinForm', '{}')),
          reply('Pin where it goes.'))
    return customerSays(customerId, store['_id'], 'Delivery please')


def pinOf(form: dict, place: dict = HOME) -> dict:
    return {'kind': 'deliveryPin', 'formId': form['formId'],
            'latitude': place['latitude'], 'longitude': place['longitude']}


def test_theDeliveryPinForm_isTheMapOnly(clean, firebase, model):
    store, customerId, _ = shop(firebase, **DELIVERS,
                                deliveryAreas='Makati and Taguig',
                                handoverDays=2)
    api.setUserStoreLocation(ADMIN, 14.55, 121.02)

    res = openPin(model, customerId, store)

    assertExactShape(ChatReplyResponse, res)
    [form] = res['components']
    assert form['kind'] == 'deliveryPinForm' and form['formId']
    assert form == {'kind': 'deliveryPinForm', 'formId': form['formId'],
                    'storeLocation': {'latitude': 14.55, 'longitude': 121.02},
                    'currency': 'PHP', 'deliveryZones': [HOME_ZONE]}


@pytest.mark.parametrize('policy, refusal', [
    ({}, NOT_OFFERED['delivery']),
    ({'deliveryOptions': ['pickup']}, NOT_OFFERED['delivery']),
    ({'deliveryOptions': ['pickup', 'delivery']}, NO_DELIVERY_ZONES_YET),
    ({'deliveryOptions': ['delivery']}, NO_DELIVERY_ZONES_YET),
])
def test_theDeliveryPinForm_opensOnlyWhereADeliveryCanBeBooked(
        clean, firebase, model, policy, refusal):
    # No delivery zone: a delivery is priced by the zone under its pin.
    store, customerId, _ = shop(firebase, zones=[], **policy)
    fake = model(calls(('c1', 'showDeliveryPinForm', '{}')),
                 reply('Pickup it is.'))

    res = customerSays(customerId, store['_id'], 'Deliver it please')

    assert res['components'] == []
    toolMessage = fake.requests[1]['messages'][-1]
    assert toolMessage['role'] == 'tool'
    assert json.loads(toolMessage['content']) == {'error': refusal}


def test_aStoreThatOnlyDelivers_opensThePinAtTheStore(clean, firebase,
                                                     model):
    store, customerId, _ = shop(firebase, deliveryOptions=['delivery'])

    [form] = openPin(model, customerId, store)['components']

    assert form['storeLocation'] == STORE_PIN


def test_aPin_thenTheAddressInChat_placesTheDelivery(clean, firebase, model):
    """The pin is the customer's turn; the model asks for the address in
    chat, then places the order with the typed address, the pin and the
    notes."""
    store, customerId, products = shop(firebase, **DELIVERS)
    first = openPin(model, customerId, store)
    [form] = first['components']
    fake = model(reply('House number, street and a landmark?'))

    second = customerSays(customerId, store['_id'], None,
                          first['conversationId'], pinOf(form))

    assert second['userText'] == ('Deliver to my pin at 14.55470, 121.02440 '
                                  '(Makati)')
    assert formAnswerDetails(fake.requests[0]['messages'][-1]['content']) == {
        'kind': 'deliveryPin', 'latitude': HOME['latitude'],
        'longitude': HOME['longitude'],
        'zone': {key: value for key, value in HOME_ZONE.items()
                 if key != 'points'}}
    assert second['components'] == []
    picked = items(products['Pandesal'], 2)
    model(calls(('c2', 'createOrder', json.dumps({
        'items': picked, 'fulfillment': 'delivery', 'delivery': HOME}))),
        reply('Placed: pay below.'))

    third = customerSays(customerId, store['_id'],
                         '7 Mabini St, Makati. Blue gate',
                         first['conversationId'])

    [checkout] = third['components']
    assert checkout['kind'] == 'checkout'
    order = checkout['order']
    assert (order['fulfillment'], order['deliveryFee'],
            order['deliveryZone'], order['total']) == (
        'delivery', HOME_FEE, 'Makati', 103.09)
    assert order['deliveryAddress'] == HOME['address']
    assert order['deliveryNotes'] == HOME['notes']
    assert (order['deliveryLatitude'], order['deliveryLongitude']) == (
        HOME['latitude'], HOME['longitude'])


def test_aPickupChosenInChat_isPlacedWithoutAnAddress(clean, firebase,
                                                     model):
    store, customerId, products = shop(firebase, **DELIVERS)
    model(calls(('c1', 'createOrder', json.dumps({
        'items': items(products['Pandesal']), 'fulfillment': 'pickup'}))),
        reply('Placed: pay below.'))

    res = customerSays(customerId, store['_id'], 'Pickup, yes')

    [checkout] = res['components']
    assert deliveryOf(checkout['order']) == PICKUP_FIELDS


def test_aPinAnswer_isCheckedAgainstItsForm(clean, firebase, model):
    store, customerId, _ = shop(firebase, deliveryOptions=['delivery'])
    first = openPin(model, customerId, store)
    cid = first['conversationId']
    [form] = first['components']
    model()

    with pytest.raises(ValueError, match=exactly(chatForms.FORM_DOES_NOT_MATCH)):
        customerSays(customerId, store['_id'], None, cid,
                     {'kind': 'phoneVerified', 'formId': form['formId']})
    with pytest.raises(ValueError, match=exactly(
            chatForms.FORM_ANSWER_INVALID.format(
                fields='deliveryPin.latitude'))):
        customerSays(customerId, store['_id'], None, cid,
                     {**pinOf(form), 'latitude': SENTINEL})
    with pytest.raises(ValueError, match='^That form answer is not valid'):
        customerSays(customerId, store['_id'], None, cid,
                     {'kind': 'fulfillment', 'formId': form['formId'],
                      'fulfillment': 'delivery', 'delivery': HOME})
    model(reply('Your address?'))
    customerSays(customerId, store['_id'], None, cid, pinOf(form))
    model()
    with pytest.raises(ValueError,
                       match=exactly(chatForms.FORM_ALREADY_ANSWERED)):
        customerSays(customerId, store['_id'], None, cid, pinOf(form))

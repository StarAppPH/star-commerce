"""The checkout summary: previewMyCheckout (a customer tool) prices the cart
as checkoutMyCart would place it, without placing it, and the chat shows it
as the checkoutSummary card, whose Place order answer (placeOrder) asks the
model to check out with the same fulfillment and delivery.

Covers:
  - Pricing parity: the preview's items, fee and total are the order's that
    checkoutMyCart then places with the same arguments (both priced by
    OrderActions.orderPricing), for a pickup and a delivery.
  - The zone's policy: its fee, free from its freeDeliveryFrom, its
    minimumOrder refused, a pin outside every zone refused, its
    deliveryDays (a pickup's: the store's handoverDays).
  - It refuses where checkoutMyCart would (an empty cart, no payment
    methods, an unverified phone, an item short of stock by its position,
    the unpaid orders cap) and writes nothing: no order, no held unit, the
    cart as it was. Units an expired order still holds count as free.
  - The card: always shown (showCard false is not offered), answerable
    once with placeOrder, whose details carry the card's fulfillment and
    delivery; an answer of another kind does not fit it.

No real Meta call: test_chat.py's FakeModel plays scripted completions."""

import datetime
import json
import re

import pytest

import chatCards
import chatForms
from actions.CartActions import CART_EMPTY, cartId
from actions.OrderActions import (BELOW_ZONE_MINIMUM, NO_PAYMENT_METHODS,
                                  NOT_IN_STOCK, OUTSIDE_DELIVERY_ZONES,
                                  PHONE_REQUIRED, TOO_MANY_UNPAID)
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addOrder, assertExactShape, guestAt, takePayments,
                      wipeCollections)
from mongoDb import db
from objects import (MAX_PENDING_ORDERS_PER_STORE, ChatReplyResponse,
                     CheckoutPreviewResponse)
from test_chat import calls, clock, frozenNow, model, offline, reply
from test_chat_components import formAnswerDetails, turns
from test_delivery import HOME, HOME_FEE, HOME_ZONE, shop

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'OrderLocks', 'Carts', 'Conversations', 'ChatTurns',
    'ChatUsage'
]
DELIVERS = {'deliveryOptions': ['pickup', 'delivery']}
# HOME's zone with a minimum order.
MINIMUM_ZONE = {**HOME_ZONE, 'minimumOrder': 100}
FAR = {'address': '1 Far Rd, Bulacan', 'latitude': 14.9, 'longitude': 121.2,
       'notes': None}

api = ApiRequests()
tools = ToolRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def atItem(position: int, message: str) -> str:
    return f'Item {position}: {message[:1].lower()}{message[1:]}'


def _item(product: dict, quantity: int) -> dict:
    return {'productId': product['_id'], 'quantity': quantity}


def _fill(customerId: str, store: dict, *lines: tuple[dict, int]) -> None:
    tools.addToCart(customerId, store['_id'],
                    [_item(product, quantity) for product, quantity in lines])


def _snapshot(customerId: str, store: dict) -> tuple:
    """What a preview must leave as it was: the cart, every product's stock
    and reservations, and the orders."""
    cart = db.read({'_id': cartId(customerId, store['_id'])}, 'Carts',
                   findOne=True)
    products = sorted(
        (p['_id'], p['inventory'], p.get('reservedInventory'), p['_version'])
        for p in db.read({'storeId': store['_id']}, 'Products'))
    return cart, products, db.read({}, 'Orders')


# ----------------------------- the preview ----------------------------- #


def test_aPickupPreview_pricesTheCart_andWritesNothing(clean, firebase):
    store, customerId, products = shop(firebase, handoverDays=2)
    _fill(customerId, store, (products['Pandesal'], 3),
          (products['Ube Cake'], 1))
    before = _snapshot(customerId, store)

    preview = tools.previewMyCheckout(customerId, store['_id'])

    assertExactShape(CheckoutPreviewResponse, preview)
    assert preview == {
        'currency': 'PHP',
        'items': [
            {'productId': products['Pandesal']['_id'], 'name': 'Pandesal',
             'price': 5, 'quantity': 3, 'lineTotal': 15},
            {'productId': products['Ube Cake']['_id'], 'name': 'Ube Cake',
             'price': 650, 'quantity': 1, 'lineTotal': 650}],
        'subtotal': 665, 'fulfillment': 'pickup', 'deliveryZone': None,
        'deliveryFee': 0, 'total': 665, 'readyInDays': 2,
        'deliveryAddress': None, 'deliveryLatitude': None,
        'deliveryLongitude': None, 'deliveryNotes': None}
    assert _snapshot(customerId, store) == before


@pytest.mark.parametrize('lines, fulfillment, delivery', [
    ([('Pandesal', 3), ('Ube Cake', 1)], None, None),
    ([('Pandesal', 4)], 'delivery', HOME),
    ([('Ube Cake', 2)], 'delivery', HOME),  # free from 1000
], ids=['pickup', 'delivery', 'freeDelivery'])
def test_thePreviewsTotal_isTheOrderCheckoutPlaces(clean, firebase, lines,
                                                   fulfillment, delivery):
    store, customerId, products = shop(firebase, **(
        DELIVERS if fulfillment else {}))
    _fill(customerId, store, *[(products[name], quantity)
                               for name, quantity in lines])

    preview = tools.previewMyCheckout(customerId, store['_id'], fulfillment,
                                      delivery)
    order = tools.checkoutMyCart(customerId, store['_id'], fulfillment,
                                 delivery)['order']

    assert preview['total'] == order['total']
    assert preview['deliveryFee'] == order['deliveryFee']
    assert preview['deliveryZone'] == order['deliveryZone']
    assert preview['fulfillment'] == order['fulfillment']
    assert [(i['name'], i['price'], i['quantity'])
            for i in preview['items']] == [
        (i['name'], i['price'], i['quantity']) for i in order['items']]
    assert (preview['deliveryAddress'], preview['deliveryNotes']) == (
        order['deliveryAddress'], order['deliveryNotes'])


def test_aDelivery_isPricedByTheZoneUnderThePin(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    _fill(customerId, store, (products['Pandesal'], 4))

    preview = tools.previewMyCheckout(customerId, store['_id'], 'delivery',
                                      HOME)

    assert (preview['subtotal'], preview['deliveryZone'],
            preview['deliveryFee'], preview['total'],
            preview['readyInDays']) == (20, 'Makati', HOME_FEE,
                                        round(20 + HOME_FEE, 2), 1)
    assert (preview['deliveryAddress'], preview['deliveryLatitude'],
            preview['deliveryLongitude'], preview['deliveryNotes']) == (
        HOME['address'], HOME['latitude'], HOME['longitude'], HOME['notes'])


def test_fromTheZonesFreeDeliveryTotal_theFeeIsZero(clean, firebase):
    store, customerId, products = shop(firebase, **DELIVERS)
    _fill(customerId, store, (products['Ube Cake'], 2))  # 1300 >= 1000

    preview = tools.previewMyCheckout(customerId, store['_id'], 'delivery',
                                      HOME)

    assert (preview['deliveryFee'], preview['total']) == (0, 1300)


def test_belowTheZonesMinimum_orOutsideEveryZone_isRefused(clean, firebase):
    store, customerId, products = shop(firebase, zones=[MINIMUM_ZONE],
                                       **DELIVERS)
    _fill(customerId, store, (products['Pandesal'], 2))  # 10 < 100
    before = _snapshot(customerId, store)

    with pytest.raises(ValueError, match=exactly(BELOW_ZONE_MINIMUM.format(
            zone='Makati', minimum=100, currency='PHP'))):
        tools.previewMyCheckout(customerId, store['_id'], 'delivery', HOME)
    with pytest.raises(ValueError, match=exactly(OUTSIDE_DELIVERY_ZONES)):
        tools.previewMyCheckout(customerId, store['_id'], 'delivery', FAR)
    assert _snapshot(customerId, store) == before


def test_itRefusesWhereCheckoutWould(clean, firebase):
    store, customerId, products = shop(firebase)

    with pytest.raises(ValueError, match=exactly(CART_EMPTY)):
        tools.previewMyCheckout(customerId, store['_id'])

    _fill(customerId, store, (products['Pandesal'], 2),
          (products['Ube Cake'], 3))
    db.db['Products'].update_one({'_id': products['Ube Cake']['_id']},
                                 {'$set': {'inventory': 2},
                                  '$inc': {'_version': 1}})
    with pytest.raises(ValueError, match=exactly(atItem(2, NOT_IN_STOCK))):
        tools.previewMyCheckout(customerId, store['_id'])

    unverified = guestAt(firebase, store, 'cp-other-guest')
    _fill(unverified, store, (products['Pandesal'], 1))
    with pytest.raises(PermissionError, match=exactly(PHONE_REQUIRED)):
        tools.previewMyCheckout(unverified, store['_id'])

    takePayments(store, [])
    with pytest.raises(ValueError, match=exactly(NO_PAYMENT_METHODS)):
        tools.previewMyCheckout(customerId, store['_id'])
    assert db.read({}, 'Orders') == []


def test_theUnpaidOrdersCap_isRefused(clean, firebase):
    store, customerId, products = shop(firebase)
    pandesal = products['Pandesal']
    for _ in range(MAX_PENDING_ORDERS_PER_STORE):
        addOrder(store['_id'], customerId, [
            {'productId': pandesal['_id'], 'name': 'Pandesal', 'price': 5,
             'quantity': 1}])
    _fill(customerId, store, (pandesal, 1))

    with pytest.raises(ValueError, match=exactly(TOO_MANY_UNPAID)):
        tools.previewMyCheckout(customerId, store['_id'])


def test_unitsAnExpiredOrderHolds_countAsFree(clean, firebase):
    """createOrder cancels expired orders before it reserves, so their
    units are free for the preview too; it cancels nothing itself."""
    store, customerId, products = shop(firebase)
    cake = products['Ube Cake']
    longAgo = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.timedelta(days=1)
    expired = addOrder(store['_id'], customerId, [
        {'productId': cake['_id'], 'name': 'Ube Cake', 'price': 650,
         'quantity': 8}], createdAt=longAgo)
    db.db['Products'].update_one(
        {'_id': cake['_id']},
        {'$set': {'inventory': 2, 'reservedInventory': [
            {'orderId': expired['_id'], 'quantity': 8}]}})
    db.db['Carts'].insert_one({
        '_id': cartId(customerId, store['_id']), 'customerId': customerId,
        'storeId': store['_id'], 'items': [_item(cake, 5)],
        'updatedAt': longAgo, '_version': 0})

    preview = tools.previewMyCheckout(customerId, store['_id'])

    assert preview['total'] == 5 * 650
    assert db.read({'_id': expired['_id']}, 'Orders',
                   findOne=True)['status'] == 'pending'
    assert tools.checkoutMyCart(customerId, store['_id'])['order'][
        'total'] == preview['total']


# ----------------------------- the chat card ----------------------------- #


def _previewInChat(firebase, model, arguments: dict):
    store, customerId, products = shop(firebase, **DELIVERS)
    _fill(customerId, store, (products['Pandesal'], 4))
    model(calls(('c1', 'previewMyCheckout', json.dumps(arguments))),
          reply('Your total is below: tap Place order.'))
    res = api.sendStoreChatMessage(customerId, store['_id'], 'Rider: blue '
                                   'gate')
    return store, customerId, res


def test_theChat_showsTheSummaryCard_andPlaceOrderChecksOut(clean, firebase,
                                                            model):
    store, customerId, res = _previewInChat(
        firebase, model, {'fulfillment': 'delivery', 'delivery': HOME})

    assertExactShape(ChatReplyResponse, res)
    [card] = res['components']
    assert card['kind'] == 'checkoutSummary' and card['formId']
    assert (card['total'], card['deliveryFee'], card['deliveryZone'],
            card['deliveryAddress']) == (round(20 + HOME_FEE, 2), HOME_FEE,
                                         'Makati', HOME['address'])
    assert card['items'] == [{'name': 'Pandesal', 'price': 5, 'quantity': 4,
                              'lineTotal': 20}]
    # Nothing is placed until the customer answers.
    assert db.read({}, 'Orders') == []

    fake = model(calls(('c2', 'checkoutMyCart', json.dumps(
        {'fulfillment': 'delivery', 'delivery': HOME}))),
        reply('Placed: choose how to pay below.'))
    placed = api.sendStoreChatMessage(
        customerId, store['_id'], None, res['conversationId'],
        {'kind': 'placeOrder', 'formId': card['formId']})

    saved = turns(res['conversationId'])[-1]
    assert saved['userText'].startswith('Place my order: 4 × Pandesal for ')
    assert formAnswerDetails(saved['userContent']) == {
        'kind': 'placeOrder', 'fulfillment': 'delivery',
        'delivery': {'address': HOME['address'],
                     'latitude': HOME['latitude'],
                     'longitude': HOME['longitude'],
                     'notes': HOME['notes']}}
    assert fake.requests
    [checkout] = placed['components']
    assert checkout['kind'] == 'checkout'
    assert checkout['order']['total'] == card['total']

    # Answered once only.
    with pytest.raises(ValueError, match=exactly(
            chatForms.FORM_ALREADY_ANSWERED)):
        api.sendStoreChatMessage(
            customerId, store['_id'], None, res['conversationId'],
            {'kind': 'placeOrder', 'formId': card['formId']})


def test_aPickupSummary_placesAPickup(clean, firebase, model):
    store, customerId, res = _previewInChat(firebase, model,
                                            {'fulfillment': 'pickup'})
    [card] = res['components']
    assert card['fulfillment'] == 'pickup' and card['deliveryFee'] == 0

    model(reply('Placing it.'))
    api.sendStoreChatMessage(customerId, store['_id'], None,
                             res['conversationId'],
                             {'kind': 'placeOrder',
                              'formId': card['formId']})

    saved = turns(res['conversationId'])[-1]
    assert formAnswerDetails(saved['userContent']) == {
        'kind': 'placeOrder', 'fulfillment': 'pickup', 'delivery': None}
    assert saved['userText'].endswith(', pickup')


def test_anotherAnswerKind_doesNotFitTheSummary(clean, firebase, model):
    store, customerId, res = _previewInChat(
        firebase, model, {'fulfillment': 'delivery', 'delivery': HOME})
    [card] = res['components']

    with pytest.raises(ValueError, match=exactly(
            chatForms.FORM_DOES_NOT_MATCH)):
        api.sendStoreChatMessage(
            customerId, store['_id'], None, res['conversationId'],
            {'kind': 'payment', 'formId': card['formId'],
             'paymentMethod': 'cash', 'paymentProvider': None})


def test_theSummaryCard_isAlwaysShown_andItsToolTakesNoShowCard():
    assert 'previewMyCheckout' in chatCards.CARDS
    assert 'previewMyCheckout' not in chatCards.SHOW_CARD_TOOLS
    assert chatForms.ANSWERS_TO['placeOrder'] == 'checkoutSummary'
    assert 'checkoutSummary' in chatForms.COMPONENT_KINDS


def test_thePrompt_previewsBeforeCheckingOut():
    import chatModel
    prompt = chatModel.CHAT_PROMPTS['customer']
    assert prompt.index('previewMyCheckout') < prompt.index(
        'check out with checkoutMyCart')
    assert 'never ask "confirm checkout?"' in prompt


def test_thePrompt_offersCheckout_afterTheCardAddsToTheCart():
    """The product card tells the chat what it added (ShopProductsCard's
    "I added ... to my cart."): the cart is saved, so the model asks about
    checking out instead of adding it again."""
    import chatModel
    prompt = chatModel.CHAT_PROMPTS['customer']
    assert 'When the customer says they added products to their cart' in prompt
    assert 'call no cart tool' in prompt
    assert 'asking whether they would like to check out' in prompt
    assert 'asking whether they would like to check out' not in (
        chatModel.CHAT_PROMPTS['admin'])

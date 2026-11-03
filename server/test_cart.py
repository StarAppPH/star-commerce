"""The customer's cart: getMyCart, addToCart, setCartItemQuantity,
removeFromCart, clearMyCart (each a route and its twin tool) and
checkoutMyCart (a tool), called directly, plus the chat path that adds to
the cart and checks it out.

Covers:
  - A cart is one Carts document per customer and store ("<uid>:<storeId>"),
    keeping product ids and quantities only: every answer reads the
    product's current name, price and stock, and leaves out a product the
    store hid or deleted (the next write drops it).
  - Adding holds no stock: the cart may hold at most the units left, an
    add on top of what is in the cart is checked as the sum, a refusal
    names the item's position and adds nothing; a cart holds at most
    CART_ITEMS_MAX products.
  - Only a customer reaches a cart, and only their own: an admin is
    refused, and another customer's cart at the same store is untouched.
  - checkoutMyCart places one order of the cart (createOrder's rules: the
    phone, the payment methods, the stock at checkout) and takes the
    ordered items out of the cart; a refused checkout leaves the cart as it
    was.
  - Store reset deletes the store's carts (test_store_reset.py).
"""

import json
import re

import pytest

from actions.CartActions import (ADD_ITEMS_COUNT, ADD_QUANTITY_RANGE,
                                 ALREADY_IN_REQUEST, CART_EMPTY, CART_FULL,
                                 NOT_IN_CART, NOT_SOLD, ONLY_LEFT,
                                 SET_QUANTITY_RANGE, SOLD_OUT, cartId)
from actions.OrderActions import NOT_IN_STOCK, PHONE_REQUIRED
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (addProduct, assertExactShape,
                      guestAt, storeWithProducts, verifiedGuestAt,
                      wipeCollections)
from mongoDb import db
from objects import (CART_ITEMS_MAX, CartResponse, CartScreenResponse,
                     ChatReplyResponse, CreatedOrderResponse)
from test_chat import calls, clock, frozenNow, model, offline, reply

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Customers', 'Products', 'Orders',
    'OrderLocks', 'Policies', 'Carts', 'Conversations', 'ChatTurns',
    'ChatUsage'
]
ADMIN = 'ct-admin'
GUEST = 'ct-guest-uid'
OTHER_GUEST = 'ct-other-guest-uid'
NOT_CUSTOMER = 'Only a customer can do this'

api = ApiRequests()
tools = ToolRequests()


def exactly(message: str) -> str:
    return '^' + re.escape(message) + '$'


def atItem(position: int, message: str) -> str:
    return f'Item {position}: {message[:1].lower()}{message[1:]}'


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


def _products(store: dict) -> dict:
    return {p['name']: p
            for p in db.read({'storeId': store['_id']}, 'Products')}


def _shop(firebase):
    """A store with its products and a verified customer: (store,
    customerId, products by name)."""
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    return store, customerId, _products(store)


def _item(product: dict, quantity: int) -> dict:
    return {'productId': product['_id'], 'quantity': quantity}


def _stored(customerId: str, storeId: str) -> dict | None:
    return db.read({'_id': cartId(customerId, storeId)}, 'Carts',
                   findOne=True)


def _quantities(cart: dict) -> dict:
    return {row['name']: row['quantity'] for row in cart['items']}


# ----------------------------- reading ----------------------------- #


def test_aNewCustomersCart_isEmpty_andStoresNothing(clean, firebase):
    store, customerId, _ = _shop(firebase)

    cart = api.getMyCart(customerId, store['_id'])

    assertExactShape(CartScreenResponse, cart)
    assert cart == {'currency': 'PHP', 'items': [], 'subtotal': 0}
    assert _stored(customerId, store['_id']) is None


def test_addToCart_showsEachProductAsItIsNow(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']

    cart = api.addToCart(customerId, store['_id'],
                         [_item(pandesal, 3), _item(cake, 1)])

    assertExactShape(CartScreenResponse, cart)
    assert cart['items'] == [
        {'productId': pandesal['_id'], 'name': 'Pandesal', 'price': 5,
         'quantity': 3, 'inventory': 10, 'hasInventory': True,
         'photoUrl': None},
        {'productId': cake['_id'], 'name': 'Ube Cake', 'price': 650,
         'quantity': 1, 'inventory': 10, 'hasInventory': True,
         'photoUrl': None},
    ]
    assert cart['subtotal'] == 665
    # The cart keeps ids and quantities only; stock is not held.
    stored = _stored(customerId, store['_id'])
    assert stored['items'] == [_item(pandesal, 3), _item(cake, 1)]
    assert stored['customerId'] == customerId
    assert stored['storeId'] == store['_id']
    assert db.read({'_id': pandesal['_id']}, 'Products',
                   findOne=True)['inventory'] == 10

    # A later price change shows in the cart; the cart is not rewritten.
    db.db['Products'].update_one({'_id': cake['_id']},
                                 {'$set': {'price': 700}})
    again = api.getMyCart(customerId, store['_id'])
    assert again['subtotal'] == 715
    assert again['items'][1]['price'] == 700


def test_theToolTwin_answersTheAiItsCart(clean, firebase):
    store, customerId, products = _shop(firebase)

    cart = tools.addToCart(customerId, store['_id'],
                           [_item(products['Ensaymada'], 2)])

    assertExactShape(CartResponse, cart)
    assert set(cart['items'][0]) == {'productId', 'name', 'price',
                                     'quantity', 'inventory', 'hasInventory'}
    assert tools.getMyCart(customerId, store['_id']) == cart


def test_addToCart_addsOnTopOfWhatTheCartHas(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    tools.addToCart(customerId, store['_id'], [_item(pandesal, 4)])

    cart = tools.addToCart(customerId, store['_id'], [_item(pandesal, 5)])

    assert _quantities(cart) == {'Pandesal': 9}
    with pytest.raises(ValueError,
                       match=exactly(atItem(1, ONLY_LEFT.format(
                           left=10, wanted=11)))):
        tools.addToCart(customerId, store['_id'], [_item(pandesal, 2)])
    assert _quantities(tools.getMyCart(customerId, store['_id'])) == {
        'Pandesal': 9}


def test_aHiddenOrDeletedProduct_leavesTheCart(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake, ensaymada = (products['Pandesal'], products['Ube Cake'],
                                 products['Ensaymada'])
    tools.addToCart(customerId, store['_id'],
                    [_item(pandesal, 1), _item(cake, 1), _item(ensaymada, 1)])
    db.db['Products'].update_one({'_id': cake['_id']},
                                 {'$set': {'isAvailable': False}})
    db.db['Products'].delete_one({'_id': ensaymada['_id']})

    cart = api.getMyCart(customerId, store['_id'])
    assert _quantities(cart) == {'Pandesal': 1}
    assert cart['subtotal'] == 5

    # The next write drops them from the stored cart.
    tools.setCartItemQuantity(customerId, store['_id'], pandesal['_id'], 2)
    assert _stored(customerId, store['_id'])['items'] == [_item(pandesal, 2)]


# ----------------------------- adding: refusals ----------------------------- #


def test_addToCart_refusesWhatTheStoreCannotSell(clean, firebase):
    store, customerId, products = _shop(firebase)
    otherStore = storeWithProducts('ct-other-admin', 'Cafe B')
    otherProduct = _products(otherStore)['Pandesal']
    soldOut = addProduct(store['_id'], 'Kalamay', 30, inventory=0)
    pandesal = products['Pandesal']
    cases = [
        ([_item(products['Old Recipe'], 1)], atItem(1, NOT_SOLD)),
        ([_item(otherProduct, 1)], atItem(1, NOT_SOLD)),
        ([{'productId': 'no-such-product', 'quantity': 1}],
         atItem(1, NOT_SOLD)),
        ([_item(pandesal, 1), _item(soldOut, 1)], atItem(2, SOLD_OUT)),
        ([_item(pandesal, 11)],
         atItem(1, ONLY_LEFT.format(left=10, wanted=11))),
        ([_item(pandesal, 0)], atItem(1, ADD_QUANTITY_RANGE)),
        ([{'productId': pandesal['_id'], 'quantity': True}],
         atItem(1, ADD_QUANTITY_RANGE)),
        ([_item(pandesal, 1), _item(pandesal, 2)],
         atItem(2, ALREADY_IN_REQUEST)),
        ([], ADD_ITEMS_COUNT),
    ]
    for items, message in cases:
        with pytest.raises(ValueError, match=exactly(message)):
            api.addToCart(customerId, store['_id'], items)
    # A refused add adds nothing, not even its valid items before.
    assert _stored(customerId, store['_id']) is None


def test_aCart_holdsAtMostCartItemsMaxProducts(clean, firebase):
    store, customerId, _ = _shop(firebase)
    many = [addProduct(store['_id'], f'Bun {n}', 1)
            for n in range(CART_ITEMS_MAX + 1)]
    tools.addToCart(customerId, store['_id'],
                    [_item(product, 1) for product in many[:CART_ITEMS_MAX]])

    with pytest.raises(ValueError, match=exactly(CART_FULL)):
        tools.addToCart(customerId, store['_id'], [_item(many[-1], 1)])
    with pytest.raises(ValueError, match=exactly(CART_FULL)):
        tools.setCartItemQuantity(customerId, store['_id'], many[-1]['_id'],
                                  1)
    # A product already in it can still change.
    cart = tools.addToCart(customerId, store['_id'], [_item(many[0], 1)])
    assert len(cart['items']) == CART_ITEMS_MAX


# ----------------------------- changing ----------------------------- #


def test_setCartItemQuantity_setsIt_andZeroRemovesIt(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']
    api.addToCart(customerId, store['_id'],
                  [_item(pandesal, 3), _item(cake, 1)])

    cart = api.setCartItemQuantity(customerId, store['_id'], pandesal['_id'],
                                   7)
    assertExactShape(CartScreenResponse, cart)
    assert _quantities(cart) == {'Pandesal': 7, 'Ube Cake': 1}

    cart = api.setCartItemQuantity(customerId, store['_id'], cake['_id'], 0)
    assert _quantities(cart) == {'Pandesal': 7}

    # A product not in the cart yet is added.
    cart = api.setCartItemQuantity(customerId, store['_id'], cake['_id'], 2)
    assert _quantities(cart) == {'Pandesal': 7, 'Ube Cake': 2}


def test_setCartItemQuantity_refusals(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    api.addToCart(customerId, store['_id'], [_item(pandesal, 1)])
    for productId, quantity, message in (
            (pandesal['_id'], 11, ONLY_LEFT.format(left=10, wanted=11)),
            (pandesal['_id'], -1, SET_QUANTITY_RANGE),
            (pandesal['_id'], 1.5, SET_QUANTITY_RANGE),
            (products['Ube Cake']['_id'], 0, NOT_IN_CART),
            (products['Old Recipe']['_id'], 1, NOT_SOLD)):
        with pytest.raises(ValueError, match=exactly(message)):
            api.setCartItemQuantity(customerId, store['_id'], productId,
                                    quantity)
    assert _quantities(api.getMyCart(customerId, store['_id'])) == {
        'Pandesal': 1}


def test_removeFromCart_andClearMyCart(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']
    api.addToCart(customerId, store['_id'],
                  [_item(pandesal, 3), _item(cake, 1)])

    cart = api.removeFromCart(customerId, store['_id'], cake['_id'])
    assertExactShape(CartScreenResponse, cart)
    assert _quantities(cart) == {'Pandesal': 3}
    with pytest.raises(ValueError, match=exactly(NOT_IN_CART)):
        api.removeFromCart(customerId, store['_id'], cake['_id'])

    cart = api.clearMyCart(customerId, store['_id'])
    assertExactShape(CartScreenResponse, cart)
    assert cart == {'currency': 'PHP', 'items': [], 'subtotal': 0}
    assert _stored(customerId, store['_id'])['items'] == []


def test_aLostRace_isRetriedOnTheNewerCart(clean, firebase, monkeypatch):
    """Another write between this write's read and its save: the save's
    _version check fails, and the write is redone on what the other wrote."""
    import actions.CartActions as cartActions
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']
    tools.addToCart(customerId, store['_id'], [_item(pandesal, 1)])
    realSave = cartActions._save
    raced = []

    def racingSave(userId, storeId, cart, lines):
        if not raced:
            raced.append(True)
            realSave(userId, storeId, cart,
                     [*cart['items'], _item(cake, 2)])
        return realSave(userId, storeId, cart, lines)

    monkeypatch.setattr(cartActions, '_save', racingSave)
    cart = tools.setCartItemQuantity(customerId, store['_id'],
                                     pandesal['_id'], 4)

    assert _quantities(cart) == {'Pandesal': 4, 'Ube Cake': 2}


# ----------------------------- who ----------------------------- #


def test_onlyACustomer_reachesACart_andOnlyTheirOwn(clean, firebase):
    store, customerId, products = _shop(firebase)
    other = guestAt(firebase, store, OTHER_GUEST)
    pandesal = products['Pandesal']
    api.addToCart(customerId, store['_id'], [_item(pandesal, 2)])

    assert api.getMyCart(other, store['_id'])['items'] == []
    api.addToCart(other, store['_id'], [_item(pandesal, 5)])
    assert _quantities(api.getMyCart(customerId, store['_id'])) == {
        'Pandesal': 2}

    for call in (lambda: api.getMyCart(ADMIN, store['_id']),
                 lambda: api.addToCart(ADMIN, store['_id'],
                                       [_item(pandesal, 1)]),
                 lambda: api.clearMyCart(ADMIN, store['_id']),
                 lambda: tools.checkoutMyCart(ADMIN, store['_id'])):
        with pytest.raises(PermissionError, match=exactly(NOT_CUSTOMER)):
            call()


def test_aCartAtOneStore_isNotTheCartAtAnother(clean, firebase):
    store, customerId, products = _shop(firebase)
    otherStore = storeWithProducts('ct-other-admin', 'Cafe B')
    api.addToCart(customerId, store['_id'], [_item(products['Pandesal'], 2)])

    assert api.getMyCart(customerId, otherStore['_id']) == {
        'currency': 'PHP', 'items': [], 'subtotal': 0}


def test_aStoreNotSetUp_hasNoCart(clean, firebase):
    store, customerId, _ = _shop(firebase)
    with pytest.raises(ValueError):
        api.getMyCart(customerId, 'no-such-store')


# ----------------------------- checkout ----------------------------- #


def test_checkoutMyCart_ordersTheCart_andEmptiesIt(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']
    tools.addToCart(customerId, store['_id'],
                    [_item(pandesal, 3), _item(cake, 1)])

    created = tools.checkoutMyCart(customerId, store['_id'])

    assertExactShape(CreatedOrderResponse, created)
    order = created['order']
    assert order['status'] == 'pending'
    assert order['total'] == 665
    assert [(item['name'], item['quantity']) for item in order['items']] == [
        ('Pandesal', 3), ('Ube Cake', 1)]
    assert created['paymentOptions']
    # The order reserved the units; the cart is empty.
    assert db.read({'_id': pandesal['_id']}, 'Products',
                   findOne=True)['inventory'] == 7
    assert api.getMyCart(customerId, store['_id'])['items'] == []


def test_checkoutMyCart_leavesOutWhatTheStoreStoppedSelling(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']
    tools.addToCart(customerId, store['_id'],
                    [_item(pandesal, 1), _item(cake, 1)])
    db.db['Products'].update_one({'_id': cake['_id']},
                                 {'$set': {'isAvailable': False}})

    order = tools.checkoutMyCart(customerId, store['_id'])['order']

    assert [item['name'] for item in order['items']] == ['Pandesal']


def test_checkoutMyCart_refusals_leaveTheCartAsItWas(clean, firebase):
    store, customerId, products = _shop(firebase)
    pandesal, cake = products['Pandesal'], products['Ube Cake']

    with pytest.raises(ValueError, match=exactly(CART_EMPTY)):
        tools.checkoutMyCart(customerId, store['_id'])

    tools.addToCart(customerId, store['_id'],
                    [_item(pandesal, 2), _item(cake, 3)])
    # Stock sold elsewhere since: checkout names the cart's item.
    db.db['Products'].update_one({'_id': cake['_id']},
                                 {'$set': {'inventory': 2},
                                  '$inc': {'_version': 1}})
    with pytest.raises(ValueError, match=exactly(atItem(2, NOT_IN_STOCK))):
        tools.checkoutMyCart(customerId, store['_id'])
    assert _quantities(tools.getMyCart(customerId, store['_id'])) == {
        'Pandesal': 2, 'Ube Cake': 3}

    # An unverified customer is asked to verify first.
    unverified = guestAt(firebase, store, OTHER_GUEST)
    tools.addToCart(unverified, store['_id'], [_item(pandesal, 1)])
    with pytest.raises(PermissionError, match=exactly(PHONE_REQUIRED)):
        tools.checkoutMyCart(unverified, store['_id'])
    assert _quantities(tools.getMyCart(unverified, store['_id'])) == {
        'Pandesal': 1}


# ----------------------------- the chat ----------------------------- #


def test_theChat_addsToTheCart_andChecksItOut(clean, firebase, model):
    store, customerId, products = _shop(firebase)
    pandesal = products['Pandesal']
    model(calls(('c1', 'addToCart', json.dumps(
        {'items': [_item(pandesal, 2)]}))),
          reply('Added 2 Pandesal to your cart.'))
    first = api.sendStoreChatMessage(customerId, store['_id'],
                                     'Add 2 pandesal to my cart')
    assertExactShape(ChatReplyResponse, first)
    assert _quantities(api.getMyCart(customerId, store['_id'])) == {
        'Pandesal': 2}

    model(calls(('c2', 'checkoutMyCart', '{}')),
          reply('Your order is placed: choose how to pay below.'))
    second = api.sendStoreChatMessage(customerId, store['_id'],
                                      'Check out my cart', first[
                                          'conversationId'])

    [checkout] = second['components']
    assert checkout['kind'] == 'checkout' and checkout['formId']
    assert checkout['order']['total'] == 10
    assert api.getMyCart(customerId, store['_id'])['items'] == []

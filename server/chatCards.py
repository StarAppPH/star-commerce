"""chatCards.py — the cards under an assistant's reply (hand-written).

A card shows what a tool returned in this turn, built from the tool's own
full result (held for the turn, while its tool message carries only the AI
copy, chatModel.aiContent), never from the model's words: a price on a card
is the price the route sent. The cards are made once the
turn's tools have run (build), in the order the tools ran:

- a list (products, orders, returns) is a card of its own;
- one product, order or return a later tool changed (completeOrder after
  getStoreOrders, say) updates its row on the turn's card that lists it, so
  a card never shows a row as it was before the turn changed it; one that no
  card lists gets a card of its own;
- a form a form tool opened (chatForms.py) takes its place among them.

A card that takes a choice gets a formId (chatForms.ANSWERS_TO): products to
order, a checkout summary to place, an order to pay, paid orders to hand over, requested returns to
decide, colors to pick the brand colors from. A tool that refused (an {"error": ...} result) shows nothing, and so
does a result that does not fit its card: it is logged, and the turn goes on.
At most CHAT_COMPONENTS_PER_TURN_MAX components are kept, the latest.

The model decides whether a card is worth showing: in the chat (never on
the MCP servers), each tool of SHOW_CARD_TOOLS takes a `showCard` argument
(chatParameters adds it to the tool's schema; the tool's own args model
ignores it, so the tool never sees it). `showCard: false` drops the card the
call would add, yet a row it changed still updates the turn's card that
lists it, so a card on screen never shows a row as it was. Left out, the
card shows. A card that is the only way to finish an action (the order to
place or pay, a new product's photo choices, the brand colors to pick)
always shows:
its tools take no `showCard` (ALWAYS_SHOWN).
"""

import json
import logging
from typing import Callable

from pydantic import TypeAdapter, ValidationError

from actions.StoreActions import STORES
from chatForms import newFormId
from mongoDb import db
from objects import (CHAT_CARD_ROWS_MAX, CHAT_COMPONENTS_PER_TURN_MAX,
                     POLICY_ANSWER_FIELDS, ChatComponent)

_COMPONENT = TypeAdapter(ChatComponent)


class CardContext:
    """What a card needs besides its tool's result: the store's currency,
    read once, only when a card needs it."""

    def __init__(self, storeId: str):
        self.storeId = storeId
        self._currency = None

    def currency(self) -> str:
        if self._currency is None:
            store = db.read({'_id': self.storeId}, STORES,
                            projection={'currency': 1}, findOne=True)
            self._currency = store['currency']
        return self._currency


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row.get(key) for key in keys}


_SHOP_PRODUCT = ('_id', 'name', 'description', 'price', 'inventory',
                 'photoUrl')
_STORE_PRODUCT = ('_id', 'name', 'price', 'inventory', 'reservedQuantity',
                  'isAvailable', 'soldCount', 'photoUrl')
_ORDER_ITEM = ('name', 'price', 'quantity')
_ORDER_DELIVERY = ('fulfillment', 'deliveryFee', 'deliveryDistanceKm',
                   'deliveryZone',
                   'deliveryAddress',
                   'deliveryLatitude', 'deliveryLongitude', 'deliveryNotes')
_STORE_ORDER = ('_id', 'status', 'total', 'currency', 'contactNumber',
                'createdAt', 'paymentMethod', 'paidAt', 'completedAt',
                'cancelReason', 'manualPaymentReference', 'paymentReceiptAt',
                *_ORDER_DELIVERY)
_MY_ORDER = ('_id', 'status', 'total', 'currency', 'createdAt', 'expiresAt',
             'paymentMethod', 'paidAt', 'completedAt', 'cancelReason',
             'refundedAt', 'paymentReceiptAt', *_ORDER_DELIVERY)
_STORE_RETURN = ('_id', 'orderId', 'status', 'reason', 'note',
                 'refundAmount', 'currency', 'createdAt', 'contactNumber',
                 'rejectReason', 'restocked')
_MY_RETURN = ('_id', 'status', 'reason', 'refundAmount', 'currency',
              'createdAt', 'rejectReason', 'refundedAt')
_STORE = ('name', 'slug', 'currency', 'description', 'contactNumber',
          'address', 'openingHours')


def _withItems(row: dict, keys: tuple[str, ...]) -> dict:
    return {**_pick(row, keys),
            'items': [_pick(item, _ORDER_ITEM) for item in row['items']]}


def _storeProductRow(row: dict) -> dict:
    """An admin's product row; a single product (AdminProductRow) lists
    its reservations, a list row (AdminProductListRow) their sum."""
    if 'reservedQuantity' not in row:
        row = {**row, 'reservedQuantity': sum(
            held['quantity'] for held in row.get('reservedInventory', []))}
    return _pick(row, _STORE_PRODUCT)


def _withFormIf(card: dict, rowsKey: str, status: str) -> dict:
    """`card` answerable (a formId) while a row of it has `status`."""
    answerable = any(row['status'] == status for row in card[rowsKey])
    return {**card, 'formId': (card.get('formId') or newFormId())
            if answerable else None}


# ── One card per tool result ────────────────

def _shopProducts(result: dict, context: CardContext) -> dict:
    return {'kind': 'shopProducts', 'formId': newFormId(),
            'currency': result['currency'],
            'products': [_pick(p, _SHOP_PRODUCT)
                         for p in result['products']]}


def _storeProducts(result: dict, context: CardContext) -> dict:
    return {'kind': 'storeProducts', 'currency': result['currency'],
            'products': [_storeProductRow(p) for p in result['products']]}


def _storeProduct(result: dict, context: CardContext) -> dict:
    return {'kind': 'storeProducts', 'currency': context.currency(),
            'products': [_storeProductRow(result)]}


def _storeOrders(result: dict, context: CardContext) -> dict:
    return _withFormIf({'kind': 'storeOrders', 'orders': [
        _withItems(o, _STORE_ORDER) for o in result['orders']]},
        'orders', 'paid')


def _storeOrder(result: dict, context: CardContext) -> dict:
    return _withFormIf({'kind': 'storeOrders',
                        'orders': [_withItems(result, _STORE_ORDER)]},
                       'orders', 'paid')


def _myOrders(result: dict, context: CardContext) -> dict:
    return {'kind': 'myOrders',
            'orders': [_withItems(o, _MY_ORDER) for o in result['orders']]}


def _myOrder(result: dict, context: CardContext) -> dict:
    return {'kind': 'myOrders', 'orders': [_withItems(result, _MY_ORDER)]}


def _checkout(result: dict, context: CardContext) -> dict:
    return {'kind': 'checkout', 'formId': newFormId(),
            'order': _withItems(result['order'], _MY_ORDER),
            'paymentOptions': result['paymentOptions']}


_CHECKOUT_LINE = ('name', 'price', 'quantity', 'lineTotal')
_CHECKOUT_SUMMARY = ('currency', 'subtotal', 'fulfillment', 'deliveryZone',
                     'deliveryFee', 'total', 'readyInDays', 'deliveryAddress',
                     'deliveryLatitude', 'deliveryLongitude', 'deliveryNotes')


def _checkoutSummary(result: dict, context: CardContext) -> dict:
    """What checking out the cart would place, before it is placed
    (answered with PlaceOrderAnswer)."""
    return {'kind': 'checkoutSummary', 'formId': newFormId(),
            **_pick(result, _CHECKOUT_SUMMARY),
            'items': [_pick(item, _CHECKOUT_LINE)
                      for item in result['items']]}


def _storeReturns(result: dict, context: CardContext) -> dict:
    return _withFormIf({'kind': 'storeReturns', 'returns': [
        _withItems(r, _STORE_RETURN) for r in result['returns']]},
        'returns', 'requested')


def _storeReturn(result: dict, context: CardContext) -> dict:
    return _withFormIf({'kind': 'storeReturns',
                        'returns': [_withItems(result, _STORE_RETURN)]},
                       'returns', 'requested')


def _myReturns(result: dict, context: CardContext) -> dict:
    return {'kind': 'myReturns',
            'returns': [_withItems(r, _MY_RETURN) for r in result['returns']]}


def _myReturn(result: dict, context: CardContext) -> dict:
    return {'kind': 'myReturns', 'returns': [_withItems(result, _MY_RETURN)]}


def _policy(result: dict, context: CardContext) -> dict:
    return {'kind': 'policy', 'currency': context.currency(),
            'deliveryZones': result.get('deliveryZones'),
            **{field: result.get(field) for field in POLICY_ANSWER_FIELDS}}


def _photos(result: dict, context: CardContext) -> dict:
    return {'kind': 'photos',
            'photos': [_pick(p, ('_id', 'url', 'prompt'))
                       for p in result['photos']],
            'notGenerated': result.get('notGenerated')}


def _photoChoice(result: dict, context: CardContext) -> dict:
    """The photos the admin picks one of for the product (answered with
    ProductPhotoAnswer)."""
    return {'kind': 'productPhotoChoice', 'formId': newFormId(),
            'productId': result['productId'],
            'productName': result['productName'],
            'photos': [_pick(p, ('_id', 'url', 'source', 'prompt'))
                       for p in result['photos']],
            'notGenerated': result['notGenerated']}


def _photoColors(result: dict, context: CardContext) -> dict | None:
    """The colors read from a photo, the admin picking the brand colors
    from them (answered with BrandColorsAnswer); none read, no card."""
    if not result['colors']:
        return None
    return {'kind': 'photoColors', 'formId': newFormId(),
            'colors': [_pick(c, ('hex', 'share', 'neutral'))
                       for c in result['colors']],
            'focus': result['focus'], 'focusFound': result['focusFound']}


def _store(result: dict, context: CardContext) -> dict | None:
    # getUserStore wraps it ({"store": ...}, null before it is set up).
    store = result['store'] if 'store' in result else result
    if store is None:
        return None
    location = store.get('location')
    return {'kind': 'store', **_pick(store, _STORE),
            'location': (_pick(location, ('latitude', 'longitude'))
                         if location else None)}


# The tools whose result makes a card, by tool name (the names are unique
# across the audiences).
CARDS: dict[str, Callable[[dict, CardContext], dict | None]] = {
    'listStoreProducts': _shopProducts,
    'listMyStoreProducts': _storeProducts,
    'addStoreProducts': _storeProducts,
    'addStoreProduct': _storeProduct,
    'updateStoreProduct': _storeProduct,
    'setProductPhoto': _storeProduct,
    'getStoreOrders': _storeOrders,
    'completeOrder': _storeOrder,
    'cancelStoreOrder': _storeOrder,
    'getMyOrders': _myOrders,
    'payOrder': _myOrder,
    'addPaymentReference': _myOrder,
    'confirmOrderPayment': _storeOrder,
    'cancelMyOrder': _myOrder,
    'createOrder': _checkout,
    'checkoutMyCart': _checkout,
    'previewMyCheckout': _checkoutSummary,
    'getStoreReturns': _storeReturns,
    'approveReturn': _storeReturn,
    'rejectReturn': _storeReturn,
    'receiveReturn': _storeReturn,
    'getMyReturns': _myReturns,
    'requestReturn': _myReturn,
    'cancelMyReturn': _myReturn,
    'getMyStorePolicy': _policy,
    'updateMyStorePolicy': _policy,
    'getStorePolicy': _policy,
    'generateProductPhotos': _photos,
    'makeProductPhotoChoices': _photoChoice,
    'findColorsInPhoto': _photoColors,
    'getUserStore': _store,
    'createUserStore': _store,
    'setUserStoreLocation': _store,
    'updateMyStoreDetails': _store,
}

# The cards that are the only way to finish an action: shown whatever the
# model says, so their tools take no showCard.
ALWAYS_SHOWN = (_checkout, _checkoutSummary, _photoChoice, _photoColors)
SHOW_CARD = 'showCard'
# The tools whose card the model may leave out.
SHOW_CARD_TOOLS = frozenset(name for name, makeCard in CARDS.items()
                            if makeCard not in ALWAYS_SHOWN)
_SHOW_CARD_SCHEMA = {
    'default': True,
    'description': ('Whether the app shows this result as a card under '
                    'your reply. false when the card would not help the '
                    'user: a look-up for yourself (an id before another '
                    'tool), a short answer, or what a card in this '
                    'conversation already shows; then say what they need '
                    'in your text.'),
    'title': 'Showcard',
    'type': 'boolean',
}


def chatParameters(name: str, schema: dict) -> dict:
    """The chat's parameter schema of tool `name`: `schema` (the MCP
    server's), plus the optional showCard of a SHOW_CARD_TOOLS tool."""
    if name not in SHOW_CARD_TOOLS:
        return schema
    return {**schema, 'properties': {**schema.get('properties', {}),
                                     SHOW_CARD: _SHOW_CARD_SCHEMA}}


def _hidesCard(call: dict) -> bool:
    """Whether the model's tool call said showCard false (a tool of
    SHOW_CARD_TOOLS only; arguments that are no JSON object show it)."""
    if call.get('name') not in SHOW_CARD_TOOLS:
        return False
    try:
        arguments = json.loads(call.get('arguments') or '{}')
    except (TypeError, ValueError):
        return False
    return isinstance(arguments, dict) and arguments.get(SHOW_CARD) is False


# A card listing rows of one kind, and the key that lists them: a later
# single-row card of that kind updates the row there.
_ROWS = {'storeProducts': 'products', 'storeOrders': 'orders',
         'myOrders': 'orders', 'storeReturns': 'returns',
         'myReturns': 'returns'}
_ANSWERABLE_STATUS = {'storeOrders': ('orders', 'paid'),
                      'storeReturns': ('returns', 'requested')}


def _merged(cards: list[dict], card: dict) -> bool:
    """Put `card`'s one row on the latest card listing it; False when no
    card lists it."""
    rowsKey = _ROWS.get(card['kind'])
    if rowsKey is None or len(card[rowsKey]) != 1:
        return False
    row = card[rowsKey][0]
    for index in range(len(cards) - 1, -1, -1):
        listed = cards[index]
        if listed['kind'] != card['kind']:
            continue
        rows = listed[rowsKey]
        position = next((i for i, r in enumerate(rows)
                         if r['_id'] == row['_id']), None)
        if position is None:
            continue
        updated = {**listed, rowsKey: [*rows[:position], row,
                                       *rows[position + 1:]]}
        if listed['kind'] in _ANSWERABLE_STATUS:
            key, status = _ANSWERABLE_STATUS[listed['kind']]
            updated = _withFormIf(updated, key, status)
        cards[index] = updated
        return True
    return False


def _validCard(card: dict, tool: str) -> dict | None:
    """`card` as it is sent and stored; None (logged) when the tool's
    result did not fit it."""
    rowsKey = _ROWS.get(card['kind']) or (
        'products' if card['kind'] == 'shopProducts' else None)
    if rowsKey is not None:
        card = {**card, rowsKey: card[rowsKey][:CHAT_CARD_ROWS_MAX]}
    try:
        return _COMPONENT.dump_python(_COMPONENT.validate_python(card),
                                      by_alias=True)
    except ValidationError as error:
        logging.warning({'event': 'chatCardSkipped', 'tool': tool,
                         'errors': [e['type'] for e in error.errors(
                             include_input=False)]})
        return None


def build(steps: list[dict], forms: dict[str, dict],
          context: CardContext,
          cardResults: dict[str, str] | None = None) -> list[dict]:
    """The components of a turn whose tool steps are `steps`: a card per
    tool result, the forms the form tools opened (`forms`, by tool call id)
    in their place, the latest CHAT_COMPONENTS_PER_TURN_MAX of them; a
    call with showCard false adds no card, only updates a listed row. A card
    is built from the tool's full result (`cardResults[callId]`, its JSON,
    held for the turn by ChatActions._runModel) when there is one, else
    from the step's content: a step holds the AI copy (chatModel.
    aiContent), which leaves out what only the card shows."""
    cardResults = cardResults or {}
    calls = [call for step in steps if step['role'] == 'assistant'
             for call in step.get('toolCalls') or []]
    names = {call['id']: call['name'] for call in calls}
    hidden = {call['id'] for call in calls if _hidesCard(call)}
    components: list[dict] = []
    for step in steps:
        if step['role'] != 'tool':
            continue
        callId = step.get('toolCallId')
        if callId in forms:
            components.append(forms[callId])
            continue
        name = names.get(callId)
        makeCard = CARDS.get(name)
        if makeCard is None:
            continue
        try:
            result = json.loads(cardResults.get(callId)
                                or step.get('content') or 'null')
        except ValueError:
            continue
        if not isinstance(result, dict) or 'error' in result:
            continue
        try:
            card = makeCard(result, context)
        except (KeyError, TypeError):
            logging.warning({'event': 'chatCardSkipped', 'tool': name,
                             'errors': ['shape']})
            continue
        if card is None:
            continue
        card = _validCard(card, name)
        if card is None:
            continue
        # A hidden card still updates the row on a card that lists it.
        if not _merged(components, card) and callId not in hidden:
            components.append(card)
    return components[-CHAT_COMPONENTS_PER_TURN_MAX:]

"""chatForms.py — the chat's forms (hand-written).

The model opens a form with a form tool: a chat-only tool (never on the MCP
servers, whose clients cannot show one) that runs here, in the chat route,
and puts a form component under its reply (objects/chatComponents.py). The
tool's result only tells the model the form is on screen.

The user answers a form, or a card that takes a choice (chatCards.py), and
the client sends the answer through the chat route's `form`. answerTurn
checks it against the component it answers (in this conversation, once),
and turns it into the user's turn: a summary the user sees (userText) and
the details the model acts on (userContent, "Form answer: {...}"). The
model then calls the tool that does it.

chatToolsCreator.py lists the form tools in chatTools.TOOLS next to the
route tools, and adds the toolset an admin with no store yet chats with
(SETUP_TOOLSET: setting up the store only).

Who and where come only from the chat route (its JWT userId, the
conversation's storeId), never from the model or the answer.
"""

import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from actions.CustomerActions import CUSTOMERS
from actions.OrderActions import (DELIVERY_FOR_PICKUP, DELIVERY_REQUIRED,
                                  NO_STORE_PIN_YET, ORDERS,
                                  bookableFulfillments, storeDelivery)
from actions.PolicyActions import _storePolicy
from actions.deliveryPricing import shownFeePerKm
from actions.StoreActions import STORES, ownerQuery
from mongoDb import db
from objects import (CHAT_ANSWER_ID_MAX_LENGTH, CHAT_CONTACT_NUMBER_MAX_LENGTH,
                     CHAT_MESSAGE_MAX_LENGTH, PHOTOS_PER_CALL_MAX,
                     POLICY_ANSWER_FIELDS, PRODUCT_BATCH_MAX,
                     PRODUCT_DESCRIPTION_MAX_LENGTH, PRODUCT_INVENTORY_MAX,
                     PRODUCT_NAME_MAX_LENGTH, PRODUCT_PRICE_MAX,
                     STORE_ADDRESS_MAX_LENGTH, STORE_DESCRIPTION_MAX_LENGTH,
                     STORE_NAME_MAX_LENGTH, STORE_OPENING_HOURS_MAX_LENGTH,
                     ChatComponent, ChatFormAnswerTagged, PolicySection,
                     StoreCurrency)
from utils import generateRandomString

# ChatActions.CHAT_TURNS (ChatActions imports this module).
CHAT_TURNS = 'ChatTurns'
# The toolset of an admin whose store is not set up yet: the store's
# details, nothing else (a route tool needs a set-up store).
SETUP_TOOLSET = 'adminSetup'
# Its route tools (chatToolsCreator.py copies them from the admin's).
SETUP_ROUTE_TOOLS = ('createUserStore', 'getUserStore')
# What a form tool tells the model.
FORM_SHOWN = ('The form is on screen under your reply. Ask the user to fill '
              'it in there; its answer comes as their next message.')
ALREADY_VERIFIED = 'The phone number is already verified'
NO_PHOTO_STYLE_YET = ('Set the photo style first (updateMyStoreTheme), then '
                      'open the photo form')
ORDER_NOT_RETURNABLE = 'Only a completed order of this customer can be returned'
ORDER_NOT_FOUND = 'Order not found'
STORE_NOT_FOUND = 'Store not found'
PICKUP_ONLY = ('Only pickup can be booked at this store: place the order '
               'without asking how it is fulfilled')
# Refusals of a form answer, the user's to act on.
FORM_NOT_FOUND = 'That form is no longer in this chat'
FORM_ALREADY_ANSWERED = 'You already sent this form'
FORM_DOES_NOT_MATCH = "That answer doesn't fit this form"
FORM_ANSWER_INVALID = 'That form answer is not valid: {fields}'
FORM_NEEDS_CONVERSATION = 'A form answer belongs to a chat'
MESSAGE_AND_FORM = 'Send a message or a form answer, not both'
NOT_ON_THE_CARD = 'Pick from what the card shows'
NOTHING_PICKED = 'Pick at least one'
PICKED_TWICE = 'Each one once'
REJECT_NEEDS_REASON = 'Say why you reject it'
PHONE_NOT_VERIFIED = 'Verify your phone number first'
NO_POLICY_ANSWER = 'Answer at least one question'
FORM_ANSWER_PREFIX = 'Form answer: '

_ANSWER = TypeAdapter(ChatFormAnswerTagged)
_COMPONENT = TypeAdapter(ChatComponent)

# The policy questions each section of the policy form asks.
POLICY_SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    'payment': ('acceptedPaymentMethods', 'paidOrderCancellation'),
    'delivery': ('deliveryOptions', 'deliveryAreas', 'offersFreeDelivery',
                 'freeDeliveryMinimum', 'handoverDays'),
    'returns': ('acceptsReturns', 'returnWindowDays', 'returnConditions',
                'nonReturnableItems', 'refundMethod', 'returnShippingPaidBy',
                'defectReportDays'),
    'other': ('sendsMarketingMessages', 'additionalTerms'),
}
assert sorted(f for fields in POLICY_SECTION_FIELDS.values()
              for f in fields) == sorted(POLICY_ANSWER_FIELDS)

# Which answer kind answers which component kind.
ANSWERS_TO = {
    'storeDetails': 'storeDetailsForm',
    'location': 'locationForm',
    'newProducts': 'newProductsForm',
    'policy': 'policyForm',
    'return': 'returnForm',
    'phoneVerified': 'phoneVerificationForm',
    'photoRequest': 'photoRequestForm',
    'orderItems': 'shopProducts',
    'payment': 'checkout',
    'handover': 'storeOrders',
    'returnDecision': 'storeReturns',
    'fulfillment': 'fulfillmentForm',
}

_LABELS = {
    'card': 'card', 'gcash': 'GCash', 'maya': 'Maya', 'grabPay': 'GrabPay',
    'bankTransfer': 'bank transfer', 'pickup': 'pickup',
    'delivery': 'delivery', 'originalPayment': 'the original payment',
    'storeCredit': 'store credit', 'exchange': 'an exchange',
    'customer': 'the customer', 'store': 'the store',
    'notAllowed': 'not allowed', 'beforeHandover': 'before handover',
    'changedMind': 'changed my mind', 'defective': 'defective',
    'wrongItem': 'wrong item',
}
_POLICY_LABELS = {
    'acceptsReturns': 'Accepts returns',
    'returnWindowDays': 'Return window (days)',
    'returnConditions': 'Return conditions',
    'nonReturnableItems': 'Non-returnable items',
    'refundMethod': 'Refund method',
    'returnShippingPaidBy': 'Return shipping paid by',
    'defectReportDays': 'Days to report a defect',
    'deliveryOptions': 'Delivery options',
    'deliveryAreas': 'Delivery areas',
    'offersFreeDelivery': 'Free delivery',
    'freeDeliveryMinimum': 'Free delivery from',
    'handoverDays': 'Days until ready for pickup or delivered',
    'acceptedPaymentMethods': 'Payment methods',
    'paidOrderCancellation': 'Cancelling a paid order',
    'sendsMarketingMessages': 'Sends offers',
    'additionalTerms': 'Other terms',
}


def newFormId() -> str:
    return generateRandomString()


def orderRef(orderId: str) -> str:
    """The short reference an order is shown by: #<last 6 of its id>."""
    return f'#{orderId[-6:].upper()}'


def _label(value) -> str:
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, list):
        return ', '.join(_label(item) for item in value)
    if isinstance(value, float):
        return f'{value:g}'
    return _LABELS.get(value, str(value)) if isinstance(value, str) else str(value)


def _cut(text: str, limit: int = CHAT_MESSAGE_MAX_LENGTH) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _component(data: dict) -> dict:
    """A component as it is sent and stored."""
    return _COMPONENT.dump_python(_COMPONENT.validate_python(data),
                                  by_alias=True)


def _fieldsOf(error: ValidationError) -> str:
    """The fields a ValidationError refused, never the values."""
    fields = []
    for detail in error.errors(include_input=False):
        field = '.'.join(str(part) for part in detail['loc']) or 'form'
        if field not in fields:
            fields.append(field)
    return ', '.join(fields)


# ── Form tools ──────────────────────────────

class _Args(BaseModel):
    model_config = ConfigDict(extra='ignore', hide_input_in_errors=True)


class ShowStoreDetailsFormArgs(_Args):
    model_config = ConfigDict(title='showStoreDetailsFormArguments')
    name: str | None = None
    description: str | None = None
    currency: StoreCurrency | None = None
    contactNumber: str | None = None
    address: str | None = None
    openingHours: str | None = None


class ShowStoreLocationFormArgs(_Args):
    model_config = ConfigDict(title='showStoreLocationFormArguments')


class ProductDraftArgs(_Args):
    model_config = ConfigDict(title='productDraft')
    name: str | None = None
    price: float | None = None
    inventory: int | None = None
    description: str | None = None


class ShowNewProductsFormArgs(_Args):
    model_config = ConfigDict(title='showNewProductsFormArguments')
    products: list[ProductDraftArgs] | None = Field(
        None, max_length=PRODUCT_BATCH_MAX)


class ShowPolicyFormArgs(_Args):
    model_config = ConfigDict(title='showPolicyFormArguments')
    section: PolicySection


class ShowPhotoRequestFormArgs(_Args):
    model_config = ConfigDict(title='showPhotoRequestFormArguments')
    count: int | None = Field(None, ge=1, le=PHOTOS_PER_CALL_MAX)


class ShowReturnFormArgs(_Args):
    model_config = ConfigDict(title='showReturnFormArguments')
    orderId: str = Field(..., min_length=1,
                         max_length=CHAT_ANSWER_ID_MAX_LENGTH)


class ShowPhoneVerificationArgs(_Args):
    model_config = ConfigDict(title='showPhoneVerificationArguments')


class ShowFulfillmentFormArgs(_Args):
    model_config = ConfigDict(title='showFulfillmentFormArguments')


class FormRefused(Exception):
    """A form tool that cannot open its form: its message goes to the model
    as the tool's error."""


def _clip(value, limit: int):
    """A prefill the model gave, cut to what the form field holds."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _ownStore(userId: str, projection: dict) -> dict:
    store = db.read(ownerQuery(userId), STORES, projection=projection,
                    findOne=True)
    if store is None:
        raise FormRefused('Create your store first')
    return store


def _storeDetailsForm(userId: str, storeId: str,
                      args: ShowStoreDetailsFormArgs) -> dict:
    store = _ownStore(userId, {'currency': 1})
    return {
        'kind': 'storeDetailsForm',
        'formId': newFormId(),
        'name': _clip(args.name, STORE_NAME_MAX_LENGTH),
        'description': _clip(args.description, STORE_DESCRIPTION_MAX_LENGTH),
        'currency': args.currency or store['currency'],
        'contactNumber': _clip(args.contactNumber,
                               CHAT_CONTACT_NUMBER_MAX_LENGTH),
        'address': _clip(args.address, STORE_ADDRESS_MAX_LENGTH),
        'openingHours': _clip(args.openingHours,
                              STORE_OPENING_HOURS_MAX_LENGTH),
    }


def _locationForm(userId: str, storeId: str,
                  args: ShowStoreLocationFormArgs) -> dict:
    store = _ownStore(userId, {'location': 1, 'address': 1})
    location = store.get('location')
    return {
        'kind': 'locationForm',
        'formId': newFormId(),
        'location': ({'latitude': location['latitude'],
                      'longitude': location['longitude']}
                     if location else None),
        'address': store.get('address'),
    }


def _newProductsForm(userId: str, storeId: str,
                     args: ShowNewProductsFormArgs) -> dict:
    store = _ownStore(userId, {'currency': 1})
    drafts = []
    for draft in args.products or []:
        price, inventory = draft.price, draft.inventory
        drafts.append({
            'name': _clip(draft.name, PRODUCT_NAME_MAX_LENGTH),
            # A prefill out of range is left for the owner to type.
            'price': (price if price is not None
                      and 0 <= price <= PRODUCT_PRICE_MAX else None),
            'inventory': (inventory if inventory is not None
                          and 0 <= inventory <= PRODUCT_INVENTORY_MAX
                          else None),
            'description': _clip(draft.description,
                                 PRODUCT_DESCRIPTION_MAX_LENGTH),
        })
    return {'kind': 'newProductsForm', 'formId': newFormId(),
            'currency': store['currency'], 'products': drafts}


def _policyForm(userId: str, storeId: str, args: ShowPolicyFormArgs) -> dict:
    store = _ownStore(userId, {'_id': 1, 'createdAt': 1, 'currency': 1})
    policy = _storePolicy(store)
    return {'kind': 'policyForm', 'formId': newFormId(),
            'section': args.section, 'currency': store['currency'],
            'deliveryFeePerKm': policy['deliveryFeePerKm'],
            **{field: policy[field] for field in POLICY_ANSWER_FIELDS}}


def _photoRequestForm(userId: str, storeId: str,
                      args: ShowPhotoRequestFormArgs) -> dict:
    store = _ownStore(userId, {'photoStyle': 1})
    if not store.get('photoStyle'):
        raise FormRefused(NO_PHOTO_STYLE_YET)
    return {'kind': 'photoRequestForm', 'formId': newFormId(),
            'count': args.count or 1}


def _returnForm(userId: str, storeId: str, args: ShowReturnFormArgs) -> dict:
    order = db.read({'_id': args.orderId, 'customerId': userId,
                     'storeId': storeId}, ORDERS,
                    projection={'status': 1, 'items': 1, 'currency': 1},
                    findOne=True)
    if order is None:
        raise FormRefused(ORDER_NOT_FOUND)
    if order['status'] != 'completed':
        raise FormRefused(ORDER_NOT_RETURNABLE)
    return {
        'kind': 'returnForm',
        'formId': newFormId(),
        'orderId': args.orderId,
        'currency': order['currency'],
        'items': [{'item': position, 'name': item['name'],
                   'price': item['price'], 'quantity': item['quantity']}
                  for position, item in enumerate(order['items'], start=1)],
    }


def _phoneVerification(userId: str, storeId: str,
                       args: ShowPhoneVerificationArgs) -> dict:
    customer = db.read({'_id': userId}, CUSTOMERS,
                       projection={'phoneNumber': 1}, findOne=True)
    if customer is not None and customer.get('phoneNumber'):
        raise FormRefused(ALREADY_VERIFIED)
    return {'kind': 'phoneVerificationForm', 'formId': newFormId()}


def _fulfillmentForm(userId: str, storeId: str,
                     args: ShowFulfillmentFormArgs) -> dict:
    delivery = storeDelivery(storeId)
    options = bookableFulfillments(delivery)
    if not options:
        raise FormRefused(NO_STORE_PIN_YET)
    if options == ['pickup']:
        raise FormRefused(PICKUP_ONLY)
    store = db.read({'_id': storeId}, STORES,
                    projection={'currency': 1, 'address': 1, 'location': 1},
                    findOne=True)
    if store is None:
        raise FormRefused(STORE_NOT_FOUND)
    location = store.get('location')
    return {
        'kind': 'fulfillmentForm',
        'formId': newFormId(),
        'options': options,
        'currency': store['currency'],
        'storeAddress': store.get('address'),
        'storeLocation': ({'latitude': location['latitude'],
                           'longitude': location['longitude']}
                          if location else None),
        'deliveryAreas': delivery['deliveryAreas'],
        'deliveryFeePerKm': shownFeePerKm(store['currency']),
        'freeDeliveryMinimum': delivery['freeDeliveryMinimum'],
        'handoverDays': delivery['handoverDays'],
    }


@dataclass(frozen=True)
class FormTool:
    toolsets: tuple[str, ...]
    description: str
    argsModel: type[BaseModel]
    build: Callable[[str, str, BaseModel], dict]

    def schema(self, name: str) -> dict:
        return {
            'type': 'function',
            'function': {
                'name': name,
                'description': self.description,
                'parameters': self.argsModel.model_json_schema(by_alias=True),
            },
        }


FORM_TOOLS: dict[str, FormTool] = {
    'showStoreDetailsForm': FormTool(
        (SETUP_TOOLSET, ),
        "Open the store details form under your reply: the store's name, "
        'what it sells, its currency, contact number, address and opening '
        'hours. Use it to set up the store instead of asking each detail in '
        'chat. Prefill only what the owner already said; never invent a '
        'value. The owner fills it in and sends it; then call '
        'createUserStore with exactly what they sent.',
        ShowStoreDetailsFormArgs, _storeDetailsForm),
    'showStoreLocationForm': FormTool(
        ('admin', ),
        'Open a map under your reply where the owner pins the store. The '
        'pin they send comes as their next message: then call '
        'setUserStoreLocation with its latitude and longitude.',
        ShowStoreLocationFormArgs, _locationForm),
    'showNewProductsForm': FormTool(
        ('admin', ),
        'Open a form under your reply where the owner lists new products: '
        'name, price, units in stock and an optional description, one row '
        'each. Prefill only what the owner already said; never invent a '
        'price or a stock count. Use it to add products instead of asking '
        'each detail in chat. Their list comes as their next message: then '
        'call addStoreProducts with it.',
        ShowNewProductsFormArgs, _newProductsForm),
    'showPolicyForm': FormTool(
        ('admin', ),
        "Open a form under your reply with one section of the store's "
        'policy questions, showing the current answers: payment (payment '
        'methods, cancelling a paid order), delivery (pickup or delivery, '
        'areas, fee, free delivery minimum, days to hand over), returns '
        '(returns, window, conditions, non-returnable items, refund, who '
        'pays return shipping, days to report a defect) or other (offers, '
        'other terms). Use it instead of asking each question in chat. The '
        'answers come as their next message: then call updateMyStorePolicy '
        'with them.',
        ShowPolicyFormArgs, _policyForm),
    'showPhotoRequestForm': FormTool(
        ('admin', ),
        "Open a form under your reply where the owner uploads 1 to 10 plain "
        'photos of one item, picks how many product photos to make (count, '
        'prefilled) and adds notes. The store needs a photo style first '
        '(updateMyStoreTheme). The upload comes as their next message: then '
        'call generateProductPhotos with its imageUrls, count and notes.',
        ShowPhotoRequestFormArgs, _photoRequestForm),
    'showReturnForm': FormTool(
        ('customer', ),
        "Open a form under your reply where the customer picks which items "
        "of their completed order go back (orderId from getMyOrders), how "
        'many, why, and a note. Tell them the policy first (getStorePolicy). '
        'Their answer comes as their next message: then call requestReturn '
        'with it.',
        ShowReturnFormArgs, _returnForm),
    'showPhoneVerification': FormTool(
        ('customer', ),
        'Open phone verification under your reply: the customer gets a code '
        'by text and enters it there. Use it when createOrder refuses '
        'because the phone number is not verified. When it is done, their '
        'next message says so: then call createOrder again.',
        ShowPhoneVerificationArgs, _phoneVerification),
    'showFulfillmentForm': FormTool(
        ('customer', ),
        'Open a form under your reply where the customer chooses pickup or '
        'delivery, and for a delivery types the address, pins it on a map '
        'and adds notes. Use it before createOrder when the store delivers '
        "(getStorePolicy's deliveryOptions); it refuses when only pickup "
        'can be booked, and then createOrder needs no fulfillment. Their '
        'choice comes as their next message: then call createOrder with '
        'the items and exactly its fulfillment and delivery.',
        ShowFulfillmentFormArgs, _fulfillmentForm),
}


def formToolNames(toolset: str) -> set[str]:
    return {name for name, tool in FORM_TOOLS.items()
            if toolset in tool.toolsets}


def formToolSchemas(toolset: str) -> list[dict]:
    return [FORM_TOOLS[name].schema(name)
            for name in sorted(formToolNames(toolset))]


def runFormTool(toolset: str, name: str, argumentsJson: str, userId: str,
                storeId: str) -> tuple[str, dict | None]:
    """The tool message's content for the model's call of the form tool
    `name`, and the form it opened (None when it refused)."""
    tool = FORM_TOOLS.get(name)
    if tool is None or toolset not in tool.toolsets:
        return json.dumps({'error': f'Unknown tool {name}'}), None
    try:
        arguments = json.loads(argumentsJson or '{}')
    except (TypeError, ValueError):
        return json.dumps({'error': f'The arguments of {name} are not '
                           'valid JSON'}), None
    if not isinstance(arguments, dict):
        return json.dumps({'error': f'The arguments of {name} must be a '
                           'JSON object'}), None
    try:
        args = tool.argsModel.model_validate(arguments)
    except ValidationError as error:
        return json.dumps({'error': f'Invalid arguments for {name}: '
                           f'{_fieldsOf(error)}'}), None
    try:
        form = _component(tool.build(userId, storeId, args))
    except FormRefused as refusal:
        return json.dumps({'error': str(refusal)}), None
    return json.dumps({'result': FORM_SHOWN}), form


# ── Form answers ────────────────────────────

def validAnswer(form) -> BaseModel | None:
    """The chat route's `form` as an answer model; None when none was
    sent. A refusal names the fields, never their values."""
    if form is None:
        return None
    if not isinstance(form, dict):
        raise ValueError(FORM_ANSWER_INVALID.format(fields='form'))
    try:
        return _ANSWER.validate_python(form)
    except ValidationError as error:
        raise ValueError(FORM_ANSWER_INVALID.format(
            fields=_fieldsOf(error))) from None


def _answeredComponent(conversationId: str, answer: BaseModel) -> dict:
    """The component `answer` answers, in this conversation: refused when
    it is not there, is of another kind, or was answered already."""
    turn = db.read({'conversationId': conversationId,
                    'components.formId': answer.formId}, CHAT_TURNS,
                   projection={'components': 1}, findOne=True)
    component = next((c for c in (turn or {}).get('components', [])
                      if c.get('formId') == answer.formId), None)
    if component is None:
        raise ValueError(FORM_NOT_FOUND)
    if ANSWERS_TO[answer.kind] != component['kind']:
        raise ValueError(FORM_DOES_NOT_MATCH)
    if db.read({'conversationId': conversationId,
                'formAnswer.formId': answer.formId}, CHAT_TURNS,
               projection={'_id': 1}, findOne=True) is not None:
        raise ValueError(FORM_ALREADY_ANSWERED)
    return component


def _unique(ids: list[str]) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError(PICKED_TWICE)


def _itemsText(items: list[dict]) -> str:
    return ', '.join(f"{item['quantity']} × {item['name']}" for item in items)


def _storeDetails(answer, component, userId) -> tuple[str, dict]:
    parts = [answer.name, answer.currency, answer.contactNumber,
             answer.address, answer.openingHours]
    text = ('Store details: ' + ' · '.join(p for p in parts if p)
            + f'\n{answer.description}')
    return text, answer.model_dump(exclude={'formId'})


def _location(answer, component, userId) -> tuple[str, dict]:
    return (f'Pinned the store at {answer.latitude:.5f}, '
            f'{answer.longitude:.5f}',
            answer.model_dump(exclude={'formId'}))


def _newProducts(answer, component, userId) -> tuple[str, dict]:
    currency = component['currency']
    lines = [f'- {p.name}, {p.price:g} {currency}, {p.inventory} in stock'
             + (f': {p.description}' if p.description else '')
             for p in answer.products]
    return ('Add these products:\n' + '\n'.join(lines),
            answer.model_dump(exclude={'formId'}))


def _policy(answer, component, userId) -> tuple[str, dict]:
    fields = POLICY_SECTION_FIELDS[component['section']]
    values = {field: getattr(answer, field) for field in fields
              if getattr(answer, field) is not None}
    if not values:
        raise ValueError(NO_POLICY_ANSWER)
    currency = component['currency']
    lines = []
    for field, value in values.items():
        shown = _label(value)
        if field == 'freeDeliveryMinimum':
            shown = f'{value:g} {currency}'
        lines.append(f'- {_POLICY_LABELS[field]}: {shown}')
    return ('Policy answers:\n' + '\n'.join(lines),
            {'kind': 'policy', **values})


def _return(answer, component, userId) -> tuple[str, dict]:
    byPosition = {item['item']: item for item in component['items']}
    _unique([item.item for item in answer.items])
    picked = []
    for item in answer.items:
        ordered = byPosition.get(item.item)
        if ordered is None or not 1 <= item.quantity <= ordered['quantity']:
            raise ValueError(NOT_ON_THE_CARD)
        picked.append({'name': ordered['name'], 'quantity': item.quantity})
    text = (f'Return {_itemsText(picked)}. Reason: {_label(answer.reason)}.'
            + (f' Note: {answer.note}' if answer.note else ''))
    return text, {'kind': 'return', 'orderId': component['orderId'],
                  **answer.model_dump(exclude={'formId', 'kind'})}


def _phoneVerified(answer, component, userId) -> tuple[str, dict]:
    customer = db.read({'_id': userId}, CUSTOMERS,
                       projection={'phoneNumber': 1}, findOne=True)
    if customer is None or not customer.get('phoneNumber'):
        raise ValueError(PHONE_NOT_VERIFIED)
    return 'I verified my phone number.', {'kind': 'phoneVerified'}


def _photoRequest(answer, component, userId) -> tuple[str, dict]:
    count = answer.count
    text = (f'Make {count} product photo{"s" if count > 1 else ""} from my '
            f'{len(answer.imageUrls)} photo'
            f'{"s" if len(answer.imageUrls) > 1 else ""}.'
            + (f' Notes: {answer.notes}' if answer.notes else ''))
    return text, answer.model_dump(exclude={'formId'})


def _orderItems(answer, component, userId) -> tuple[str, dict]:
    products = {p['_id']: p for p in component['products']}
    _unique([item.productId for item in answer.items])
    picked = []
    for item in answer.items:
        product = products.get(item.productId)
        if product is None or item.quantity < 1:
            raise ValueError(NOT_ON_THE_CARD)
        picked.append({'name': product['name'], 'quantity': item.quantity})
    return (f'Order {_itemsText(picked)}',
            answer.model_dump(exclude={'formId'}))


def _payment(answer, component, userId) -> tuple[str, dict]:
    if answer.paymentMethod not in component['paymentMethods']:
        raise ValueError(NOT_ON_THE_CARD)
    order = component['order']
    return (f'Pay {order["total"]:g} {order["currency"]} with '
            f'{_label(answer.paymentMethod)}',
            {'kind': 'payment', 'orderId': order['_id'],
             'paymentMethod': answer.paymentMethod})


def _handover(answer, component, userId) -> tuple[str, dict]:
    paid = {o['_id']: o for o in component['orders'] if o['status'] == 'paid'}
    _unique(answer.orderIds)
    if not answer.orderIds:
        raise ValueError(NOTHING_PICKED)
    lines = []
    for orderId in answer.orderIds:
        order = paid.get(orderId)
        if order is None:
            raise ValueError(NOT_ON_THE_CARD)
        lines.append(f'- {orderRef(orderId)}: {_itemsText(order["items"])}')
    return ('Handed over:\n' + '\n'.join(lines),
            answer.model_dump(exclude={'formId'}))


def _returnDecision(answer, component, userId) -> tuple[str, dict]:
    requested = {r['_id']: r for r in component['returns']
                 if r['status'] == 'requested'}
    request = requested.get(answer.returnId)
    if request is None:
        raise ValueError(NOT_ON_THE_CARD)
    reason = (answer.reason or '').strip() or None
    if answer.decision == 'reject' and reason is None:
        raise ValueError(REJECT_NEEDS_REASON)
    items = _itemsText(request['items'])
    if answer.decision == 'approve':
        return (f'Approve the return of {items}',
                {'kind': 'returnDecision', 'returnId': answer.returnId,
                 'decision': 'approve'})
    return (f'Reject the return of {items}: {reason}',
            {'kind': 'returnDecision', 'returnId': answer.returnId,
             'decision': 'reject', 'reason': reason})


def _fulfillment(answer, component, userId) -> tuple[str, dict]:
    if answer.fulfillment not in component['options']:
        raise ValueError(NOT_ON_THE_CARD)
    if answer.fulfillment == 'pickup':
        if answer.delivery is not None:
            raise ValueError(DELIVERY_FOR_PICKUP)
        return ('I’ll pick it up at the store.',
                {'kind': 'fulfillment', 'fulfillment': 'pickup',
                 'delivery': None})
    if answer.delivery is None:
        raise ValueError(DELIVERY_REQUIRED)
    # An empty note is no note.
    delivery = {**answer.delivery.model_dump(),
                'notes': (answer.delivery.notes or '').strip() or None}
    text = (f"Deliver it to {delivery['address']}."
            + (f" Notes: {delivery['notes']}" if delivery['notes'] else ''))
    return text, {'kind': 'fulfillment', 'fulfillment': 'delivery',
                  'delivery': delivery}


_ANSWER_TURNS = {
    'storeDetails': _storeDetails,
    'location': _location,
    'newProducts': _newProducts,
    'policy': _policy,
    'return': _return,
    'phoneVerified': _phoneVerified,
    'photoRequest': _photoRequest,
    'orderItems': _orderItems,
    'payment': _payment,
    'handover': _handover,
    'returnDecision': _returnDecision,
    'fulfillment': _fulfillment,
}
assert set(_ANSWER_TURNS) == set(ANSWERS_TO)


def answerTurn(conversationId: str, userId: str,
               answer: BaseModel) -> tuple[str, str]:
    """The user's turn for a form answer: (userText, userContent). userText
    is the summary the user sees; userContent is what the model reads, the
    summary and the details it acts on."""
    component = _answeredComponent(conversationId, answer)
    text, details = _ANSWER_TURNS[answer.kind](answer, component, userId)
    userText = _cut(text)
    userContent = (f'{userText}\n\n{FORM_ANSWER_PREFIX}'
                   f'{json.dumps(details, ensure_ascii=False, default=str)}')
    return userText, userContent

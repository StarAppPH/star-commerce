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
from typing import Callable, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from actions.CustomerActions import CUSTOMERS
from actions.OrderActions import (NO_DELIVERY_ZONES_YET, NOT_OFFERED,
                                  OUTSIDE_DELIVERY_ZONES,
                                  bookableFulfillments, offeredFulfillments,
                                  storeDelivery)
from actions.StoreActions import STORES, ownerQuery
import photoStorage
from mongoDb import db
from objects import (CHAT_ATTACHMENT_EXTENSIONS, CHAT_MESSAGE_MAX_LENGTH,
                     PHOTOS_PER_CALL_MAX, PRODUCT_BATCH_MAX,
                     PRODUCT_DESCRIPTION_MAX_LENGTH, PRODUCT_INVENTORY_MAX,
                     PRODUCT_NAME_MAX_LENGTH, PRODUCT_PRICE_MAX,
                     ChatComponent, ChatFormAnswerTagged,
                     DELIVERY_ZONE_RULES, deliveryZoneAt, ruledZones)
from utils import generateRandomString

# ChatActions.CHAT_TURNS (ChatActions imports this module).
CHAT_TURNS = 'ChatTurns'
# The toolset of an admin whose store is not set up yet: the store's
# details, nothing else (a route tool needs a set-up store).
SETUP_TOOLSET = 'adminSetup'
# Its route tools (chatToolsCreator.py copies them from the admin's).
SETUP_ROUTE_TOOLS = ('createUserStore', 'getUserStore')
# The setup tool that sets the store up: once it succeeds, the turn goes on
# with the admin's tools (the map to pin the store among them).
SETS_UP_THE_STORE = 'createUserStore'
# What a form tool tells the model.
FORM_SHOWN = ('The form is on screen under your reply. Ask the user to fill '
              'it in there; its answer comes as their next message.')
ALREADY_VERIFIED = 'The phone number is already verified'
NO_PHOTO_STYLE_YET = ('Set the photo style first (updateMyStoreTheme), then '
                      'open the photo form')
STORE_NOT_FOUND = 'Store not found'
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
FORM_ANSWER_PREFIX = 'Form answer: '
# The attachments a new products row may take as its photo.
PHOTO_EXTENSIONS = tuple(extension for extension, contentType
                         in CHAT_ATTACHMENT_EXTENSIONS.items()
                         if contentType.startswith('image/'))

_ANSWER = TypeAdapter(ChatFormAnswerTagged)
_COMPONENT = TypeAdapter(ChatComponent)
# The component kinds the app shows. A turn saved with a kind since retired
# (the store details, policy, return and fulfillment forms, now asked in
# chat) is shown without it (ChatActions._turnRow).
COMPONENT_KINDS = frozenset(
    get_args(model.model_fields['kind'].annotation)[0]
    for model in get_args(get_args(ChatComponent)[0]))

# Which answer kind answers which component kind.
ANSWERS_TO = {
    'location': 'locationForm',
    'newProducts': 'newProductsForm',
    'phoneVerified': 'phoneVerificationForm',
    'photoRequest': 'photoRequestForm',
    'orderItems': 'shopProducts',
    'payment': 'checkout',
    'handover': 'storeOrders',
    'returnDecision': 'storeReturns',
    'deliveryPin': 'deliveryPinForm',
    'deliveryZones': 'deliveryZonesForm',
    'productPhoto': 'productPhotoChoice',
}

# How a payment method reads in the user's turn.
_LABELS = {
    'card': 'card', 'gcash': 'GCash', 'maya': 'Maya', 'grabPay': 'GrabPay',
    'bankTransfer': 'bank transfer',
}


def newFormId() -> str:
    return generateRandomString()


def orderRef(orderId: str) -> str:
    """The short reference an order is shown by: #<last 6 of its id>."""
    return f'#{orderId[-6:].upper()}'


def _label(value: str) -> str:
    return _LABELS.get(value, value)


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


class ShowStoreLocationFormArgs(_Args):
    model_config = ConfigDict(title='showStoreLocationFormArguments')


class ProductDraftArgs(_Args):
    model_config = ConfigDict(title='productDraft')
    name: str | None = None
    price: float | None = None
    inventory: int | None = None
    description: str | None = None
    imageUrl: str | None = None


class ShowNewProductsFormArgs(_Args):
    model_config = ConfigDict(title='showNewProductsFormArguments')
    products: list[ProductDraftArgs] | None = Field(
        None, max_length=PRODUCT_BATCH_MAX)


class ShowPhotoRequestFormArgs(_Args):
    model_config = ConfigDict(title='showPhotoRequestFormArguments')
    count: int | None = Field(None, ge=1, le=PHOTOS_PER_CALL_MAX)


class ShowPhoneVerificationArgs(_Args):
    model_config = ConfigDict(title='showPhoneVerificationArguments')


class ShowDeliveryPinFormArgs(_Args):
    model_config = ConfigDict(title='showDeliveryPinFormArguments')


class ShowDeliveryZonesFormArgs(_Args):
    model_config = ConfigDict(title='showDeliveryZonesFormArguments')


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


def _draftPhoto(userId: str, imageUrl) -> str | None:
    """The Storage path of a prefill's photo: the owner's own JPG or PNG
    (one they attached), else None (left for the owner to add)."""
    if imageUrl is None:
        return None
    try:
        path = photoStorage.ownImagePath(
            userId, photoStorage.objectPathFromUrl(imageUrl, 'imageUrl'),
            'imageUrl')
    except ValueError:
        return None
    return path if photoStorage.hasExtension(path, PHOTO_EXTENSIONS) else None


def _newProductsForm(userId: str, storeId: str,
                     args: ShowNewProductsFormArgs) -> dict:
    store = _ownStore(userId, {'currency': 1})
    drafts, photos = [], []
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
        photos.append(_draftPhoto(userId, draft.imageUrl))
    # A photo on several rows shows them all (a list, a screenshot): no
    # row's own photo.
    photos = [None if photos.count(path) > 1 else path for path in photos]
    return {'kind': 'newProductsForm', 'formId': newFormId(),
            'currency': store['currency'], 'products': drafts,
            'photos': photos}


def _photoRequestForm(userId: str, storeId: str,
                      args: ShowPhotoRequestFormArgs) -> dict:
    store = _ownStore(userId, {'photoStyle': 1})
    if not store.get('photoStyle'):
        raise FormRefused(NO_PHOTO_STYLE_YET)
    return {'kind': 'photoRequestForm', 'formId': newFormId(),
            'count': args.count or 1}


def _phoneVerification(userId: str, storeId: str,
                       args: ShowPhoneVerificationArgs) -> dict:
    customer = db.read({'_id': userId}, CUSTOMERS,
                       projection={'phoneNumber': 1}, findOne=True)
    if customer is not None and customer.get('phoneNumber'):
        raise FormRefused(ALREADY_VERIFIED)
    return {'kind': 'phoneVerificationForm', 'formId': newFormId()}


def _deliveryPinForm(userId: str, storeId: str,
                     args: ShowDeliveryPinFormArgs) -> dict:
    delivery = storeDelivery(storeId)
    if 'delivery' not in offeredFulfillments(delivery):
        raise FormRefused(NOT_OFFERED['delivery'])
    if 'delivery' not in bookableFulfillments(delivery):
        raise FormRefused(NO_DELIVERY_ZONES_YET)
    store = db.read({'_id': storeId}, STORES,
                    projection={'location': 1, 'currency': 1}, findOne=True)
    if store is None:
        raise FormRefused(STORE_NOT_FOUND)
    location = store.get('location')
    return {
        'kind': 'deliveryPinForm',
        'formId': newFormId(),
        'storeLocation': _pin(location),
        'currency': store['currency'],
        'deliveryZones': delivery['deliveryZones'],
    }


def _pin(location: dict | None) -> dict | None:
    return ({'latitude': location['latitude'],
             'longitude': location['longitude']} if location else None)


def _deliveryZonesForm(userId: str, storeId: str,
                       args: ShowDeliveryZonesFormArgs) -> dict:
    store = _ownStore(userId, {'location': 1, 'deliveryZones': 1,
                               'currency': 1})
    return {
        'kind': 'deliveryZonesForm',
        'formId': newFormId(),
        'currency': store['currency'],
        'storeLocation': _pin(store.get('location')),
        'zones': ruledZones(store.get('deliveryZones')),
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
    'showStoreLocationForm': FormTool(
        ('admin', ),
        'Open a map under your reply where the owner pins the store. The '
        'pin they send comes as their next message: then call '
        'setUserStoreLocation with its latitude and longitude, then '
        'suggestStoreAddress.',
        ShowStoreLocationFormArgs, _locationForm),
    'showNewProductsForm': FormTool(
        ('admin', ),
        'Open a form under your reply where the owner lists new products: '
        'name, price, units in stock, an optional description and an '
        'optional photo, one row each. Prefill each row from what the owner '
        'gave in this conversation: their words and the files they attached '
        '(a photo, a screenshot, a price list, a PDF), one row per product '
        'they show. Take a name, price or description only where it is '
        'written or said; never guess a price or a stock count, and leave a '
        'field empty when it is not there. Keep a unit a price is for (per '
        'pack of 25, per box) in the name or description. Give a row '
        'imageUrl, the url of an attached image, only when that image is a '
        'photo of that one product alone, never a list or a screenshot of '
        'several. In your reply, say what you filled in from their files '
        'so the owner checks it. Use it to add products instead of asking '
        'each detail in chat. The owner may upload one photo per product '
        'there. Their list comes as their next message: then call '
        'addStoreProducts with it. For each product sent with an imageUrl, '
        'then call makeProductPhotoChoices with its new _id and that '
        'imageUrl, one product per turn, the first right after '
        'addStoreProducts; the owner picks a photo on its card, and you then '
        'call setProductPhoto and go on to the next.',
        ShowNewProductsFormArgs, _newProductsForm),
    'showPhotoRequestForm': FormTool(
        ('admin', ),
        "Open a form under your reply where the owner uploads 1 to 10 plain "
        'photos of one item, picks how many product photos to make (count, '
        'prefilled) and adds notes. The store needs a photo style first '
        '(updateMyStoreTheme). The upload comes as their next message: then '
        'call generateProductPhotos with its imageUrls, count and notes.',
        ShowPhotoRequestFormArgs, _photoRequestForm),
    'showPhoneVerification': FormTool(
        ('customer', ),
        'Open phone verification under your reply: the customer gets a code '
        'by text and enters it there. Use it when createOrder refuses '
        'because the phone number is not verified. When it is done, their '
        'next message says so: then call createOrder again.',
        ShowPhoneVerificationArgs, _phoneVerification),
    'showDeliveryPinForm': FormTool(
        ('customer', ),
        'Open a map under your reply where the customer pins where their '
        'delivery goes (or uses where they are). Use it before createOrder, '
        'once they chose delivery in chat; it refuses when the store cannot '
        'deliver. The map shows the store\'s delivery zones and takes a pin '
        'only inside one. The pin comes as their next message, with its zone '
        "and that zone's fee, delivery days, minimum order and free delivery "
        "total: tell them, then ask in chat for the rest of createOrder's "
        '`delivery` and call it with fulfillment delivery and the pin\'s '
        'latitude and longitude.',
        ShowDeliveryPinFormArgs, _deliveryPinForm),
    'showDeliveryZonesForm': FormTool(
        ('admin', ),
        'Open a map under your reply where the owner draws the zones the '
        'store delivers to (up to 10, never overlapping) and gives each its '
        'own delivery policy there: a name, a flat fee, the days a delivery '
        'takes, and optionally a minimum order and an order total from which '
        'delivery is free. Use it in the policy\'s delivery section once the '
        'owner said they deliver, instead of asking about areas or fees in '
        'chat: never ask for a zone\'s policy in chat. A store that delivers '
        'takes no delivery order until it has a zone. The map saves the '
        'zones itself; their next message lists them: then ask, optionally, '
        'for deliveryAreas, a name for them all (e.g. Metro Manila).',
        ShowDeliveryZonesFormArgs, _deliveryZonesForm),
}


def setsUpTheStore(toolset: str, name: str, content: str) -> bool:
    """Whether the tool `name` of `toolset`, whose tool message is
    `content`, just set the store up."""
    if toolset != SETUP_TOOLSET or name != SETS_UP_THE_STORE:
        return False
    try:
        result = json.loads(content)
    except ValueError:
        return False
    return isinstance(result, dict) and 'error' not in result


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


def _location(answer, component, userId) -> tuple[str, dict]:
    return (f'Pinned the store at {answer.latitude:.5f}, '
            f'{answer.longitude:.5f}',
            answer.model_dump(exclude={'formId'}))


def _newProducts(answer, component, userId) -> tuple[str, dict]:
    currency = component['currency']
    # Each photo is the user's own upload, checked here (nothing read) so
    # the model is never handed another URL; makeProductPhotoChoices
    # checks it again.
    for i, product in enumerate(answer.products):
        if product.imageUrl is not None:
            label = f'The photo of product {i + 1}'
            photoStorage.ownImagePath(
                userId, photoStorage.objectPathFromUrl(product.imageUrl,
                                                       label), label)
    lines = [f'- {p.name}, {p.price:g} {currency}, {p.inventory} in stock'
             + (f': {p.description}' if p.description else '')
             + (' (with a photo)' if p.imageUrl is not None else '')
             for p in answer.products]
    return ('Add these products:\n' + '\n'.join(lines),
            answer.model_dump(exclude={'formId'}))


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


def _productPhoto(answer, component, userId) -> tuple[str, dict]:
    photos = component['photos']
    position = next((i for i, photo in enumerate(photos)
                     if photo['_id'] == answer.photoId), None)
    if position is None or answer.productId != component['productId']:
        raise ValueError(NOT_ON_THE_CARD)
    if photos[position]['source'] == 'original':
        label = 'the original photo'
    else:
        styled = [p for p in photos[:position + 1]
                  if p['source'] == 'generated']
        label = f'styled photo {len(styled)}'
    return (f'Use {label} for {component["productName"]}',
            {'kind': 'productPhoto', 'productId': component['productId'],
             'photoId': answer.photoId})


def _zoneRules(zone: dict) -> dict:
    """A zone's delivery policy, without its shape."""
    return {rule: zone.get(rule) for rule in DELIVERY_ZONE_RULES}


def _deliveryPin(answer, component, userId) -> tuple[str, dict]:
    # The zones the customer saw; createOrder checks the store's own again.
    zone = deliveryZoneAt(answer.latitude, answer.longitude,
                          component.get('deliveryZones'))
    if zone is None:
        raise ValueError(OUTSIDE_DELIVERY_ZONES)
    return (f'Deliver to my pin at {answer.latitude:.5f}, '
            f'{answer.longitude:.5f} ({zone["name"]})',
            {**answer.model_dump(exclude={'formId'}),
             'zone': _zoneRules(zone)})


def _deliveryZones(answer, component, userId) -> tuple[str, dict]:
    # The map saved them (setMyDeliveryZones): what the store has now.
    store = _ownStore(userId, {'deliveryZones': 1})
    zones = ruledZones(store.get('deliveryZones'))
    if not zones:
        text = 'No delivery zones: no delivery orders until I draw one.'
    else:
        names = ', '.join(zone['name'] for zone in zones)
        text = (f'Saved {len(zones)} delivery zone'
                f'{"s" if len(zones) > 1 else ""}: {names}.')
    return text, {'kind': 'deliveryZones',
                  'zones': [_zoneRules(zone) for zone in zones]}


_ANSWER_TURNS = {
    'location': _location,
    'newProducts': _newProducts,
    'phoneVerified': _phoneVerified,
    'photoRequest': _photoRequest,
    'orderItems': _orderItems,
    'payment': _payment,
    'handover': _handover,
    'returnDecision': _returnDecision,
    'deliveryPin': _deliveryPin,
    'deliveryZones': _deliveryZones,
    'productPhoto': _productPhoto,
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

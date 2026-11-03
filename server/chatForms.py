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
tools, and adds the toolset an admin with no store yet chats with
(SETUP_TOOLSET: setting up the store only).

Who and where come only from the chat route (its JWT userId, the
conversation's storeId), never from the model or the answer.
"""

import json
import re
from dataclasses import dataclass
from typing import Callable, NamedTuple, get_args

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter,
                      ValidationError, field_validator)

from actions.CustomerActions import CUSTOMERS
from actions.OrderActions import (NO_DELIVERY_ZONES_YET, NOT_OFFERED,
                                  OUTSIDE_DELIVERY_ZONES,
                                  bookableFulfillments, offeredFulfillments,
                                  storeDelivery)
from actions.PhotoActions import (PROMPT_STORE_FIELDS, defaultPhotoPrompt,
                                  setUploadedProductPhotos,
                                  cardShots)
from actions.ProductActions import importStoreProducts
from actions.StoreActions import STORES, ownerQuery
import chatSpreadsheets
import geocoding
import photoStorage
import productImport
from mongoDb import db
from objects import (BRAND_COLOR_SUGGESTIONS_MAX, HEX_COLOR_PATTERN,
                     CHAT_ATTACHMENT_EXTENSIONS, CHAT_MESSAGE_MAX_LENGTH,
                     PHOTOS_PER_CALL, PRODUCT_BATCH_MAX,
                     PRODUCT_IMPORT_ROWS_MAX,
                     PRODUCT_DESCRIPTION_MAX_LENGTH, PRODUCT_INVENTORY_MAX,
                     PRODUCT_NAME_MAX_LENGTH, PRODUCT_PRICE_MAX,
                     ChatComponent, ChatFormAnswerTagged,
                     DELIVERY_ZONE_RULES, UploadedProductPhotosResponse,
                     deliveryZoneAt, ruledZones)
from services.__request import ServiceUnavailable
from toolArguments import ArgumentsRefused, refusedFields, validated
from utils import generateRandomString

# ChatActions.CHAT_TURNS (ChatActions imports this module).
CHAT_TURNS = 'ChatTurns'
# The toolset of an admin whose store is not set up yet: the store's
# details, and its brand colors picked on the color picker
# (showBrandColorsForm) or read from a logo (findColorsInPhoto, the one
# tool that needs no set-up store).
SETUP_TOOLSET = 'adminSetup'
# Its tools (chatToolsCreator.py copies them from the admin's).
SETUP_ROUTE_TOOLS = ('createUserStore', 'getUserStore', 'findColorsInPhoto')
# The setup tool that sets the store up: once it succeeds, the turn goes on
# with the admin's tools (the map to pin the store among them).
SETS_UP_THE_STORE = 'createUserStore'
# What a form tool tells the model.
FORM_SHOWN = ('The form is on screen under your reply. Ask the user to fill '
              'it in there; its answer comes as their next message.')
ALREADY_VERIFIED = 'The phone number is already verified'
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
    'makeProductPhotos': 'productPhotoOffer',
    'brandColors': 'photoColors',
    'brandColorsPicked': 'brandColorsForm',
    'placeOrder': 'checkoutSummary',
}

# How a payment method reads in the user's turn.
_LABELS = {
    'cash': 'cash', 'card': 'card', 'eWallet': 'e-wallet',
    'bankTransfer': 'bank transfer', 'check': 'check', 'crypto': 'crypto',
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
    hasInventory: bool | None = None
    description: str | None = None
    imageUrl: str | None = None


class ShowNewProductsFormArgs(_Args):
    model_config = ConfigDict(title='showNewProductsFormArguments')
    products: list[ProductDraftArgs] | None = Field(
        None, max_length=PRODUCT_BATCH_MAX)


class ShowNewProductsFromSpreadsheetArgs(_Args):
    model_config = ConfigDict(title='showNewProductsFromSpreadsheetArguments')
    fileName: str = Field(..., min_length=1,
                          description="The attached spreadsheet's name, as "
                          'its <attachment name="..."> says')
    sheet: str | None = Field(None, description='The sheet to read (an '
                              'XLSX); left out: the first with rows')
    nameColumn: str = Field(..., min_length=1, description='The header of '
                            'the column with the product names')
    priceColumn: str = Field(..., min_length=1, description='The header of '
                             'the column with the prices')
    inventoryColumn: str | None = Field(
        None, description='The header of the column with the units in '
        'stock; left out when there is none (every row then has no stock '
        'count, unlimited, until the owner ticks Track stock)')
    descriptionColumn: str | None = Field(
        None, description='The header of the column with a description; '
        'left out when there is none')
    firstRow: int | None = Field(
        None, description='The row number (as the spreadsheet numbers its '
        'rows) to start from; left out: the row under the headers')
    lastRow: int | None = Field(
        None, description='The row number to stop at; at most '
        f'{PRODUCT_IMPORT_ROWS_MAX} product rows are read at once')

    @field_validator('sheet', 'inventoryColumn', 'descriptionColumn',
                     mode='before')
    @classmethod
    def _blankIsNone(cls, value):
        """"" (or spaces) is a column or sheet left out."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class ShowPhotoRequestFormArgs(_Args):
    model_config = ConfigDict(title='showPhotoRequestFormArguments')


class ShowPhoneVerificationArgs(_Args):
    model_config = ConfigDict(title='showPhoneVerificationArguments')


class ShowDeliveryPinFormArgs(_Args):
    model_config = ConfigDict(title='showDeliveryPinFormArguments')


class ShowDeliveryZonesFormArgs(_Args):
    model_config = ConfigDict(title='showDeliveryZonesFormArguments')


class ShowBrandColorsFormArgs(_Args):
    model_config = ConfigDict(title='showBrandColorsFormArguments')
    suggestions: list[str] | None = Field(
        None, description=f'Up to {BRAND_COLOR_SUGGESTIONS_MAX} hex colors '
        'like #1A73E8 that suit the store, shown as swatches to tap')


class FormRefused(Exception):
    """A form tool that cannot open its form: its message goes to the model
    as the tool's error."""


class FormContext(NamedTuple):
    """What a form tool that reads the turn's files gets from the chat
    route (FormTool.withContext): the conversation, this message's files
    (ChatAttachment dicts: not stored yet), the turn's deadline
    (time.monotonic()), and readSheet(label, path, sheet), which reads one
    of the caller's own chat spreadsheets and converts it
    (chatSpreadsheets.importText; only
    `sheet`'s rows when one is named), its bytes held in the process's
    bytes in flight only while it is read: (its SheetText, its content
    type). A ValueError names `label` when it cannot be read, SlotsBusy or
    a ServiceUnavailable says the process or Storage cannot read it now."""
    conversationId: str
    attachments: tuple
    deadline: float
    readSheet: Callable[[str, str, str | None], tuple]


class FormOpened(NamedTuple):
    """A form tool's form, with its whole tool message for the model
    (`result`, in place of FORM_SHOWN alone)."""
    form: dict
    result: dict


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
    """The Storage path of a prefill's photo: the owner's own JPG, PNG or
    WebP (one they attached), else None (left for the owner to add)."""
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
            # Only false prefills an unlimited product: anything else is
            # the default, counted stock.
            'hasInventory': draft.hasInventory is not False,
            'description': _clip(draft.description,
                                 PRODUCT_DESCRIPTION_MAX_LENGTH),
        })
        photos.append(_draftPhoto(userId, draft.imageUrl))
    # A photo on several rows shows them all (a list, a screenshot): no
    # row's own photo.
    photos = [None if photos.count(path) > 1 else path for path in photos]
    return {'kind': 'newProductsForm', 'formId': newFormId(),
            'currency': store['currency'], 'products': drafts,
            'rowPhotos': photos}


def _photoRequestForm(userId: str, storeId: str,
                      args: ShowPhotoRequestFormArgs) -> dict:
    store = _ownStore(userId, PROMPT_STORE_FIELDS)
    return {'kind': 'photoRequestForm', 'formId': newFormId(),
            'defaultPrompt': defaultPhotoPrompt(store),
            'shots': cardShots(store)}


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


def _suggestedColors(suggestions: list | None) -> list[str]:
    """The model's suggestions the picker shows: the hex colors among
    them, uppercase, each once, at most BRAND_COLOR_SUGGESTIONS_MAX
    (anything else is dropped, never refused)."""
    colors = []
    for value in suggestions or []:
        if not isinstance(value, str):
            continue
        value = value.strip().upper()
        if re.fullmatch(HEX_COLOR_PATTERN, value) and value not in colors:
            colors.append(value)
    return colors[:BRAND_COLOR_SUGGESTIONS_MAX]


def _brandColorsForm(userId: str, storeId: str,
                     args: ShowBrandColorsFormArgs) -> dict:
    # The store a sign-up creates (createEmptyStore) is there before setup.
    store = _ownStore(userId, {'primaryColor': 1, 'secondaryColor': 1})
    return {
        'kind': 'brandColorsForm',
        'formId': newFormId(),
        'primaryColor': store.get('primaryColor'),
        'secondaryColor': store.get('secondaryColor'),
        'suggestions': _suggestedColors(args.suggestions),
    }


# How many of the conversation's latest turns with files a spreadsheet
# import looks through for the file it names.
IMPORT_TURNS_SEARCHED = 50
IMPORT_FILE_LABEL = 'The spreadsheet'
IMPORT_FILE_NOT_FOUND = ('No spreadsheet named "{name}" was attached in this '
                         'chat. Its spreadsheets: {names}')
IMPORT_NO_SPREADSHEET = 'No spreadsheet was attached in this chat'
IMPORT_BUSY = ('Spreadsheet reading is busy right now: ask the owner to try '
               'again in a minute')
IMPORT_SHOWN = (FORM_SHOWN + ' Its rows are read from the spreadsheet: tell '
                'the owner how many rows are on it, which rows were left out '
                'and why, and to check them and submit. Their submit saves '
                'the products and puts on the photos they add itself: their '
                'next message says how many were added; never call '
                'addStoreProducts or setUploadedProductPhotos for them.')
ONCE_PER_TURN = ('{name} runs once per message: tell the owner to check '
                 'the form shown, then ask for the next rows in a new '
                 'message')


def _conversationSheets(context: FormContext) -> list[dict]:
    """The spreadsheets of this message, then of the conversation's latest
    IMPORT_TURNS_SEARCHED turns with files, newest first (ChatAttachment
    dicts). The conversation is the caller's (the chat route holds its
    lock); each file is read as the caller's own (FormContext.readFile)."""
    turns = db.aggregate([
        {'$match': {'conversationId': context.conversationId,
                    'attachments.0': {'$exists': True}}},
        {'$sort': {'seq': -1}},
        {'$limit': IMPORT_TURNS_SEARCHED},
        {'$project': {'_id': 0, 'attachments': 1}},
    ], CHAT_TURNS)
    attachments = [*reversed(context.attachments),
                   *(attachment for turn in turns
                     for attachment in reversed(turn['attachments']))]
    return [attachment for attachment in attachments
            if chatSpreadsheets.isSheet(attachment['contentType'])]


def _newProductsFromSpreadsheet(userId: str, storeId: str,
                                args: ShowNewProductsFromSpreadsheetArgs,
                                context: FormContext) -> FormOpened:
    store = _ownStore(userId, {'_id': 1, 'currency': 1})
    sheets = _conversationSheets(context)
    if not sheets:
        raise FormRefused(IMPORT_NO_SPREADSHEET)
    wanted = args.fileName.strip().casefold()
    attachment = next((sheet for sheet in sheets
                       if sheet['name'].casefold() == wanted), None)
    if attachment is None:
        names = list(dict.fromkeys(sheet['name'] for sheet in sheets))
        raise FormRefused(IMPORT_FILE_NOT_FOUND.format(
            name=args.fileName[:productImport.LISTED_LENGTH_MAX],
            names=', '.join(f'"{name}"' for name in names)))
    try:
        sheet, contentType = context.readSheet(
            IMPORT_FILE_LABEL, attachment['path'], args.sheet)
        imported = productImport.importRows(
            sheet.text, contentType, sheet.cutBy is not None,
            attachment['name'], store['_id'],
            productImport.Mapping(args.nameColumn, args.priceColumn,
                                  args.inventoryColumn,
                                  args.descriptionColumn),
            args.sheet, args.firstRow, args.lastRow)
    except (chatSpreadsheets.SlotsBusy, ServiceUnavailable):
        raise FormRefused(IMPORT_BUSY) from None
    except ValueError as error:
        raise FormRefused(str(error)) from None
    form = {'kind': 'newProductsForm', 'formId': newFormId(),
            'currency': store['currency'], 'products': imported.drafts,
            'rowPhotos': [], 'spreadsheetImport': imported.summary}
    return FormOpened(form, productImport.aiResult(imported, IMPORT_SHOWN))


@dataclass(frozen=True)
class FormTool:
    toolsets: tuple[str, ...]
    description: str
    argsModel: type[BaseModel]
    # build(userId, storeId, args), with withContext a FormContext last:
    # the form, or a FormOpened (the form and the model's tool message).
    build: Callable[..., dict | FormOpened]
    withContext: bool = False
    # Refused (ONCE_PER_TURN) when the model calls it again in one turn.
    oncePerTurn: bool = False
    # What the chat shows while it runs (chatThinking.note): only a form
    # tool that takes a while has any.
    thinkingText: tuple[str, ...] = ()

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
        'they show (a spreadsheet of more than a few products: '
        'showNewProductsFromSpreadsheet instead, which reads its rows '
        'itself). Take a name, price or description only where it is '
        'written or said; never guess a price or a stock count, and leave a '
        'field empty when it is not there. Set a row hasInventory false only '
        'when the owner says the item has no stock count (unlimited: a '
        'service, made to order); the form has a checkbox for it. Keep a '
        'unit a price is for (per pack of 25, per box) in the name or '
        'description. Give a row '
        'imageUrl, the url of an attached image, only when that image is a '
        'photo of that one product alone, never a list or a screenshot of '
        'several. In your reply, say what you filled in from their files '
        'so the owner checks it. Use it to add products instead of asking '
        'each detail in chat. The owner may upload one photo per product '
        'there. Their list comes as their next message: then call '
        'addStoreProducts with it, then call setUploadedProductPhotos with '
        'every product sent with an imageUrl and its new _id, and in one '
        'line tell the owner their photos are on and that they can make '
        'better photos from them on the card. Make photos '
        '(makeProductPhotoChoices) only when they ask.',
        ShowNewProductsFormArgs, _newProductsForm),
    'showNewProductsFromSpreadsheet': FormTool(
        ('admin', ),
        'Open the new products form under your reply with the rows of a '
        'spreadsheet the owner attached (CSV or XLSX), read by the server: '
        'use it whenever the owner wants products added from a spreadsheet '
        'with more than a few products, instead of showNewProductsForm or '
        'addStoreProducts, and never write its rows yourself. Give the '
        "file's name and which column holds the names, the prices and, "
        'when there are, the stock and a description, by their headers as '
        'the spreadsheet shows them; ask the owner which column is which '
        'only when the headers leave it unclear. An empty stock cell, or '
        '"unlimited", is a product with no stock count; prices may carry a '
        'currency or thousands separators. It reads at most '
        f'{PRODUCT_IMPORT_ROWS_MAX} product rows at a time (firstRow, '
        'lastRow: row numbers as the spreadsheet shows them), once per '
        'message: when nextFirstRow is not null, tell the owner the rest '
        'can be imported next, from that row. It returns how many rows are '
        'on the form, the rows left out and why (no name, a bad price, a '
        "name repeated in the file or already in the store), and the sheet's "
        'headers and sheets. If it refuses (no such file, sheet or column, '
        'no row to import), fix the call from what it says, or relay it to '
        'the owner.',
        ShowNewProductsFromSpreadsheetArgs, _newProductsFromSpreadsheet,
        withContext=True, oncePerTurn=True,
        thinkingText=('Reading your spreadsheet',
                      'Lining up your products',
                      'Checking each row')),
    'showPhotoRequestForm': FormTool(
        ('admin', ),
        "Open a form under your reply where the owner uploads 1 to 10 plain "
        f'photos of one item; {PHOTOS_PER_CALL} product photos are made from '
        'them, from a prompt they can edit there. The upload comes as their '
        'next message: then call generateProductPhotos with its imageUrls '
        'and its prompt copied character for character (never shortened, '
        'translated or reworded; left out when it is null: an unchanged '
        "box means the store's current default prompt).",
        ShowPhotoRequestFormArgs, _photoRequestForm),
    'showPhoneVerification': FormTool(
        ('customer', ),
        'Open phone verification under your reply: the customer gets a code '
        'by text and enters it there. Use it when previewMyCheckout or '
        'checkoutMyCart refuses because the phone number is not verified. '
        'When it is done, their next message says so: then call that tool '
        'again.',
        ShowPhoneVerificationArgs, _phoneVerification),
    'showDeliveryPinForm': FormTool(
        ('customer', ),
        'Open a map under your reply where the customer pins where their '
        'delivery goes (or uses where they are). Use it before '
        'checkoutMyCart, '
        'once they chose delivery in chat; it refuses when the store cannot '
        'deliver. The map shows the store\'s delivery zones and takes a pin '
        'only inside one. The pin comes as their next message, with its zone '
        "and that zone's fee, delivery days, minimum order and free delivery "
        'total, and a suggestedAddress from the map (null when there is '
        "none): tell them, then ask in chat for the rest of checkoutMyCart's "
        '`delivery` and call previewMyCheckout with fulfillment delivery and '
        'the pin\'s latitude and longitude.',
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
        'how the store delivers (deliveryMethod, in their own words, e.g. '
        'own rider or a courier) and for deliveryAreas, a name for them all '
        '(e.g. Metro Manila).',
        ShowDeliveryZonesFormArgs, _deliveryZonesForm),
    'showBrandColorsForm': FormTool(
        ('admin', SETUP_TOOLSET),
        "Open a color picker under your reply where the owner picks the "
        "store's two brand colors, primary and secondary (the shop is drawn "
        'in them): a color square, a hue slider and a hex field each. Give '
        f'it up to {BRAND_COLOR_SUGGESTIONS_MAX} `suggestions`, hex colors '
        'that suit what the store sells, shown as swatches to tap. Use it '
        'whenever the brand colors are asked or changed, instead of asking '
        'for hex codes in chat (a code the owner types in chat still '
        'counts). Their picks come as their next message: that is their '
        'choice, so take both at once, never asking to confirm them: while '
        'the store is being set up, pass them to createUserStore; once it '
        'is set up, save them with updateMyStoreTheme.',
        ShowBrandColorsFormArgs, _brandColorsForm),
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


# The forms that show a map. A reply that mentions a map with none of them
# called in its turn told the user about a map they cannot see.
MAP_FORM_TOOLS = ('showStoreLocationForm', 'showDeliveryZonesForm',
                  'showDeliveryPinForm')
_MENTIONS_A_MAP = re.compile(r'\bmaps?\b', re.IGNORECASE)
# Words that point the user to something shown under the reply. Cards and
# forms come only from the tools called in the turn, so with none called
# nothing is shown, however much the model read earlier in the chat.
_POINTS_TO_A_CARD = re.compile(
    r'\bhere you go\b|\bhere it is\b|\bhere they are\b|\bbelow\b'
    r'|\b(?:see|on|in) the card\b', re.IGNORECASE)


def nothingShownNudge(toolset: str, reply: str,
                      toolNames: list[str]) -> tuple[str, str] | None:
    """(kind, note) sending the model back when its `reply` points the user
    to something the turn did not show (`toolNames`, the tools the turn
    called): 'map' when it mentions a map with none of `toolset`'s map forms
    called, 'card' when it points to a card or form ("here you go",
    "below") with no tool called at all. None otherwise. The note lets the
    model reply again in text, so a false alarm costs one more model call,
    never a card nobody asked for."""
    mapTools = [name for name in MAP_FORM_TOOLS
                if name in formToolNames(toolset)]
    if (mapTools and _MENTIONS_A_MAP.search(reply)
            and not any(name in toolNames for name in mapTools)):
        return 'map', (
            'Your reply mentions a map, but you opened none this turn, so '
            'the user sees no map. If they need one now, call '
            f'{" or ".join(mapTools)}; otherwise reply again without '
            'saying a map is shown. Never say a map is shown without '
            'calling its tool.')
    if not toolNames and _POINTS_TO_A_CARD.search(reply):
        return 'card', (
            'Your reply points the user to something shown under it, but '
            'you called no tool this turn, so nothing is shown: a card '
            'shows only what a tool returned in this turn. If the user '
            'wants to see it, call its tool now, even if you read it '
            'earlier in the chat; otherwise reply again in text without '
            'pointing to a card.')
    return None


def formToolNames(toolset: str) -> set[str]:
    return {name for name, tool in FORM_TOOLS.items()
            if toolset in tool.toolsets}


def formToolSchemas(toolset: str) -> list[dict]:
    return [FORM_TOOLS[name].schema(name)
            for name in sorted(formToolNames(toolset))]


def runFormTool(toolset: str, name: str, argumentsJson: str, userId: str,
                storeId: str, context: FormContext | None = None
                ) -> tuple[str, dict | None]:
    """The tool message's content for the model's call of the form tool
    `name`, and the form it opened (None when it refused). A tool that
    reads the turn's files (FormTool.withContext) gets `context`, and
    refuses without one."""
    tool = FORM_TOOLS.get(name)
    if tool is None or toolset not in tool.toolsets:
        return json.dumps({'error': f'Unknown tool {name}'}), None
    try:
        args = validated(tool.argsModel, name, argumentsJson, 'form')
    except ArgumentsRefused as refusal:
        return json.dumps({'error': str(refusal)}), None
    try:
        if tool.withContext:
            if context is None:
                raise FormRefused(f'{name} works in the chat only')
            built = tool.build(userId, storeId, args, context)
        else:
            built = tool.build(userId, storeId, args)
    except FormRefused as refusal:
        return json.dumps({'error': str(refusal)}), None
    if isinstance(built, FormOpened):
        return (json.dumps(built.result, ensure_ascii=False),
                _component(built.form))
    return json.dumps({'result': FORM_SHOWN}), _component(built)


def oncePerTurn(name: str) -> bool:
    """Whether the form tool `name` runs at most once per turn."""
    tool = FORM_TOOLS.get(name)
    return tool is not None and tool.oncePerTurn


def formThinkingTexts() -> dict[str, tuple[str, ...]]:
    """The thinking texts of the form tools that have any, by name."""
    return {name: tool.thinkingText for name, tool in FORM_TOOLS.items()
            if tool.thinkingText}


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
            fields=refusedFields(error, 'form'))) from None


def _answers(component: dict, formId: str) -> bool:
    """Whether `formId` is the component's: its own, or one of a photo
    offer's rows (each answered on its own)."""
    if component.get('formId') == formId:
        return True
    return component.get('kind') == 'productPhotoOffer' and any(
        row.get('formId') == formId
        for row in component.get('products') or [])


def _answeredComponent(conversationId: str, answer: BaseModel) -> dict:
    """The component `answer` answers, in this conversation: refused when
    it is not there, is of another kind, or was answered already (a photo
    offer's row: that row)."""
    turn = db.read({'conversationId': conversationId,
                    '$or': [{'components.formId': answer.formId},
                            {'components.products.formId': answer.formId}]},
                   CHAT_TURNS, projection={'components': 1}, findOne=True)
    component = next((c for c in (turn or {}).get('components', [])
                      if _answers(c, answer.formId)), None)
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


def _checkRowPhotos(answer, userId: str) -> None:
    """Each row's photo is the user's own upload, checked here (nothing
    read), so neither the model nor the server is handed another URL."""
    for i, product in enumerate(answer.products):
        if product.imageUrl is not None:
            label = f'The photo of product {i + 1}'
            photoStorage.ownImagePath(
                userId, photoStorage.objectPathFromUrl(product.imageUrl,
                                                       label), label)


def _putOnPhotos(userId: str, photos: list[dict]) -> tuple[int, int]:
    """Put each imported product's photo on it ({productId, imageUrl}), as
    setUploadedProductPhotos does (its checks, quota and copies), at most
    PRODUCT_BATCH_MAX a call: (how many are on, how many are not)."""
    on = notOn = 0
    for start in range(0, len(photos), PRODUCT_BATCH_MAX):
        chunk = photos[start:start + PRODUCT_BATCH_MAX]
        try:
            done = setUploadedProductPhotos(UploadedProductPhotosResponse,
                                            userId, chunk)
        except (ValueError, ServiceUnavailable):
            notOn += len(chunk)
            continue
        on += len(done['products'])
        notOn += len(done['notSaved'])
    return on, notOn


def _plural(count: int, word: str) -> str:
    return f'{count} {word}{"" if count == 1 else "s"}'


def _importedProducts(answer, component, userId) -> tuple[str, dict]:
    """A spreadsheet form's submit (its spreadsheetImport): the products
    are saved here, never by the model (which would write every row
    again), skipping what cannot be added, and each row's photo is put on
    its product (_putOnPhotos). The model reads only the counts and the
    reasons (each row by its place on the form, formRow)."""
    rows = [product.model_dump() for product in answer.products]
    saved = importStoreProducts(userId, rows, component['formId'])
    added, skipped = saved['added'], saved['skipped']
    on, notOn = _putOnPhotos(userId, [
        {'productId': row['_id'],
         'imageUrl': rows[row['position'] - 1]['imageUrl']}
        for row in added if rows[row['position'] - 1]['imageUrl'] is not None])
    text = (f'Added {_plural(len(added), "product")} from '
            f'{component["spreadsheetImport"]["fileName"]}')
    if skipped:
        text += f'; {len(skipped)} not added'
    if on or notOn:
        text += f'; {_plural(on, "photo")} put on'
        if notOn:
            text += f', {notOn} not'
    return (text + '.', {
        'kind': 'newProductsSaved',
        'added': len(added),
        'notAdded': len(skipped),
        'notAddedRows': [{'formRow': row['position'], 'name': row['name'],
                          'reason': row['reason']}
                         for row in skipped[:productImport.AI_SKIPPED_MAX]],
        'nextFirstRow': component['spreadsheetImport']['nextFirstRow'],
        'photosOn': on,
        'photosNotOn': notOn,
    })


def _newProducts(answer, component, userId) -> tuple[str, dict]:
    _checkRowPhotos(answer, userId)
    if component.get('spreadsheetImport') is not None:
        return _importedProducts(answer, component, userId)
    if len(answer.products) > PRODUCT_BATCH_MAX:
        raise ValueError(FORM_ANSWER_INVALID.format(fields='products'))
    currency = component['currency']
    # setUploadedProductPhotos checks each photo again.
    lines = [f'- {p.name}, {p.price:g} {currency}, '
             + (f'{p.inventory} in stock' if p.hasInventory
                else 'no inventory (unlimited)')
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


def _ownPrompt(prompt: str | None, defaultPrompt: str | None) -> str | None:
    """The prompt a photo answer sent, as the model passes it on: None when
    it is blank or the default unchanged (the tool then writes the default
    itself), else stripped. The client applies the same rule before sending
    (client/components/chat/cards/PhotoPromptBox.tsx, promptAnswer): change
    both together."""
    prompt = (prompt or '').strip() or None
    if prompt is not None and prompt == (defaultPrompt or '').strip():
        return None
    return prompt


def _photoRequest(answer, component, userId) -> tuple[str, dict]:
    prompt = _ownPrompt(answer.prompt, component.get('defaultPrompt'))
    text = (f'Make {PHOTOS_PER_CALL} product photos from my '
            f'{len(answer.imageUrls)} photo'
            f'{"s" if len(answer.imageUrls) > 1 else ""}'
            + (' with my prompt.' if prompt is not None else '.'))
    return text, {'kind': 'photoRequest', 'imageUrls': answer.imageUrls,
                  'prompt': prompt}


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
    # A card saved before payment options has none, so it takes no answer.
    if not any(option['method'] == answer.paymentMethod
               and option['provider'] == answer.paymentProvider
               for option in component.get('paymentOptions') or []):
        raise ValueError(NOT_ON_THE_CARD)
    order = component['order']
    how = _label(answer.paymentMethod)
    if answer.paymentProvider is None:
        how += ', paid to the store'
    else:
        how += ' online'
    return (f'Pay {order["total"]:g} {order["currency"]} by {how}',
            {'kind': 'payment', 'orderId': order['_id'],
             'paymentMethod': answer.paymentMethod,
             'paymentProvider': answer.paymentProvider})


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


def _makeProductPhotos(answer, component, userId) -> tuple[str, dict]:
    """A photo offer row's "Make photos": makeProductPhotoChoices for that
    product, with the prompt the admin sent (None: the default one)."""
    row = next(row for row in component['products']
               if row['formId'] == answer.formId)
    if answer.productId != row['productId']:
        raise ValueError(NOT_ON_THE_CARD)
    prompt = _ownPrompt(answer.prompt, row['defaultPrompt'])
    return (f'Make photos of {row["productName"]}'
            + (' with my prompt' if prompt is not None else ''),
            {'kind': 'makeProductPhotos', 'productId': row['productId'],
             'prompt': prompt})


def _brandColors(answer, component, userId) -> tuple[str, dict]:
    picks = [(role, color.upper()) for role, color in
             (('primary', answer.primaryColor),
              ('secondary', answer.secondaryColor)) if color is not None]
    if not picks:
        raise ValueError(NOTHING_PICKED)
    _unique([color for _, color in picks])
    shown = {color['hex'] for color in component['colors']}
    if any(color not in shown for _, color in picks):
        raise ValueError(NOT_ON_THE_CARD)
    text = ' and '.join(f'{color} as the {role} color'
                        for role, color in picks)
    return (f'Use {text}',
            {'kind': 'brandColors', **{f'{role}Color': color
                                       for role, color in picks}})


def _brandColorsPicked(answer, component, userId) -> tuple[str, dict]:
    """The color picker's two colors: any hex colors, not the same one."""
    primary = answer.primaryColor.upper()
    secondary = answer.secondaryColor.upper()
    _unique([primary, secondary])
    return (f'Use {primary} as the primary color and {secondary} as the '
            'secondary color',
            {'kind': 'brandColorsPicked', 'primaryColor': primary,
             'secondaryColor': secondary})


def _placeOrder(answer, component, userId) -> tuple[str, dict]:
    """The summary's Place order: checkoutMyCart with the fulfillment and
    delivery the card was priced on (checkoutMyCart prices the cart again,
    as it is then)."""
    delivery = None
    if component['fulfillment'] == 'delivery':
        delivery = {'address': component['deliveryAddress'],
                    'latitude': component['deliveryLatitude'],
                    'longitude': component['deliveryLongitude'],
                    'notes': component['deliveryNotes']}
    how = ('pickup' if delivery is None
           else f'delivery to {component["deliveryAddress"]}')
    return (f'Place my order: {_itemsText(component["items"])} for '
            f'{component["total"]:g} {component["currency"]}, {how}',
            {'kind': 'placeOrder', 'fulfillment': component['fulfillment'],
             'delivery': delivery})


def _zoneRules(zone: dict) -> dict:
    """A zone's delivery policy, without its shape."""
    return {rule: zone.get(rule) for rule in DELIVERY_ZONE_RULES}


def _deliveryPin(answer, component, userId) -> tuple[str, dict]:
    # The zones the customer saw; createOrder checks the store's own again.
    zone = deliveryZoneAt(answer.latitude, answer.longitude,
                          component.get('deliveryZones'))
    if zone is None:
        raise ValueError(OUTSIDE_DELIVERY_ZONES)
    # Google's street address for the pin, only a suggestion the customer
    # confirms or corrects (None when there is none: ask for it).
    return (f'Deliver to my pin at {answer.latitude:.5f}, '
            f'{answer.longitude:.5f} ({zone["name"]})',
            {**answer.model_dump(exclude={'formId'}),
             'zone': _zoneRules(zone),
             'suggestedAddress': geocoding.reverseGeocode(
                 answer.latitude, answer.longitude)})


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
    'makeProductPhotos': _makeProductPhotos,
    'brandColors': _brandColors,
    'brandColorsPicked': _brandColorsPicked,
    'placeOrder': _placeOrder,
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

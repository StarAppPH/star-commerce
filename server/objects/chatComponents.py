"""Chat components: what the app shows under an assistant's reply.

A turn's components are cards and forms. A card shows what a tool returned
(products, orders, returns, a policy, photos, the store), built by the server
from the tool's own result (chatComponents.py), never written by the model. A
form asks the user for details: the model opens one with a form tool
(chatForms.py). A card that takes a choice (products to order, a payment
method, orders to hand over, a return to decide) is answered like a form.

Every answerable component carries a formId. Its answer goes back through the
chat route (`form`, one of the *Answer models below) as the user's next turn,
and the model acts on it. A component is stored with its turn as it was sent:
what the user saw then.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
from typing import Annotated, Literal, Union

from pydantic import Field

from objects.base import AppModel, ResponseModel
from objects.order import (ORDER_ITEMS_MAX, CancelReason, NewOrderItem,
                           OrderDeliveryRow, OrderItemRow, OrderStatus,
                           PaymentMethod)
from objects.photo import (PHOTO_NOTES_MAX_LENGTH, PHOTO_REFERENCES_MAX,
                           PHOTOS_PER_CALL_MAX, PHOTO_CHOICES_GENERATED,
                           GeneratedPhotoRow, PhotoChoiceRow)
from objects.policy import (AcceptedPaymentMethods, AcceptsReturns,
                            AdditionalTerms, DefectReportDays, DeliveryAreas,
                            DeliveryOptions,
                            HandoverDays, NonReturnableItems,
                            PaidOrderCancellationAnswer,
                            PaidOrderCancellationHours, RefundMethodAnswer,
                            ReturnConditions, ReturnShippingPaidBy,
                            ReturnWindowDays, SendsMarketingMessages)
from objects.product import PRODUCT_BATCH_MAX, NewProduct
from objects.returns import (RETURN_REJECT_REASON_MAX_LENGTH,
                             ReturnItemRow, ReturnReason, ReturnStatus)
from objects.store import (DELIVERY_ZONES_MAX, DeliveryZoneResponse,
                           DeliveryZoneRule,
                           Latitude, Longitude, StoreCurrency,
                           StoreLocationResponse)

# The most components one turn shows: past it, the earliest are dropped.
CHAT_COMPONENTS_PER_TURN_MAX = 6
# The most rows a card holds (the list tools return at most 50).
CHAT_CARD_ROWS_MAX = 50
# A formId: generateRandomString's length, with room to spare.
CHAT_FORM_ID_MAX_LENGTH = 64
# The longest id a form answer may name (a product, an order, a return).
CHAT_ANSWER_ID_MAX_LENGTH = 64
# A photo URL in a photo request.
CHAT_PHOTO_URL_MAX_LENGTH = 2048

FormId = Annotated[str, Field(min_length=1, max_length=CHAT_FORM_ID_MAX_LENGTH,
                              description='Answer it with this id')]


# ──────────────────────────────────────────────
# Cards: built from a tool's result
# ──────────────────────────────────────────────

class ChatShopProductRow(ResponseModel):
    """A product on a shop card."""
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., description='Product name')
    description: str | None = Field(..., description='What the product is')
    price: float = Field(..., ge=0, description="Price in the store's currency")
    inventory: int = Field(..., ge=0,
                           description='Units free to sell; 0 is sold out')
    photoUrl: str | None = Field(..., description='Its photo; null when '
                                 'it has none')


class ShopProductsCard(ResponseModel):
    """The products a shopper asked about. Answered with OrderItemsAnswer:
    the quantities the shopper picked."""
    kind: Literal['shopProducts'] = Field(..., description='The card')
    formId: FormId
    currency: StoreCurrency = Field(..., description='Every price is in it')
    products: list[ChatShopProductRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The products')


class ChatStoreProductRow(ResponseModel):
    """A product on the admin's product card."""
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., description='Product name')
    price: float = Field(..., ge=0, description="Price in the store's currency")
    inventory: int = Field(..., ge=0, description='Units free to sell')
    reservedQuantity: int = Field(...,
                                  ge=0,
                                  description='Units unpaid orders hold')
    isAvailable: bool = Field(..., description='Shown to customers')
    soldCount: int = Field(..., ge=0, description='Units sold so far')
    photoUrl: str | None = Field(..., description='Its photo; null when '
                                 'it has none')


class StoreProductsCard(ResponseModel):
    """The admin's products a tool listed, added or changed."""
    kind: Literal['storeProducts'] = Field(..., description='The card')
    currency: StoreCurrency = Field(..., description='Every price is in it')
    products: list[ChatStoreProductRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The products')


class ChatStoreOrderRow(OrderDeliveryRow):
    """An order on the admin's order card."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(..., description="The order's currency")
    contactNumber: str = Field(..., description="The customer's number")
    createdAt: datetime.datetime = Field(..., description='When it was placed')
    paymentMethod: PaymentMethod | None = Field(...,
                                                description='How it was paid')
    paidAt: datetime.datetime | None = Field(...,
                                             description='When it was paid')
    completedAt: datetime.datetime | None = Field(
        ..., description='When it was handed over')
    cancelReason: CancelReason | None = Field(...,
                                              description='Why it was cancelled')


class StoreOrdersCard(ResponseModel):
    """The store's orders a tool listed or changed. With paid orders it is
    answered with HandoverAnswer (formId set); otherwise formId is null."""
    kind: Literal['storeOrders'] = Field(..., description='The card')
    formId: FormId | None = Field(..., description='Set when it has paid '
                                  'orders to hand over')
    orders: list[ChatStoreOrderRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The orders')


class ChatMyOrderRow(OrderDeliveryRow):
    """An order on a shopper's order card."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(..., description="The order's currency")
    createdAt: datetime.datetime = Field(..., description='When it was placed')
    expiresAt: datetime.datetime = Field(
        ..., description='Unpaid by then, it is cancelled')
    paymentMethod: PaymentMethod | None = Field(...,
                                                description='How it was paid')
    paidAt: datetime.datetime | None = Field(...,
                                             description='When it was paid')
    completedAt: datetime.datetime | None = Field(
        ..., description='When it was handed over')
    cancelReason: CancelReason | None = Field(...,
                                              description='Why it was cancelled')
    refundedAt: datetime.datetime | None = Field(
        ..., description='When its payment was refunded')


class MyOrdersCard(ResponseModel):
    """The shopper's orders a tool listed, paid or cancelled."""
    kind: Literal['myOrders'] = Field(..., description='The card')
    orders: list[ChatMyOrderRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The orders')


class CheckoutCard(ResponseModel):
    """The order a shopper just placed, unpaid. Answered with PaymentAnswer:
    the payment method the shopper picked."""
    kind: Literal['checkout'] = Field(..., description='The card')
    formId: FormId
    order: ChatMyOrderRow = Field(..., description='The order to pay')
    paymentMethods: list[PaymentMethod] = Field(
        ..., description='The ways the shopper can pay it (never none)')


class ChatStoreReturnRow(ResponseModel):
    """A return on the admin's return card."""
    id: str = Field(..., alias='_id', description='Returns _id')
    orderId: str = Field(..., description='The Orders _id')
    status: ReturnStatus = Field(..., description='Where the return is')
    items: list[ReturnItemRow] = Field(..., description='What goes back')
    reason: ReturnReason = Field(..., description='Why it goes back')
    note: str | None = Field(..., description="The customer's note")
    refundAmount: float = Field(..., ge=0, description='What it refunds')
    currency: StoreCurrency = Field(..., description="The order's currency")
    createdAt: datetime.datetime = Field(...,
                                         description='When it was requested')
    contactNumber: str = Field(..., description="The customer's number")
    rejectReason: str | None = Field(...,
                                     description='Why the store rejected it')
    restocked: bool | None = Field(...,
                                   description='Whether the units went back')


class StoreReturnsCard(ResponseModel):
    """The store's returns a tool listed or decided. With requested returns
    it is answered with ReturnDecisionAnswer (formId set); otherwise formId
    is null."""
    kind: Literal['storeReturns'] = Field(..., description='The card')
    formId: FormId | None = Field(..., description='Set when it has returns '
                                  'waiting for a decision')
    returns: list[ChatStoreReturnRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The returns')


class ChatMyReturnRow(ResponseModel):
    """A return on a shopper's return card."""
    id: str = Field(..., alias='_id', description='Returns _id')
    status: ReturnStatus = Field(..., description='Where the return is')
    items: list[ReturnItemRow] = Field(..., description='What goes back')
    reason: ReturnReason = Field(..., description='Why it goes back')
    refundAmount: float = Field(..., ge=0, description='What it refunds')
    currency: StoreCurrency = Field(..., description="The order's currency")
    createdAt: datetime.datetime = Field(...,
                                         description='When it was requested')
    rejectReason: str | None = Field(...,
                                     description='Why the store rejected it')
    refundedAt: datetime.datetime | None = Field(
        ..., description='When it was refunded')


class MyReturnsCard(ResponseModel):
    """The shopper's returns a tool listed, requested or cancelled."""
    kind: Literal['myReturns'] = Field(..., description='The card')
    returns: list[ChatMyReturnRow] = Field(
        ..., max_length=CHAT_CARD_ROWS_MAX, description='The returns')


class PolicyCard(ResponseModel):
    """The store's policy a tool read or saved: every answer, null while
    the store has not said."""
    kind: Literal['policy'] = Field(..., description='The card')
    currency: StoreCurrency = Field(..., description='Fees are in it')
    acceptsReturns: AcceptsReturns
    returnWindowDays: ReturnWindowDays
    returnConditions: ReturnConditions
    nonReturnableItems: NonReturnableItems
    refundMethod: RefundMethodAnswer
    returnShippingPaidBy: ReturnShippingPaidBy
    defectReportDays: DefectReportDays
    deliveryOptions: DeliveryOptions
    deliveryAreas: DeliveryAreas
    handoverDays: HandoverDays
    acceptedPaymentMethods: AcceptedPaymentMethods
    paidOrderCancellation: PaidOrderCancellationAnswer
    paidOrderCancellationHours: PaidOrderCancellationHours
    sendsMarketingMessages: SendsMarketingMessages
    additionalTerms: AdditionalTerms
    # Null on a card saved before zones had policies.
    deliveryZones: list[DeliveryZoneRule] | None = Field(
        None, max_length=DELIVERY_ZONES_MAX,
        description="The store's delivery zones and their policies")


class PhotosCard(ResponseModel):
    """The product photos a tool made."""
    kind: Literal['photos'] = Field(..., description='The card')
    photos: list[GeneratedPhotoRow] = Field(..., description='The photos')
    prompt: str | None = Field(
        None, description='The prompt they were made with; null on a card '
        'saved before prompts were shown')


class ProductPhotoChoiceCard(ResponseModel):
    """The photos the admin picks one of for a product: its original and
    the styled ones made from it. Answered with ProductPhotoAnswer."""
    kind: Literal['productPhotoChoice'] = Field(..., description='The card')
    formId: FormId
    productId: str = Field(..., description='The Products _id')
    productName: str = Field(..., description='Product name')
    photos: list[PhotoChoiceRow] = Field(
        ..., min_length=1, max_length=1 + PHOTO_CHOICES_GENERATED,
        description='The original first, then the styled photos')
    prompt: str | None = Field(..., description='The prompt the styled '
                               'photos were made with; null when none were')
    notGenerated: str | None = Field(..., description='Why no styled photos '
                                     'were made; null when they were')


class StoreCard(ResponseModel):
    """The admin's store a tool read, created or pinned."""
    kind: Literal['store'] = Field(..., description='The card')
    name: str | None = Field(..., description='Store name')
    slug: str | None = Field(..., description="The store's link name")
    currency: StoreCurrency = Field(..., description="The store's currency")
    description: str | None = Field(..., description='What the store sells')
    contactNumber: str | None = Field(..., description='Contact number')
    address: str | None = Field(..., description='Street address')
    openingHours: str | None = Field(..., description='Opening hours')
    location: StoreLocationResponse | None = Field(
        ..., description='Map location; null until pinned')


# ──────────────────────────────────────────────
# Forms: opened by the model's form tools
# ──────────────────────────────────────────────

class LocationForm(ResponseModel):
    """Asks the admin to pin the store on a map. Answered with
    LocationAnswer."""
    kind: Literal['locationForm'] = Field(..., description='The form')
    formId: FormId
    location: StoreLocationResponse | None = Field(
        ..., description='The pin so far; null when there is none')
    address: str | None = Field(..., description="The store's address")


class ChatProductDraft(ResponseModel):
    """A product the admin mentioned, prefilled on the new products
    form."""
    name: str | None = Field(..., description='Product name')
    price: float | None = Field(..., description='Price')
    inventory: int | None = Field(..., description='Units in stock')
    description: str | None = Field(..., description='What it is')


class NewProductsForm(ResponseModel):
    """Asks the admin for new products. Answered with NewProductsAnswer."""
    kind: Literal['newProductsForm'] = Field(..., description='The form')
    formId: FormId
    currency: StoreCurrency = Field(..., description='Prices are in it')
    products: list[ChatProductDraft] = Field(
        ..., max_length=PRODUCT_BATCH_MAX, description='Prefilled rows')


class PhoneVerificationForm(ResponseModel):
    """Asks the shopper to verify their phone number (Firebase sends the
    code). Answered with PhoneVerifiedAnswer once verifyCustomerPhone
    stored it."""
    kind: Literal['phoneVerificationForm'] = Field(..., description='The form')
    formId: FormId


class PhotoRequestForm(ResponseModel):
    """Asks the admin for photos of one item to turn into product photos.
    Answered with PhotoRequestAnswer."""
    kind: Literal['photoRequestForm'] = Field(..., description='The form')
    formId: FormId
    count: int = Field(..., ge=1, le=PHOTOS_PER_CALL_MAX,
                       description='How many photos to make, prefilled')


class DeliveryPinForm(ResponseModel):
    """Asks the shopper to pin where their delivery goes: a map and "use
    where I am", nothing typed (the model asks for the address in chat).
    Answered with DeliveryPinAnswer."""
    kind: Literal['deliveryPinForm'] = Field(..., description='The form')
    formId: FormId
    storeLocation: StoreLocationResponse | None = Field(
        ..., description="The store's pin, where the map opens; null until "
        'pinned')
    # Null on a form saved before zones had policies.
    currency: StoreCurrency | None = Field(
        None, description="The zones' fees and totals are in it")
    # A pin must fall in one. A form saved before zones existed has none
    # (null), and takes no pin.
    deliveryZones: list[DeliveryZoneResponse] | None = Field(
        None, max_length=DELIVERY_ZONES_MAX,
        description='The zones the store delivers to, with their policies')


class DeliveryZonesForm(ResponseModel):
    """Asks the admin to draw where the store delivers: zones on a map,
    each closed, never overlapping, each with its own delivery policy. The
    map saves them (setMyDeliveryZones), then answers with
    DeliveryZonesAnswer."""
    kind: Literal['deliveryZonesForm'] = Field(..., description='The form')
    formId: FormId
    currency: StoreCurrency = Field(..., description="The zones' fees and "
                                    'totals are in it')
    storeLocation: StoreLocationResponse | None = Field(
        ..., description="The store's pin, where the map opens; null until "
        'pinned')
    zones: list[DeliveryZoneResponse] = Field(
        ..., max_length=DELIVERY_ZONES_MAX,
        description='The areas drawn so far; empty: none')


ChatComponent = Annotated[Union[
    ShopProductsCard, StoreProductsCard, StoreOrdersCard, MyOrdersCard,
    CheckoutCard, StoreReturnsCard, MyReturnsCard, PolicyCard, PhotosCard,
    ProductPhotoChoiceCard, StoreCard, LocationForm, NewProductsForm, PhoneVerificationForm,
    PhotoRequestForm, DeliveryPinForm, DeliveryZonesForm,
], Field(discriminator='kind')]


# ──────────────────────────────────────────────
# Answers: what the client sends back (the chat route's `form`)
# ──────────────────────────────────────────────
# Types and sizes only: the tool the model then calls checks every value, and
# chatForms.py checks each answer against the component it answers.

AnswerId = Annotated[str, Field(min_length=1,
                                max_length=CHAT_ANSWER_ID_MAX_LENGTH)]


class LocationAnswer(AppModel):
    """A LocationForm's pin."""
    kind: Literal['location'] = Field(..., description='The answer')
    formId: FormId
    latitude: Latitude = Field(..., description='Degrees north')
    longitude: Longitude = Field(..., description='Degrees east')


class NewProductAnswerRow(NewProduct):
    """A NewProductsForm row: a product to add, and the photo the admin
    uploaded for it, if any."""
    imageUrl: str | None = Field(
        None, min_length=1, max_length=CHAT_PHOTO_URL_MAX_LENGTH,
        description="The uploaded photo's download URL; null without one")


class NewProductsAnswer(AppModel):
    """A NewProductsForm, filled in."""
    kind: Literal['newProducts'] = Field(..., description='The answer')
    formId: FormId
    products: list[NewProductAnswerRow] = Field(..., min_length=1,
                                       max_length=PRODUCT_BATCH_MAX,
                                       description='The products to add')


class PhoneVerifiedAnswer(AppModel):
    """A PhoneVerificationForm done: the number is verified."""
    kind: Literal['phoneVerified'] = Field(..., description='The answer')
    formId: FormId


class PhotoRequestAnswer(AppModel):
    """A PhotoRequestForm, filled in: the uploaded photos' URLs."""
    kind: Literal['photoRequest'] = Field(..., description='The answer')
    formId: FormId
    imageUrls: list[Annotated[str, Field(
        min_length=1, max_length=CHAT_PHOTO_URL_MAX_LENGTH)]] = Field(
            ..., min_length=1, max_length=PHOTO_REFERENCES_MAX,
            description='The download URLs of the uploaded photos')
    count: int = Field(..., ge=1, le=PHOTOS_PER_CALL_MAX,
                       description='How many photos to make')
    notes: str | None = Field(None, max_length=PHOTO_NOTES_MAX_LENGTH,
                              description='Extra direction')


class OrderItemsAnswer(AppModel):
    """A ShopProductsCard's picks."""
    kind: Literal['orderItems'] = Field(..., description='The answer')
    formId: FormId
    items: list[NewOrderItem] = Field(..., min_length=1,
                                      max_length=ORDER_ITEMS_MAX,
                                      description='What to order')


class PaymentAnswer(AppModel):
    """A CheckoutCard's payment method."""
    kind: Literal['payment'] = Field(..., description='The answer')
    formId: FormId
    paymentMethod: PaymentMethod = Field(..., description='How to pay')


class HandoverAnswer(AppModel):
    """A StoreOrdersCard's orders handed over."""
    kind: Literal['handover'] = Field(..., description='The answer')
    formId: FormId
    orderIds: list[AnswerId] = Field(..., min_length=1,
                                     max_length=CHAT_CARD_ROWS_MAX,
                                     description='The orders handed over')


class ReturnDecisionAnswer(AppModel):
    """A StoreReturnsCard's decision on one requested return."""
    kind: Literal['returnDecision'] = Field(..., description='The answer')
    formId: FormId
    returnId: AnswerId = Field(..., description='The Returns _id')
    decision: Literal['approve', 'reject'] = Field(...,
                                                   description='The decision')
    reason: str | None = Field(None,
                               max_length=RETURN_REJECT_REASON_MAX_LENGTH,
                               description='Why it is rejected')


class DeliveryPinAnswer(AppModel):
    """A DeliveryPinForm's pin: where the delivery goes."""
    kind: Literal['deliveryPin'] = Field(..., description='The answer')
    formId: FormId
    latitude: Latitude = Field(..., description='Degrees north')
    longitude: Longitude = Field(..., description='Degrees east')


class DeliveryZonesAnswer(AppModel):
    """A DeliveryZonesForm done: the areas are saved."""
    kind: Literal['deliveryZones'] = Field(..., description='The answer')
    formId: FormId


class ProductPhotoAnswer(AppModel):
    """A ProductPhotoChoiceCard's pick: the photo to put on the product."""
    kind: Literal['productPhoto'] = Field(..., description='The answer')
    formId: FormId
    productId: AnswerId = Field(..., description="The card's Products _id")
    photoId: AnswerId = Field(..., description='The GeneratedPhotos _id')


# The chat route's `form` param (a plain union: a route param cannot be
# Annotated); ChatFormAnswerTagged reads it by its kind.
ChatFormAnswer = (LocationAnswer | NewProductsAnswer | PhoneVerifiedAnswer
                  | PhotoRequestAnswer | OrderItemsAnswer | PaymentAnswer
                  | HandoverAnswer | ReturnDecisionAnswer | DeliveryPinAnswer
                  | DeliveryZonesAnswer | ProductPhotoAnswer)
ChatFormAnswerTagged = Annotated[ChatFormAnswer, Field(discriminator='kind')]

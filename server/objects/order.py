"""Orders (a customer's order at one store).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field
from typing import Literal

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.store import (DELIVERY_ZONE_NAME_MAX_LENGTH, LATITUDE_LIMIT,
                           LONGITUDE_LIMIT,
                           PHONE_NUMBER_PATTERN, StoreCurrency)
from objects.product import (PRODUCT_NAME_MAX_LENGTH, PRODUCT_PRICE_MAX,
                             Product)
from objects.payment import OrderPaymentMethod, PaymentOption, PaymentProvider


# ──────────────────────────────────────────────
# Orders (a customer's order at one store)
# ──────────────────────────────────────────────

# Where an order is:
#   pending: placed, its units held in each product's reservedInventory,
#     awaiting the customer's choice of how to pay (ORDER_PAYMENT_MINUTES);
#   awaitingPayment: the customer chose a way paid by hand to the store
#     (cash, a check, a bank transfer, the store's own e-wallet account):
#     its units stay held, with no deadline, until the store's admin
#     confirms the payment (paid) or someone cancels it;
#   paid: the customer paid (through the payment provider, or by hand and
#     confirmed by the store);
#   completed: handed over, its units counted in each product's soldCount;
#   cancelled: its units went back to inventory, and a paid one was
#     refunded.
# Every move out of pending, awaitingPayment or paid takes the order's
# reservation off its products, so reservedInventory only holds orders still
# open.
OrderStatus = Literal['pending', 'awaitingPayment', 'paid', 'completed',
                      'cancelled']
# Why an order was cancelled: unpaid by its expiresAt, by the store's
# admin, or by the customer while it was unpaid.
CancelReason = Literal['expired', 'byStore', 'byCustomer']
# The reference a customer gives for a payment made by hand (a bank
# transfer's, an e-wallet's).
MANUAL_PAYMENT_REFERENCE_MAX_LENGTH = 100
# The start of every reference the demo payment provider (payments.py)
# hands out, for a payment and for a refund.
PAYMENT_REFERENCE_PREFIX = 'DEMO-'
REFUND_REFERENCE_PREFIX = 'DEMO-R-'
# The most items one order holds (the checkout enforces it).
ORDER_ITEMS_MAX = 50
# How long a customer has to pay a placed order before it is cancelled.
ORDER_PAYMENT_MINUTES = 30
# The most unpaid orders (pending and unexpired, or awaitingPayment) a
# customer holds at a store.
MAX_PENDING_ORDERS_PER_STORE = 3
# How a customer gets an order: picked up at the store, or delivered to the
# address and pin they gave. The store's policy (deliveryOptions) says which
# it offers; one that has not said offers pickup.
Fulfillment = Literal['pickup', 'delivery']
DELIVERY_ADDRESS_MAX_LENGTH = 200
DELIVERY_NOTES_MAX_LENGTH = 300
# Half the Earth's circumference, rounded up: no two pins are farther apart.
DELIVERY_DISTANCE_MAX_KM = 20_100


class OrderItem(AppModel):
    """One product of an order, a snapshot taken when the order is placed:
    a later change of the product's name or price never rewrites it."""
    productId: str = Field(..., min_length=1, description='The Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name when ordered')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description='Unit price paid, in the order currency')
    quantity: int = Field(..., ge=1, description='Units ordered')


class NewOrderItem(AppModel):
    """One item of a createOrder call. Types only, no limits: the route
    checks them, so a refusal names the item's position and never quotes
    the value (NewProduct's reason)."""
    productId: str = Field(..., description='The Products _id')
    quantity: int = Field(..., description='Units to order')


class NewOrderDelivery(AppModel):
    """Where a createOrder delivery goes. Types only, no limits: the route
    checks them, never quoting a value."""
    address: str = Field(..., description='The street address')
    latitude: float = Field(..., description="The pin's degrees north")
    longitude: float = Field(..., description="The pin's degrees east")
    notes: str | None = Field(None, description='For the delivery, e.g. a '
                              'landmark or a gate code')


class Order(MongoModel):
    """An Orders document, read newest first: the (storeId, status,
    createdAt) and (customerId, storeId, createdAt) indexes
    (mongoDb.ensureIndexes) serve the admin's and the customer's lists,
    (status, expiresAt) and (storeId, status, expiresAt) the sweeps of
    expired unpaid orders.

    Every write to a product's inventory or reservedInventory an order
    makes (placing, cancelling or completing it) must also $inc the
    product's _version, or updateStoreProduct's _version lock misses it and
    the admin's save overwrites it."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    customerId: str = Field(..., min_length=1,
                            description='The Customers _id')
    items: list[OrderItem] = Field(..., min_length=1,
                                   max_length=ORDER_ITEMS_MAX,
                                   description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    status: OrderStatus = Field(..., description='Where the order is')
    createdAt: datetime.datetime = Field(..., description='When it was placed')
    # Stored, not derived from createdAt: a later change of
    # ORDER_PAYMENT_MINUTES never moves a placed order's deadline.
    expiresAt: datetime.datetime = Field(
        ..., description='Still pending (no way to pay chosen) by then, it is '
        'cancelled (UTC)')
    paymentMethod: OrderPaymentMethod | None = Field(
        None, description='How the customer pays or paid')
    paymentProvider: PaymentProvider | None = Field(
        None, description='The payment provider it was paid through; null: '
        'paid by hand to the store (or not chosen yet)')
    paymentReference: str | None = Field(
        None, description="The payment provider's id of the payment")
    manualPaymentReference: str | None = Field(
        None, min_length=1, max_length=MANUAL_PAYMENT_REFERENCE_MAX_LENGTH,
        description='The reference the customer gave for a payment made by '
        "hand, e.g. a bank transfer's")
    paidAt: datetime.datetime | None = Field(None,
                                             description='When it was paid')
    cancelledAt: datetime.datetime | None = Field(
        None, description='When it was cancelled')
    cancelReason: CancelReason | None = Field(
        None, description='Why it was cancelled')
    completedAt: datetime.datetime | None = Field(
        None, description='When it was handed over')
    refundedAt: datetime.datetime | None = Field(
        None, description='When its payment was refunded')
    refundReference: str | None = Field(
        None, description="The payment provider's id of the refund")
    # Delivery. A pickup order has no fee and no address. An order placed
    # before delivery existed is a pickup (these defaults). total includes
    # deliveryFee: it is what was charged.
    fulfillment: Fulfillment = Field(
        'pickup', description='How the customer gets it: pickup or delivery')
    deliveryFee: float = Field(0, ge=0, le=PRODUCT_PRICE_MAX,
                               description='What delivery adds to the total')
    deliveryDistanceKm: float | None = Field(
        None, ge=0, le=DELIVERY_DISTANCE_MAX_KM,
        description="Km from the store's pin to the delivery pin, in a "
        'straight line: what the fee was priced on, for a delivery priced '
        'per km before delivery zones; null since')
    deliveryZone: str | None = Field(
        None, min_length=1, max_length=DELIVERY_ZONE_NAME_MAX_LENGTH,
        description='The delivery zone whose policy priced it; null for a '
        'pickup, or a delivery from before delivery zones')
    deliveryAddress: str | None = Field(
        None, min_length=1, max_length=DELIVERY_ADDRESS_MAX_LENGTH,
        description='Where it is delivered, as the customer typed it')
    deliveryLatitude: float | None = Field(
        None, ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
        description="The delivery pin's degrees north")
    deliveryLongitude: float | None = Field(
        None, ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
        description="The delivery pin's degrees east")
    deliveryNotes: str | None = Field(
        None, min_length=1, max_length=DELIVERY_NOTES_MAX_LENGTH,
        description="The customer's notes for the delivery")


class OrderView(StoredDocumentKeys, Order):
    """An Orders document as stored."""


class OrderItemRow(ResponseModel):
    """One item of an order as a route sends it: OrderItem without its
    productId. Each field restates OrderItem's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name when ordered')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description='Unit price paid, in the order currency')
    quantity: int = Field(..., ge=1, description='Units ordered')


class OrderDeliveryRow(ResponseModel):
    """How an order row's customer gets it, for every order row (the
    store's, the customer's, the chat cards'). Each field restates
    Order's, which test_route_response_models.py's MIRRORS keeps in step;
    an order stored before delivery existed reads as a pickup
    (OrderActions.orderRow)."""
    fulfillment: Fulfillment = Field(
        ..., description='How the customer gets it: pickup or delivery')
    deliveryFee: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                               description='What delivery adds to the total')
    deliveryDistanceKm: float | None = Field(
        ..., ge=0, le=DELIVERY_DISTANCE_MAX_KM,
        description="Km from the store's pin to the delivery pin, in a "
        'straight line: what the fee was priced on, for a delivery priced '
        'per km before delivery zones; null since')
    deliveryZone: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_ZONE_NAME_MAX_LENGTH,
        description='The delivery zone whose policy priced it; null for a '
        'pickup, or a delivery from before delivery zones')
    deliveryAddress: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_ADDRESS_MAX_LENGTH,
        description='Where it is delivered, as the customer typed it')
    deliveryLatitude: float | None = Field(
        ..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
        description="The delivery pin's degrees north")
    deliveryLongitude: float | None = Field(
        ..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
        description="The delivery pin's degrees east")
    deliveryNotes: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_NOTES_MAX_LENGTH,
        description="The customer's notes for the delivery")


class StoreOrderRow(OrderDeliveryRow):
    """One order as its store's admin sees it. Each field restates
    Order's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., min_length=1,
                                      max_length=ORDER_ITEMS_MAX,
                                      description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    createdAt: datetime.datetime = Field(..., description='When it was placed')
    paymentMethod: OrderPaymentMethod | None = Field(
        ..., description='How the customer pays or paid')
    # Defaults, not required: an order stored before payments made by hand
    # has neither.
    paymentProvider: PaymentProvider | None = Field(
        None, description='The payment provider it was paid through; null: '
        'paid by hand to the store (or not chosen yet)')
    manualPaymentReference: str | None = Field(
        None, min_length=1, max_length=MANUAL_PAYMENT_REFERENCE_MAX_LENGTH,
        description='The reference the customer gave for a payment made by '
        "hand, e.g. a bank transfer's")
    paidAt: datetime.datetime | None = Field(...,
                                             description='When it was paid')
    expiresAt: datetime.datetime = Field(
        ..., description='Still pending (no way to pay chosen) by then, it is '
        'cancelled (UTC)')
    completedAt: datetime.datetime | None = Field(
        ..., description='When it was handed over')
    cancelledAt: datetime.datetime | None = Field(
        ..., description='When it was cancelled')
    cancelReason: CancelReason | None = Field(
        ..., description='Why it was cancelled')
    refundedAt: datetime.datetime | None = Field(
        ..., description='When its payment was refunded')
    paymentReference: str | None = Field(
        ..., description="The payment provider's id of the payment")
    refundReference: str | None = Field(
        ..., description="The payment provider's id of the refund")


class MyOrderRow(OrderDeliveryRow):
    """One order as the customer who placed it sees it: StoreOrderRow
    without the contact number and the payment and refund references. Each field
    restates Order's, which test_route_response_models.py's MIRRORS keeps
    in step."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., min_length=1,
                                      max_length=ORDER_ITEMS_MAX,
                                      description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    createdAt: datetime.datetime = Field(..., description='When it was placed')
    paymentMethod: OrderPaymentMethod | None = Field(
        ..., description='How the customer pays or paid')
    # Defaults, not required: an order stored before payments made by hand
    # has neither.
    paymentProvider: PaymentProvider | None = Field(
        None, description='The payment provider it was paid through; null: '
        'paid by hand to the store (or not chosen yet)')
    manualPaymentReference: str | None = Field(
        None, min_length=1, max_length=MANUAL_PAYMENT_REFERENCE_MAX_LENGTH,
        description='The reference the customer gave for a payment made by '
        "hand, e.g. a bank transfer's")
    paidAt: datetime.datetime | None = Field(...,
                                             description='When it was paid')
    expiresAt: datetime.datetime = Field(
        ..., description='Still pending (no way to pay chosen) by then, it is '
        'cancelled (UTC)')
    completedAt: datetime.datetime | None = Field(
        ..., description='When it was handed over')
    cancelledAt: datetime.datetime | None = Field(
        ..., description='When it was cancelled')
    cancelReason: CancelReason | None = Field(
        ..., description='Why it was cancelled')
    refundedAt: datetime.datetime | None = Field(
        ..., description='When its payment was refunded')


class StoreOrderListResponse(ResponseModel):
    orders: list[StoreOrderRow] = Field(
        ..., description='The orders, newest first')


class MyOrderListResponse(ResponseModel):
    orders: list[MyOrderRow] = Field(...,
                                     description='The orders, newest first')


class CreatedOrderResponse(ResponseModel):
    order: MyOrderRow = Field(..., description='The order just placed')
    paymentOptions: list[PaymentOption] = Field(
        ..., min_length=1, description='The ways the customer can pay it')

"""Carts: what a customer has put aside to order at one store.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from typing import ClassVar
from pydantic import Field

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.order import (DELIVERY_ADDRESS_MAX_LENGTH,
                           DELIVERY_NOTES_MAX_LENGTH, ORDER_ITEMS_MAX,
                           Fulfillment)
from objects.policy import POLICY_DAYS_MAX
from objects.product import (HAS_INVENTORY_DESCRIPTION,
                             PRODUCT_INVENTORY_MAX, PRODUCT_NAME_MAX_LENGTH,
                             PRODUCT_PRICE_MAX)
from objects.store import (DELIVERY_ZONE_DAYS_MAX,
                           DELIVERY_ZONE_NAME_MAX_LENGTH, LATITUDE_LIMIT,
                           LONGITUDE_LIMIT, StoreCurrency)


# ──────────────────────────────────────────────
# Carts (one per customer and store)
# ──────────────────────────────────────────────

# The most products one cart holds: what one order takes, so a whole cart
# can always be checked out as one order.
CART_ITEMS_MAX = ORDER_ITEMS_MAX


class CartLine(AppModel):
    """One product in a cart: only its id and how many. The name, price and
    stock are read from the product each time the cart is shown, so a cart
    never shows a stale price."""
    productId: str = Field(..., min_length=1, description='The Products _id')
    quantity: int = Field(..., ge=1, le=PRODUCT_INVENTORY_MAX,
                          description='Units the customer wants')


class Cart(MongoModel):
    """A Carts document: one customer's cart at one store, its _id
    "<customerId>:<storeId>" (cartId), so each pair has one cart and a
    racing first add cannot make two. Nothing in a cart holds stock: the
    stock is checked when an item is added and again at checkout, which
    reserves the units (OrderActions.createOrder). Every write checks and
    bumps _version."""
    customerId: str = Field(..., min_length=1,
                            description='The Customers _id')
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    items: list[CartLine] = Field(default_factory=list,
                                  max_length=CART_ITEMS_MAX,
                                  description='The products, in the order '
                                  'they were added')
    updatedAt: datetime.datetime = Field(..., description='Last changed')


class CartView(StoredDocumentKeys, Cart):
    """A Carts document as stored."""


class CartItemRow(ResponseModel):
    """One product of the customer's cart, with the product's current name,
    price and stock."""
    AI_READS: ClassVar[dict[str, str]] = {
        'productId': 'the productId the cart tools take',
        'name': 'the customer names the products in their cart',
        'price': 'the unit price the customer asks about',
        'quantity': 'how many the customer has in the cart',
        'inventory': 'tells the customer when fewer are left than they '
                     'want; 0 is sold out',
        'hasInventory': 'false: unlimited, never sold out (ignore inventory)',
    }
    productId: str = Field(..., min_length=1, description='The Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Unit price now, in the store's "
                         'currency')
    quantity: int = Field(..., ge=1, le=PRODUCT_INVENTORY_MAX,
                          description='Units in the cart')
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell now; 0 is sold '
                           'out')
    hasInventory: bool = Field(..., description=HAS_INVENTORY_DESCRIPTION)


class CartResponse(ResponseModel):
    """The customer's cart at the store, as the AI reads it."""
    AI_READS: ClassVar[dict[str, str]] = {
        'currency': 'the prices are in it',
        'items': 'the products in the cart',
        'subtotal': 'what the items cost, before any delivery fee',
    }
    currency: StoreCurrency = Field(
        ..., description='ISO 4217 code of every price in the cart')
    items: list[CartItemRow] = Field(
        ..., max_length=CART_ITEMS_MAX,
        description='The products still sold, in the order they were added')
    subtotal: float = Field(..., ge=0,
                            description='Every item at its price now, times '
                            'its quantity')


class CartScreenItemRow(CartItemRow):
    """A cart row as the shop's cart panel shows it."""
    photoUrl: str | None = Field(
        None, min_length=1, description="The product's photo; null when it "
        'has none')


class CartScreenResponse(CartResponse):
    """The customer's cart as the shop's cart panel shows it."""
    items: list[CartScreenItemRow] = Field(
        ..., max_length=CART_ITEMS_MAX,
        description='The products still sold, in the order they were added')


# ──────────────────────────────────────────────
# Checkout summary (previewMyCheckout, before the order is placed)
# ──────────────────────────────────────────────

class CheckoutLineRow(ResponseModel):
    """One line of a checkout summary: a product of the cart at its price
    now (the chat's summary card shows these)."""
    AI_READS: ClassVar[dict[str, str]] = {
        'name': 'the customer names what they are ordering',
        'price': 'the unit price the customer asks about',
        'quantity': 'how many of it the order takes',
        'lineTotal': 'what the line costs, when the customer asks',
    }
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Unit price now, in the store's "
                         'currency')
    quantity: int = Field(..., ge=1, le=PRODUCT_INVENTORY_MAX,
                          description='Units ordered')
    lineTotal: float = Field(..., ge=0,
                             description='The price times the quantity')


class CheckoutPreviewItemRow(CheckoutLineRow):
    """One line of the summary as the AI reads it."""
    AI_READS: ClassVar[dict[str, str]] = {
        'productId': 'the productId setCartItemQuantity and removeFromCart '
                     'take when the customer changes a line',
    }
    productId: str = Field(..., min_length=1, description='The Products _id')


class CheckoutSummary(ResponseModel):
    """What checking out the cart would place, priced as the order would be
    (OrderActions.orderPricing): the AI's previewMyCheckout answer and the
    chat's summary card share these fields."""
    AI_READS: ClassVar[dict[str, str]] = {
        'currency': 'the amounts are in it',
        'subtotal': 'what the items cost, before delivery',
        'fulfillment': 'pickup or delivery, as checkoutMyCart then takes it',
        'deliveryZone': 'the zone whose policy prices the delivery',
        'deliveryFee': 'what delivery adds to the total; 0 is free',
        'total': 'what the customer would pay; said when they only hear you',
        'readyInDays': 'when the customer asks how soon it comes',
    }
    currency: StoreCurrency = Field(
        ..., description='ISO 4217 code of every amount')
    subtotal: float = Field(..., ge=0, description='The items, before '
                            'delivery')
    fulfillment: Fulfillment = Field(
        ..., description='How the customer gets it: pickup or delivery')
    deliveryZone: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_ZONE_NAME_MAX_LENGTH,
        description='The delivery zone whose policy prices it; null for a '
        'pickup')
    deliveryFee: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                               description='What delivery adds to the total '
                               '(0: free, or a pickup)')
    total: float = Field(..., ge=0, description='The subtotal plus the '
                         'delivery fee')
    readyInDays: int | None = Field(
        ..., ge=0, le=max(DELIVERY_ZONE_DAYS_MAX, POLICY_DAYS_MAX),
        description="Days from payment until it arrives (the zone's "
        "deliveryDays) or is ready for pickup (the store's handoverDays); "
        "null: the store hasn't said")
    deliveryAddress: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_ADDRESS_MAX_LENGTH,
        description='Where it is delivered, as the customer gave it; null '
        'for a pickup')
    deliveryLatitude: float | None = Field(
        ..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
        description="The delivery pin's degrees north")
    deliveryLongitude: float | None = Field(
        ..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
        description="The delivery pin's degrees east")
    deliveryNotes: str | None = Field(
        ..., min_length=1, max_length=DELIVERY_NOTES_MAX_LENGTH,
        description="The customer's notes for the rider")


class CheckoutPreviewResponse(CheckoutSummary):
    """previewMyCheckout's answer: the order checkoutMyCart would place now,
    nothing placed or held."""
    AI_READS: ClassVar[dict[str, str]] = {
        'items': 'what the order would hold',
    }
    items: list[CheckoutPreviewItemRow] = Field(
        ..., min_length=1, max_length=CART_ITEMS_MAX,
        description="The cart's products still sold, in the cart's order")

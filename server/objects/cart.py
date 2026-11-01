"""Carts: what a customer has put aside to order at one store.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from typing import ClassVar
from pydantic import Field

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.order import ORDER_ITEMS_MAX
from objects.product import (PRODUCT_INVENTORY_MAX, PRODUCT_NAME_MAX_LENGTH,
                             PRODUCT_PRICE_MAX)
from objects.store import StoreCurrency


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

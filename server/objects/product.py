"""Products (each belongs to one store).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
import typing

from typing import ClassVar
from pydantic import Field

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.store import DELIVERY_ZONE_AMOUNT_MAX, StoreCurrency


# ──────────────────────────────────────────────
# Products (each belongs to one store)
# ──────────────────────────────────────────────

PRODUCT_NAME_MAX_LENGTH = 80
PRODUCT_DESCRIPTION_MAX_LENGTH = 500
# Caps on what an admin can type: they keep a stray huge number from
# overflowing MongoDB's int64 (or a price's float precision).
PRODUCT_INVENTORY_MAX = 1_000_000
PRODUCT_PRICE_MAX = 10_000_000
# A zone's delivery fee and totals share the price's limit (objects/store.py
# cannot import this module).
if DELIVERY_ZONE_AMOUNT_MAX != PRODUCT_PRICE_MAX:
    raise TypeError('DELIVERY_ZONE_AMOUNT_MAX must equal PRODUCT_PRICE_MAX')
# The most products one addStoreProducts call adds.
PRODUCT_BATCH_MAX = 50
# The most products one store holds.
PRODUCTS_PER_STORE_MAX = 2000
# How listMyStoreProducts orders an admin's products: by name, best sellers
# first (soldCount), fewest units left first (inventory), or the most
# expensive first (price). Each sort ties on the name and has its own
# (storeId, ..., nameKey) index (mongoDb.ensureIndexes), so a list longer
# than its limit is cut in order.
AdminProductSort = typing.Literal['name', 'soldCount', 'inventory', 'price']
ADMIN_PRODUCT_SORTS = typing.get_args(AdminProductSort)


def productNameKey(name: str) -> str:
    """The key that makes a product name unique in its store: lowercased,
    with every run of whitespace collapsed to one space."""
    return ' '.join(name.lower().split())


class ReservedInventory(AppModel):
    """Units of a product an order is holding (taken out of inventory)."""
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    quantity: int = Field(..., ge=1, description='Units the order holds')


class Product(MongoModel):
    """A Products document, priced in its store's currency. Listed best
    sellers first: the (storeId, isAvailable, soldCount, name) index
    (mongoDb.ensureIndexes) serves that sort. (storeId, nameKey) is a
    unique index: one store never has two products of the same name.

    Every write to inventory or reservedInventory (an order reserving or
    releasing units included) must also $inc _version, or
    updateStoreProduct's _version lock misses it and the admin's save
    overwrites it."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    nameKey: str = Field(..., min_length=1,
                         description='productNameKey(name), unique per store')
    description: str | None = Field(
        None, max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedInventory: list[ReservedInventory] = Field(
        default_factory=list, description='Units orders are holding')
    soldCount: int = Field(0, ge=0, description='Units sold so far')
    isAvailable: bool = Field(True, description='Shown to customers')
    createdAt: datetime.datetime = Field(..., description='When it was added')
    photoUrl: str | None = Field(
        None, min_length=1,
        description='The photo the shop shows: the url of one of the '
        "store's GeneratedPhotos (setProductPhoto)")


class ProductView(StoredDocumentKeys, Product):
    """A Products document as stored."""


class NewProduct(AppModel):
    """One product of an addStoreProducts call, as the admin described it.
    Types only, no limits: the route checks them (_validProduct), so a
    refusal names the product's position and never quotes the value."""
    name: str = Field(..., description='Product name')
    price: float = Field(..., description="Price in the store's currency")
    inventory: int = Field(..., description='Units in stock, free to sell')
    description: str | None = Field(None, description='What the product is')
    isAvailable: bool = Field(True, description='Shown to customers')


class ProductRow(ResponseModel):
    """One product as a customer's list sends it. Each field restates
    Product's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the productId addToCart and createOrder take',
        'name': 'the customer asks for products by name',
        'description': 'the customer asks what a product is',
        'price': 'the customer asks the price',
        'inventory': 'tells the customer what is left; 0 is sold out',
    }
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        None, max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    photoUrl: str | None = Field(
        None, min_length=1,
        description='The photo the shop shows: the url of one of the '
        "store's GeneratedPhotos (setProductPhoto)")


class ProductListResponse(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {
        'currency': 'the prices are in it',
        'products': 'the products the AI answers about',
    }
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[ProductRow] = Field(
        ..., description='The products, best sellers first')


class ReservedInventoryResponse(ResponseModel):
    """ReservedInventory as a tool sends it: the units only (which order
    holds them is no one's question). Each field restates
    ReservedInventory's, which test_route_response_models.py's MIRRORS
    keeps in step."""
    AI_READS: ClassVar[dict[str, str]] = {
        'quantity': 'units held: updateStoreProduct subtracts them from a total on hand',
    }
    quantity: int = Field(..., ge=1, description='Units the order holds')


class AdminProductRow(ResponseModel):
    """One product as its store's admin sees it. Each field restates
    Product's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the productId updateStoreProduct and the photo tools take',
        'name': 'the admin names products',
        'description': 'no admin card shows it: the admin asks what it says',
        'price': 'the admin asks and changes prices',
        'inventory': 'stock questions and updates',
        'reservedInventory': 'units orders hold (its quantities)',
        'isAvailable': 'hidden products',
        'soldCount': 'best sellers',
        'hasPhoto': 'which products still need a photo',
    }
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        ..., max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedInventory: list[ReservedInventoryResponse] = Field(
        ..., description='Units orders are holding')
    isAvailable: bool = Field(..., description='Shown to customers')
    soldCount: int = Field(..., ge=0, description='Units sold so far')
    photoUrl: str | None = Field(
        None, min_length=1,
        description='The photo the shop shows: the url of one of the '
        "store's GeneratedPhotos (setProductPhoto)")
    hasPhoto: bool = Field(
        ..., description='Whether the shop shows a photo of it: what the AI '
        'reads, the card showing the photo itself '
        '(ProductActions.adminProductRow)')


class AdminProductListRow(ResponseModel):
    """One product of an admin's list: AdminProductRow with the
    reservations summed (reservedQuantity), so a row never grows with its
    open orders. Each field restates Product's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the productId updateStoreProduct and the photo tools take',
        'name': 'the admin names products',
        'description': 'no admin card shows it: the admin asks what it says',
        'price': 'the admin asks and changes prices',
        'inventory': 'low or out-of-stock questions',
        'reservedQuantity': 'updateStoreProduct subtracts it from a total on hand',
        'isAvailable': 'hidden products',
        'soldCount': 'best sellers (sortBy soldCount)',
        'hasPhoto': 'which products still need a photo',
    }
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        ..., max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedQuantity: int = Field(
        ..., ge=0, description='Units orders are holding, all orders summed')
    isAvailable: bool = Field(..., description='Shown to customers')
    soldCount: int = Field(..., ge=0, description='Units sold so far')
    photoUrl: str | None = Field(
        None, min_length=1,
        description='The photo the shop shows: the url of one of the '
        "store's GeneratedPhotos (setProductPhoto)")
    hasPhoto: bool = Field(
        ..., description='Whether the shop shows a photo of it: what the AI '
        'reads, the card showing the photo itself '
        '(ProductActions.adminProductRow)')


class AdminProductListResponse(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {
        'currency': 'the prices are in it',
        'products': 'the products the AI answers about',
    }
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[AdminProductListRow] = Field(...,
                                                description='The products')

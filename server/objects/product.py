"""Products (each belongs to one store).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.store import StoreCurrency


# ──────────────────────────────────────────────
# Products (each belongs to one store)
# ──────────────────────────────────────────────

PRODUCT_NAME_MAX_LENGTH = 80
PRODUCT_DESCRIPTION_MAX_LENGTH = 500
# Caps on what an admin can type: they keep a stray huge number from
# overflowing MongoDB's int64 (or a price's float precision).
PRODUCT_INVENTORY_MAX = 1_000_000
PRODUCT_PRICE_MAX = 10_000_000
# The most products one addStoreProducts call adds.
PRODUCT_BATCH_MAX = 50
# The most products one store holds.
PRODUCTS_PER_STORE_MAX = 2000


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
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[ProductRow] = Field(
        ..., description='The products, best sellers first')


class ReservedInventoryResponse(ResponseModel):
    """ReservedInventory as a route sends it. Each field restates
    ReservedInventory's, which test_route_response_models.py's MIRRORS
    keeps in step."""
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    quantity: int = Field(..., ge=1, description='Units the order holds')


class AdminProductRow(ResponseModel):
    """One product as its store's admin sees it. Each field restates
    Product's, which test_route_response_models.py's MIRRORS keeps in
    step."""
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


class AdminProductListRow(ResponseModel):
    """One product of an admin's list: AdminProductRow with the
    reservations summed (reservedQuantity), so a row never grows with its
    open orders. Each field restates Product's, which
    test_route_response_models.py's MIRRORS keeps in step."""
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


class AdminProductListResponse(ResponseModel):
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[AdminProductListRow] = Field(...,
                                                description='The products')

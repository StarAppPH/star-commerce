"""Stores (one per admin) and customers.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field, field_validator
from typing import Literal

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)


# ──────────────────────────────────────────────
# Stores (one per admin) and customers
# ──────────────────────────────────────────────

STORE_NAME_MAX_LENGTH = 60
STORE_DESCRIPTION_MAX_LENGTH = 300
STORE_ADDRESS_MAX_LENGTH = 200
STORE_OPENING_HOURS_MAX_LENGTH = 100
LATITUDE_LIMIT = 90
LONGITUDE_LIMIT = 180
# The store's link name: lowercase letters and digits, single hyphens between.
STORE_SLUG_PATTERN = r'^[a-z0-9]+(-[a-z0-9]+)*$'
STORE_SLUG_MAX_LENGTH = 50
# A verified Firebase phone number is E.164: +, country code, subscriber.
PHONE_NUMBER_PATTERN = r'^\+[1-9][0-9]{6,14}$'
# The currencies a store can price in (ISO 4217 codes); PHP by default.
StoreCurrency = Literal['PHP', 'USD', 'EUR', 'GBP', 'JPY', 'SGD', 'AUD',
                        'CAD', 'HKD', 'CNY', 'KRW', 'MYR', 'THB', 'IDR',
                        'VND', 'INR', 'AED']
DEFAULT_STORE_CURRENCY = 'PHP'


class StoreLocation(AppModel):
    """Where a store is on the map, pinned by its admin."""
    latitude: float = Field(..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
                            description='Degrees north (negative: south)')
    longitude: float = Field(..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
                             description='Degrees east (negative: west)')


class StoreLocationResponse(ResponseModel):
    """A store's map location as a route sends it. Each field restates
    StoreLocation's, which test_route_response_models.py's MIRRORS keeps
    in step."""
    latitude: float = Field(..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
                            description='Degrees north (negative: south)')
    longitude: float = Field(..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
                             description='Degrees east (negative: west)')


class Store(MongoModel):
    """A Stores document. One per admin: ownerUserId (the admin's Users _id)
    and slug are both unique indexes (mongoDb.ensureIndexes)."""
    ownerUserId: str = Field(..., min_length=1,
                             description="The admin's Users _id")
    name: str = Field(..., min_length=1, max_length=STORE_NAME_MAX_LENGTH,
                      description='Store name shown to customers')
    slug: str = Field(..., max_length=STORE_SLUG_MAX_LENGTH,
                      pattern=STORE_SLUG_PATTERN,
                      description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str = Field(..., min_length=1,
                             max_length=STORE_DESCRIPTION_MAX_LENGTH,
                             description='What the store sells')
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description='Contact number, E.164')
    address: str = Field(..., min_length=1, max_length=STORE_ADDRESS_MAX_LENGTH,
                         description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        None, max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocation | None = Field(
        None, description='Map location; null until the admin pins it')
    createdAt: datetime.datetime = Field(..., description='When it was created')

    @field_validator('name', 'description', 'contactNumber', 'address',
                     'openingHours', mode='before')
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v


class StoreView(StoredDocumentKeys, Store):
    """A Stores document as stored."""


class Customer(MongoModel):
    """A Customers document. The _id is the customer's Firebase uid: an
    anonymous guest at first, linked to a phone at checkout (same uid)."""
    phoneNumber: str | None = Field(None, pattern=PHONE_NUMBER_PATTERN,
                                    description='Verified contact number')
    createdAt: datetime.datetime = Field(..., description='First visit')


class CustomerView(StoredDocumentKeys, Customer):
    """A Customers document as stored."""


class StoreResponse(ResponseModel):
    """A store as a route sends it. Each field restates Store's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Stores _id')
    name: str = Field(..., min_length=1, max_length=STORE_NAME_MAX_LENGTH,
                      description='Store name shown to customers')
    slug: str = Field(..., max_length=STORE_SLUG_MAX_LENGTH,
                      pattern=STORE_SLUG_PATTERN,
                      description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str = Field(..., min_length=1,
                             max_length=STORE_DESCRIPTION_MAX_LENGTH,
                             description='What the store sells')
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description='Contact number, E.164')
    address: str = Field(..., min_length=1, max_length=STORE_ADDRESS_MAX_LENGTH,
                         description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        ..., max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocationResponse | None = Field(
        ..., description='Map location; null until the admin pins it')


class MyStoreResponse(ResponseModel):
    store: StoreResponse | None = Field(
        ..., description="The admin's store; null until it is created")


class CustomerSessionResponse(ResponseModel):
    """What startGuestSession sends: its _id becomes the JWT identity."""
    id: str = Field(..., alias='_id', description='Customers _id')
    phoneNumber: str | None = Field(..., pattern=PHONE_NUMBER_PATTERN,
                                    description='Verified contact number')
    store: StoreResponse = Field(..., description='The store being visited')


class PhoneVerifiedResponse(ResponseModel):
    phoneNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                             description='The verified contact number')

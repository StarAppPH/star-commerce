"""Stores (one per admin) and customers.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
import re

from pydantic import Field, ValidationInfo, field_validator
from typing import Annotated, Literal

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
# A map pin's coordinates (NaN and infinity are out of range too).
Latitude = Annotated[float, Field(ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT)]
Longitude = Annotated[float, Field(ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT)]
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
# The store's look for its product photos (imageModel): a style the admin
# writes, and two brand colors as #RRGGBB, stored uppercase.
PHOTO_STYLE_MAX_LENGTH = 300
HEX_COLOR_PATTERN = r'^#[0-9A-Fa-f]{6}$'
HEX_COLOR_EXAMPLE = '#1A73E8'
STORE_THEME_FIELDS = ('photoStyle', 'primaryColor', 'secondaryColor')
# The ElevenLabs voice the store's assistant speaks with (voiceModel), set
# with updateMyStoreTheme. Like a theme field, the owner's views send it only
# once set (STORE_OWNER_ONLY_FIELDS).
VOICE_ID_PATTERN = r'^[A-Za-z0-9]{1,64}$'
STORE_VOICE_FIELD = 'voiceId'
STORE_OWNER_ONLY_FIELDS = (*STORE_THEME_FIELDS, STORE_VOICE_FIELD)


def hexColor(value, field: str) -> str | None:
    """`value` as a stored brand color: a #RRGGBB hex color, uppercase.
    None or "" is None; anything else is refused, naming `field`, never
    quoting the value."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == '':
            return None
        if re.fullmatch(HEX_COLOR_PATTERN, value) is not None:
            return value.upper()
    raise ValueError(f'{field} must be a hex color like {HEX_COLOR_EXAMPLE}')


class StoreLocation(AppModel):
    """Where a store is on the map, pinned by its admin."""
    latitude: Latitude = Field(...,
                               description='Degrees north (negative: south)')
    longitude: Longitude = Field(...,
                                 description='Degrees east (negative: west)')


class StoreLocationResponse(ResponseModel):
    """A store's map location as a route sends it. Each field restates
    StoreLocation's, which test_route_response_models.py's MIRRORS keeps
    in step."""
    latitude: Latitude = Field(...,
                               description='Degrees north (negative: south)')
    longitude: Longitude = Field(...,
                                 description='Degrees east (negative: west)')


class Store(MongoModel):
    """A Stores document. One per admin: ownerUserId (the admin's Users _id)
    is a unique index, and so is slug among the stores that have one
    (mongoDb.ensureIndexes).

    Sign-up creates the admin's store empty: only its owner and currency.
    A store is *set up* once its slug is a string, which createUserStore
    sets together with the other details; until then every store reader
    treats it as no store yet (StoreActions.SET_UP)."""
    ownerUserId: str = Field(..., min_length=1,
                             description="The admin's Users _id")
    name: str | None = Field(None, min_length=1,
                             max_length=STORE_NAME_MAX_LENGTH,
                             description='Store name shown to customers')
    slug: str | None = Field(None, max_length=STORE_SLUG_MAX_LENGTH,
                             pattern=STORE_SLUG_PATTERN,
                             description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str | None = Field(None, min_length=1,
                                    max_length=STORE_DESCRIPTION_MAX_LENGTH,
                                    description='What the store sells')
    contactNumber: str | None = Field(None, pattern=PHONE_NUMBER_PATTERN,
                                      description='Contact number, E.164')
    address: str | None = Field(None, min_length=1,
                                max_length=STORE_ADDRESS_MAX_LENGTH,
                                description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        None, max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocation | None = Field(
        None, description='Map location; null until the admin pins it')
    photoStyle: str | None = Field(
        None, min_length=1, max_length=PHOTO_STYLE_MAX_LENGTH,
        description="The look of the store's product photos")
    primaryColor: str | None = Field(None, pattern=HEX_COLOR_PATTERN,
                                     description='Main brand color, #RRGGBB')
    secondaryColor: str | None = Field(
        None, pattern=HEX_COLOR_PATTERN,
        description='Second brand color, #RRGGBB')
    voiceId: str | None = Field(
        None, pattern=VOICE_ID_PATTERN,
        description="The ElevenLabs voice its assistant speaks with; null: "
        'the default voice')
    createdAt: datetime.datetime = Field(..., description='When it was created')

    @field_validator('name', 'description', 'contactNumber', 'address',
                     'openingHours', 'photoStyle', mode='before')
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator('primaryColor', 'secondaryColor', mode='before')
    @classmethod
    def _hex_color(cls, v, info: ValidationInfo):
        return hexColor(v, info.field_name)


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
    name: str | None = Field(None, min_length=1,
                             max_length=STORE_NAME_MAX_LENGTH,
                             description='Store name shown to customers')
    slug: str | None = Field(None, max_length=STORE_SLUG_MAX_LENGTH,
                             pattern=STORE_SLUG_PATTERN,
                             description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str | None = Field(None, min_length=1,
                                    max_length=STORE_DESCRIPTION_MAX_LENGTH,
                                    description='What the store sells')
    contactNumber: str | None = Field(None, pattern=PHONE_NUMBER_PATTERN,
                                      description='Contact number, E.164')
    address: str | None = Field(None, min_length=1,
                                max_length=STORE_ADDRESS_MAX_LENGTH,
                                description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        ..., max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocationResponse | None = Field(
        ..., description='Map location; null until the admin pins it')


def _isNone(value) -> bool:
    return value is None


class ShopStoreResponse(StoreResponse):
    """The store a shopper visits: a StoreResponse plus its brand colors,
    which the shop is drawn in. A color is sent only once set (the route
    leaves an unset one out), so a store with none reads exactly as a
    StoreResponse. Each field restates Store's
    (test_route_response_models.py's MIRRORS)."""
    primaryColor: str | None = Field(None, pattern=HEX_COLOR_PATTERN,
                                     exclude_if=_isNone,
                                     description='Main brand color, #RRGGBB')
    secondaryColor: str | None = Field(
        None, pattern=HEX_COLOR_PATTERN, exclude_if=_isNone,
        description='Second brand color, #RRGGBB')


class AdminStoreResponse(StoreResponse):
    """The admin's own store: a StoreResponse plus its photo theme and
    voice. The photo style feeds the product photos and the voice the
    spoken replies, and customers never get either; they get the brand
    colors (ShopStoreResponse). Each is sent only once set (a dict the
    route builds leaves it out, and exclude_if drops it from a dump), so a
    store with none reads exactly as a StoreResponse. Each field restates
    Store's (test_route_response_models.py's MIRRORS)."""
    photoStyle: str | None = Field(
        None, min_length=1, max_length=PHOTO_STYLE_MAX_LENGTH,
        exclude_if=_isNone,
        description="The look of the store's product photos")
    primaryColor: str | None = Field(None, pattern=HEX_COLOR_PATTERN,
                                     exclude_if=_isNone,
                                     description='Main brand color, #RRGGBB')
    secondaryColor: str | None = Field(
        None, pattern=HEX_COLOR_PATTERN, exclude_if=_isNone,
        description='Second brand color, #RRGGBB')
    voiceId: str | None = Field(
        None, pattern=VOICE_ID_PATTERN, exclude_if=_isNone,
        description="The ElevenLabs voice its assistant speaks with; null: "
        'the default voice')


class MyStoreResponse(ResponseModel):
    store: AdminStoreResponse | None = Field(
        ..., description="The admin's store; null until it is created")


class StoreThemeResponse(ResponseModel):
    """What updateMyStoreTheme sends: the store's photo theme, each field
    null while unset (never the voice: getUserStore sends it). Each field
    restates Store's (MIRRORS)."""
    photoStyle: str | None = Field(
        ..., min_length=1, max_length=PHOTO_STYLE_MAX_LENGTH,
        description="The look of the store's product photos")
    primaryColor: str | None = Field(..., pattern=HEX_COLOR_PATTERN,
                                     description='Main brand color, #RRGGBB')
    secondaryColor: str | None = Field(
        ..., pattern=HEX_COLOR_PATTERN,
        description='Second brand color, #RRGGBB')


class AddressSuggestionResponse(ResponseModel):
    """What suggestStoreAddress sends: the street address Google gives for
    the store's pin, and the address the store has now."""
    suggestedAddress: str | None = Field(
        ..., min_length=1, max_length=STORE_ADDRESS_MAX_LENGTH,
        description="The pin's street address; null when there is none")
    address: str | None = Field(None, min_length=1,
                                max_length=STORE_ADDRESS_MAX_LENGTH,
                                description='Street address, as the admin typed it')


class CustomerSessionResponse(ResponseModel):
    """What startGuestSession sends: its _id becomes the JWT identity."""
    id: str = Field(..., alias='_id', description='Customers _id')
    phoneNumber: str | None = Field(..., pattern=PHONE_NUMBER_PATTERN,
                                    description='Verified contact number')
    store: ShopStoreResponse = Field(...,
                                     description='The store being visited')


class PhoneVerifiedResponse(ResponseModel):
    phoneNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                             description='The verified contact number')

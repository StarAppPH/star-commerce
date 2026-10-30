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


# Where a store delivers: up to DELIVERY_ZONES_MAX zones the admin draws on
# the map, each a closed shape of corners in drawing order (the last joins
# the first) with its own delivery policy: a name, a flat fee, the days a
# delivery takes, and optionally a minimum order and an order total from
# which delivery is free. Zones never overlap (zonesOverlap); they may share
# a border. A store with no zone takes no delivery order.
DELIVERY_ZONES_MAX = 10
DELIVERY_ZONE_CORNERS_MIN = 3
DELIVERY_ZONE_CORNERS_MAX = 100
DELIVERY_ZONE_NAME_MAX_LENGTH = 40
DELIVERY_ZONE_DAYS_MAX = 365
# PRODUCT_PRICE_MAX's value (objects/product.py imports this module, so it
# pins the two equal).
DELIVERY_ZONE_AMOUNT_MAX = 10_000_000

DeliveryZoneName = Annotated[str, Field(
    min_length=1, max_length=DELIVERY_ZONE_NAME_MAX_LENGTH,
    description='What the zone is called, e.g. Makati')]
DeliveryZoneFee = Annotated[float, Field(
    ge=0, le=DELIVERY_ZONE_AMOUNT_MAX,
    description="What a delivery into the zone costs, in the store's "
    'currency; 0: free')]
DeliveryZoneDays = Annotated[int, Field(
    ge=0, le=DELIVERY_ZONE_DAYS_MAX,
    description='Days from payment until a delivery into the zone arrives')]
DeliveryZoneMinimumOrder = Annotated[float | None, Field(
    ge=0, le=DELIVERY_ZONE_AMOUNT_MAX,
    description="The smallest items total the zone takes, in the store's "
    'currency; null: no minimum')]
DeliveryZoneFreeFrom = Annotated[float | None, Field(
    ge=0, le=DELIVERY_ZONE_AMOUNT_MAX,
    description="The items total from which delivery into the zone is free, "
    "in the store's currency; null: never free")]
DeliveryZoneCorners = Annotated[list[StoreLocation], Field(
    min_length=DELIVERY_ZONE_CORNERS_MIN,
    max_length=DELIVERY_ZONE_CORNERS_MAX,
    description='The corners in drawing order; the last joins the first')]
# The policy fields of a zone, beside its points.
DELIVERY_ZONE_RULES = ('name', 'deliveryFee', 'deliveryDays', 'minimumOrder',
                       'freeDeliveryFrom')


class DeliveryZone(AppModel):
    """One zone a store delivers to, with its own delivery policy
    (deliveryZoneProblem holds its shape, zonesOverlap keeps zones apart)."""
    name: DeliveryZoneName
    points: DeliveryZoneCorners
    deliveryFee: DeliveryZoneFee
    deliveryDays: DeliveryZoneDays
    minimumOrder: DeliveryZoneMinimumOrder = None
    freeDeliveryFrom: DeliveryZoneFreeFrom = None


class DeliveryZoneRule(ResponseModel):
    """A zone's delivery policy as a route sends it, without its shape.
    Each field restates DeliveryZone's, which test_route_response_models.py's
    MIRRORS keeps in step."""
    name: DeliveryZoneName
    deliveryFee: DeliveryZoneFee
    deliveryDays: DeliveryZoneDays
    minimumOrder: DeliveryZoneMinimumOrder = Field(...)
    freeDeliveryFrom: DeliveryZoneFreeFrom = Field(...)


class DeliveryZoneResponse(DeliveryZoneRule):
    """A zone as a route sends it: its policy and its shape."""
    points: list[StoreLocationResponse] = Field(
        ..., min_length=DELIVERY_ZONE_CORNERS_MIN,
        max_length=DELIVERY_ZONE_CORNERS_MAX,
        description='The corners in drawing order; the last joins the first')


class DeliveryZonesResponse(ResponseModel):
    """What setMyDeliveryZones sends: the store's delivery zones as saved."""
    deliveryZones: list[DeliveryZoneResponse] = Field(
        ..., max_length=DELIVERY_ZONES_MAX,
        description='Where the store delivers; empty: it takes no delivery')


class NewDeliveryPoint(AppModel):
    """A corner of a setMyDeliveryZones zone. Types only, no limits: the
    route checks them, never quoting a value (NewOrderDelivery's reason)."""
    latitude: float = Field(..., description='Degrees north')
    longitude: float = Field(..., description='Degrees east')


class NewDeliveryZone(AppModel):
    """A setMyDeliveryZones zone: its corners in drawing order and its
    delivery policy. Types only, no limits (NewDeliveryPoint's reason)."""
    name: str = Field(..., description='What the zone is called')
    points: list[NewDeliveryPoint] = Field(
        ..., description='The corners in drawing order; the last joins the '
        'first')
    deliveryFee: float = Field(..., description="The fee, in the store's "
                               'currency; 0: free')
    deliveryDays: int = Field(..., description='Days from payment until it '
                              'arrives')
    minimumOrder: float | None = Field(None, description='The smallest '
                                       'items total; null: none')
    freeDeliveryFrom: float | None = Field(
        None, description='The items total from which delivery is free; '
        'null: never')


Corner = tuple[float, float]


def _orientation(a: Corner, b: Corner, c: Corner) -> int:
    """Which way a, b, c turn: 1 left, -1 right, 0 in a line."""
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return (cross > 0) - (cross < 0)


def _onSegment(a: Corner, b: Corner, c: Corner) -> bool:
    """Whether c, in line with a and b, lies between them."""
    return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))


def _segmentsMeet(p1, p2, q1, q2) -> bool:
    """Whether the segments p1-p2 and q1-q2 touch or cross."""
    o1, o2 = _orientation(p1, p2, q1), _orientation(p1, p2, q2)
    o3, o4 = _orientation(q1, q2, p1), _orientation(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _onSegment(p1, p2, q1))
            or (o2 == 0 and _onSegment(p1, p2, q2))
            or (o3 == 0 and _onSegment(q1, q2, p1))
            or (o4 == 0 and _onSegment(q1, q2, p2)))


def _segmentsCross(p1, p2, q1, q2) -> bool:
    """Whether the segments cross through each other: touching at a corner
    or running along each other (a shared border) is no crossing."""
    o1, o2 = _orientation(p1, p2, q1), _orientation(p1, p2, q2)
    o3, o4 = _orientation(q1, q2, p1), _orientation(q1, q2, p2)
    return 0 not in (o1, o2, o3, o4) and o1 != o2 and o3 != o4


def _sides(corners: list[Corner]) -> list[tuple[Corner, Corner]]:
    count = len(corners)
    return [(corners[i], corners[(i + 1) % count]) for i in range(count)]


def deliveryZoneProblem(corners: list[Corner]) -> str | None:
    """What is wrong with a delivery zone's shape, the admin's to fix; None
    when it is a closed shape whose sides never cross. `corners` are
    (latitude, longitude) pairs in drawing order, flat on the map (a zone
    is a town or a city, never across the 180th meridian)."""
    if not DELIVERY_ZONE_CORNERS_MIN <= len(corners) <= \
            DELIVERY_ZONE_CORNERS_MAX:
        return (f'draw {DELIVERY_ZONE_CORNERS_MIN} to '
                f'{DELIVERY_ZONE_CORNERS_MAX} corners')
    if len(set(corners)) != len(corners):
        return 'a corner is placed twice'
    count = len(corners)
    sides = _sides(corners)
    for i in range(count):
        for j in range(i + 1, count):
            # Neighbouring sides share a corner, which is no crossing.
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segmentsMeet(*sides[i], *sides[j]):
                return 'its sides cross: draw it without crossing lines'
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in sides)
    if area == 0:
        return 'its corners lie in one line'
    return None


def _onBorder(point: Corner, corners: list[Corner]) -> bool:
    return any(_orientation(a, b, point) == 0 and _onSegment(a, b, point)
               for a, b in _sides(corners))


def _rayInside(point: Corner, corners: list[Corner]) -> bool:
    """Whether a ray from the point to the east crosses the shape's sides
    an odd number of times (a point on a side may go either way)."""
    inside = False
    latitude, longitude = point
    for a, b in _sides(corners):
        if (a[0] > latitude) != (b[0] > latitude):
            crossing = a[1] + (latitude - a[0]) * (b[1] - a[1]) / (b[0] - a[0])
            if longitude < crossing:
                inside = not inside
    return inside


def _strictlyInside(point: Corner, corners: list[Corner]) -> bool:
    return not _onBorder(point, corners) and _rayInside(point, corners)


def _holds(point: Corner, corners: list[Corner]) -> bool:
    """Whether the shape holds the point, its border included."""
    return _onBorder(point, corners) or _rayInside(point, corners)


def zonesOverlap(first: list[Corner], second: list[Corner]) -> bool:
    """Whether two valid zone shapes share any ground: their sides cross,
    or a corner or the middle of a side of one lies inside the other. Two
    zones that only share a border (or a corner) do not overlap."""
    if set(first) == set(second):
        return True
    for p1, p2 in _sides(first):
        for q1, q2 in _sides(second):
            if _segmentsCross(p1, p2, q1, q2):
                return True
    for one, other in ((first, second), (second, first)):
        probes = [*one, *(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                          for a, b in _sides(one))]
        if any(_strictlyInside(probe, other) for probe in probes):
            return True
    return False


def zoneCorners(zone: dict) -> list[Corner]:
    """A stored zone's corners as (latitude, longitude) pairs."""
    return [(p['latitude'], p['longitude']) for p in zone['points']]


def ruledZones(zones: list[dict] | None) -> list[dict]:
    """The stored zones that carry a delivery policy: one drawn before zones
    had policies (shape only) takes no delivery until it is drawn again."""
    return [zone for zone in zones or []
            if all(rule in zone for rule in DELIVERY_ZONE_RULES)]


def deliveryZoneAt(latitude: float, longitude: float,
                   zones: list[dict] | None) -> dict | None:
    """The zone (as stored) a pin falls in, its border included: the first
    one listed, so a pin on a shared border has one policy. None outside
    every zone."""
    point = (latitude, longitude)
    return next((zone for zone in ruledZones(zones)
                 if _holds(point, zoneCorners(zone))), None)


def deliveryFeeFor(zone: dict, itemsTotal: float) -> float:
    """What a delivery of items worth `itemsTotal` into `zone` costs: its
    fee, nothing from its freeDeliveryFrom on."""
    freeFrom = zone.get('freeDeliveryFrom')
    if freeFrom is not None and itemsTotal >= freeFrom:
        return 0
    return zone['deliveryFee']


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
    deliveryZones: list[DeliveryZone] | None = Field(
        None, max_length=DELIVERY_ZONES_MAX,
        description='Where it delivers, drawn on the map, each zone with its '
        'own delivery policy; null or empty: no delivery')
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

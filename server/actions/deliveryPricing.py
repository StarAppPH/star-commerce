"""deliveryPricing: what a delivery costs.

A delivery costs DELIVERY_USD_PER_KM US dollars per km, in a straight line
(haversine) from the store's pin to the customer's, converted to the
store's currency at the day's rate, and nothing from the store's
freeDeliveryMinimum on. The km is rounded to 0.1 first, so the order's
deliveryDistanceKm times the rate is its fee.

The rates come from exchangeRates.fetchUsdRates, cached in the one
ExchangeRates document: used while younger than RATES_FRESH_FOR, refetched
after. When a refetch fails, rates younger than RATES_USABLE_FOR still
price a delivery (logged); older ones, or none, are its 503 ("Delivery
pricing is unavailable right now"). A US dollar store needs no rate.

The clock: every read of now is _now(), which reads this module's
`datetime`, so a test freezes it by replacing deliveryPricing.datetime.
"""

import datetime
import logging
import math

import exchangeRates
from mongoDb import db
from objects import PRODUCT_PRICE_MAX, USD_RATES_ID, ExchangeRates
from services.__request import ServiceUnavailable
from sharedConstants import DELIVERY_USD_PER_KM

EXCHANGE_RATES = 'ExchangeRates'
RATES_FRESH_FOR = datetime.timedelta(hours=24)
RATES_USABLE_FOR = datetime.timedelta(days=7)
EARTH_RADIUS_KM = 6371.0088
TOO_FAR = ('That address is too far from the store to deliver to: choose '
           'pickup, or an address nearer the store')


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _cachedRates() -> dict | None:
    """The cached ExchangeRates document (rates, fetchedAt), or None."""
    return db.read({'_id': USD_RATES_ID}, EXCHANGE_RATES,
                   projection={'rates': 1, 'fetchedAt': 1}, findOne=True)


def _saveRates(rates: dict[str, float], fetchedAt: datetime.datetime) -> None:
    stored = ExchangeRates(_id=USD_RATES_ID, rates=rates,
                           fetchedAt=fetchedAt)
    db.findOneAndUpdate(
        {'_id': USD_RATES_ID},
        {'$set': stored.model_dump(include={'rates', 'fetchedAt'}),
         '$inc': {'_version': 1}},
        EXCHANGE_RATES, upsert=True, projection={'_id': 1})


def usdRate(currency: str) -> float:
    """Units of `currency` per US dollar today (see the module
    docstring), or ServiceUnavailable."""
    if currency == 'USD':
        return 1.0
    now = _now()
    cached = _cachedRates()
    age = now - cached['fetchedAt'] if cached else None
    if age is not None and age < RATES_FRESH_FOR and currency in cached['rates']:
        return cached['rates'][currency]
    try:
        rates = exchangeRates.fetchUsdRates()
    except ServiceUnavailable:
        if (age is not None and age < RATES_USABLE_FOR
                and currency in cached['rates']):
            logging.warning({'event': 'exchangeRatesStale',
                             'ageHours': round(age.total_seconds() / 3600)})
            return cached['rates'][currency]
        raise
    _saveRates(rates, now)
    return rates[currency]


def feePerKm(currency: str) -> float:
    """What a delivery costs per km in `currency`, unrounded, or
    ServiceUnavailable."""
    return DELIVERY_USD_PER_KM * usdRate(currency)


def shownFeePerKm(currency: str) -> float | None:
    """feePerKm to 2 decimals, to show; None while no rate can be read (the
    page then says what it costs in US dollars)."""
    try:
        return round(feePerKm(currency), 2)
    except ServiceUnavailable:
        return None


def distanceKm(fromLatitude: float, fromLongitude: float,
               toLatitude: float, toLongitude: float) -> float:
    """The straight-line (great circle) km between two pins, to 0.1 km."""
    lat1, lat2 = math.radians(fromLatitude), math.radians(toLatitude)
    dLat = lat2 - lat1
    dLon = math.radians(toLongitude - fromLongitude)
    a = (math.sin(dLat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dLon / 2) ** 2)
    return round(2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a))), 1)


def deliveryFee(itemsTotal: float, km: float, perKm: float,
                freeDeliveryMinimum: float | None) -> float:
    """What a delivery of items worth `itemsTotal` over `km` costs at
    `perKm`: nothing from the free delivery minimum on. A fee over what an
    order can hold is TOO_FAR."""
    if freeDeliveryMinimum is not None and itemsTotal >= freeDeliveryMinimum:
        return 0
    fee = round(km * perKm, 2)
    if fee > PRODUCT_PRICE_MAX:
        raise ValueError(TOO_FAR)
    return fee

"""ExchangeRates: the day's US dollar rates, cached for delivery pricing.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field

from objects.base import MongoModel, StoredDocumentKeys


# The one ExchangeRates document (its _id): rates from one US dollar.
USD_RATES_ID = 'USD'


class ExchangeRates(MongoModel):
    """The ExchangeRates document of USD_RATES_ID: what one US dollar buys
    in each currency, as exchangeRates.fetchUsdRates read it at fetchedAt
    (actions/deliveryPricing.py refreshes it daily)."""
    rates: dict[str, float] = Field(
        ..., description='Units of each currency (ISO 4217) per US dollar')
    fetchedAt: datetime.datetime = Field(
        ..., description='When the rates were read')


class ExchangeRatesView(StoredDocumentKeys, ExchangeRates):
    """An ExchangeRates document as stored."""

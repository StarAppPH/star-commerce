"""Delivery pricing (actions/deliveryPricing.py, exchangeRates.py): the
straight-line km between two pins, the fee per km in the store's currency,
the daily rate cache and what happens when the rates API fails.

No test reaches the network: conftest's _fakeExchangeRates answers
TEST_USD_RATES, and the tests here replace its answer."""

import datetime
import json

import pytest
import requests

import actions.deliveryPricing as deliveryPricing
import exchangeRates
from conftest import TEST_USD_RATES, FakeRatesSession, wipeCollections
from mongoDb import db
from objects import PRODUCT_PRICE_MAX, USD_RATES_ID
from services.__request import ServiceUnavailable

NOW = datetime.datetime(2026, 10, 29, 6, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def rates(monkeypatch, _fakeExchangeRates):
    """No cached rates, the clock frozen at NOW, and the fake session."""
    wipeCollections(['ExchangeRates'])
    clock = {'now': NOW}

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['now']

    monkeypatch.setattr(deliveryPricing.datetime, 'datetime', FrozenDatetime)
    yield _fakeExchangeRates, clock
    wipeCollections(['ExchangeRates'])


def cache(rates: dict, fetchedAt: datetime.datetime) -> None:
    db.db['ExchangeRates'].replace_one(
        {'_id': USD_RATES_ID},
        {'rates': rates, 'fetchedAt': fetchedAt, '_version': 0}, upsert=True)


def install(monkeypatch, session: FakeRatesSession) -> FakeRatesSession:
    monkeypatch.setattr(exchangeRates, 'httpSession', lambda: session)
    return session


# ----------------------------- distance and fee ----------------------------- #


def test_distanceKm_isTheStraightLine_toATenthOfAKm():
    # Manila to Cebu City: about 571 km as the crow flies.
    assert deliveryPricing.distanceKm(14.5995, 120.9842, 10.3157,
                                      123.8854) == 571.0
    assert deliveryPricing.distanceKm(14.6, 121.0, 14.6, 121.0) == 0
    # Antipodes: half the Earth's circumference.
    assert deliveryPricing.distanceKm(14.5547, 121.0244, -14.5547,
                                      -58.9756) == 20015.1


def test_deliveryFee_isKmTimesThePrice_freeFromTheMinimum():
    assert deliveryPricing.deliveryFee(100, 10.7, 8.7, None) == 93.09
    assert deliveryPricing.deliveryFee(999.99, 10.7, 8.7, 1000) == 93.09
    assert deliveryPricing.deliveryFee(1000, 10.7, 8.7, 1000) == 0
    assert deliveryPricing.deliveryFee(5, 0, 8.7, None) == 0


def test_aFeeOverWhatAnOrderHolds_isTooFar():
    # Half the world away in dong: 20015.1 km at 3750 VND a km.
    with pytest.raises(ValueError, match='^That address is too far'):
        deliveryPricing.deliveryFee(5, 20015.1, 0.15 * 25000, None)
    assert deliveryPricing.deliveryFee(
        5, 1, PRODUCT_PRICE_MAX, None) == PRODUCT_PRICE_MAX


def test_feePerKm_isTheDollarRate_inTheStoresCurrency(rates):
    session, _ = rates

    assert deliveryPricing.shownFeePerKm('PHP') == 8.7
    assert deliveryPricing.shownFeePerKm('JPY') == 22.5
    assert deliveryPricing.shownFeePerKm('VND') == 3750
    assert deliveryPricing.shownFeePerKm('USD') == 0.15
    # One fetch, cached for the rest.
    assert session.calls == 1


def test_aDollarStore_needsNoRate(rates):
    session, _ = rates
    session.error = requests.ConnectionError()

    assert deliveryPricing.feePerKm('USD') == 0.15
    assert session.calls == 0


# ----------------------------- the rate cache ----------------------------- #


def test_theRates_areFetchedOnce_andKeptWithTheirTime(rates):
    session, clock = rates

    assert deliveryPricing.usdRate('PHP') == 58.0
    clock['now'] = NOW + datetime.timedelta(hours=23)
    assert deliveryPricing.usdRate('EUR') == 0.92

    assert session.calls == 1
    stored = db.read({'_id': USD_RATES_ID}, 'ExchangeRates', findOne=True)
    assert stored['rates'] == TEST_USD_RATES
    assert stored['fetchedAt'] == NOW
    assert stored['_version'] == 1


def test_aDayOld_theRatesAreFetchedAgain(rates):
    session, clock = rates
    cache({**TEST_USD_RATES, 'PHP': 50.0}, NOW - datetime.timedelta(hours=24))

    assert deliveryPricing.usdRate('PHP') == 58.0

    assert session.calls == 1
    stored = db.read({'_id': USD_RATES_ID}, 'ExchangeRates', findOne=True)
    assert (stored['rates']['PHP'], stored['fetchedAt']) == (58.0, NOW)


def test_whenAFetchFails_ratesUpToAWeekOld_stillPrice(rates):
    session, _ = rates
    session.error = requests.Timeout()
    cache({**TEST_USD_RATES, 'PHP': 50.0},
          NOW - datetime.timedelta(days=7) + datetime.timedelta(minutes=1))

    assert deliveryPricing.usdRate('PHP') == 50.0
    assert deliveryPricing.shownFeePerKm('PHP') == 7.5


def test_whenAFetchFails_olderRatesOrNone_areUnavailable(rates):
    session, _ = rates
    session.error = requests.ConnectionError()

    with pytest.raises(ServiceUnavailable) as error:
        deliveryPricing.usdRate('PHP')
    assert error.value.service == 'delivery pricing'
    # A price to show is then none: the page shows the US dollar rate.
    assert deliveryPricing.shownFeePerKm('PHP') is None

    cache(TEST_USD_RATES, NOW - datetime.timedelta(days=7))
    with pytest.raises(ServiceUnavailable):
        deliveryPricing.usdRate('PHP')


# ----------------------------- the rates API ----------------------------- #


@pytest.mark.parametrize('session', [
    FakeRatesSession(status=500),
    FakeRatesSession(status=302),
    FakeRatesSession(body=b'not json'),
    FakeRatesSession(body=json.dumps({'result': 'error'}).encode()),
    FakeRatesSession(body=json.dumps({'result': 'success'}).encode()),
    FakeRatesSession(body=json.dumps({
        'result': 'success',
        'rates': {k: v for k, v in TEST_USD_RATES.items() if k != 'AED'},
    }).encode()),
    FakeRatesSession(body=json.dumps({
        'result': 'success', 'rates': {**TEST_USD_RATES, 'PHP': 0},
    }).encode()),
    FakeRatesSession(body=json.dumps({
        'result': 'success', 'rates': {**TEST_USD_RATES, 'PHP': '58'},
    }).encode()),
    FakeRatesSession(body=json.dumps({
        'result': 'success', 'rates': {**TEST_USD_RATES, 'PHP': True},
    }).encode()),
    FakeRatesSession(body=b' ' * (exchangeRates.MAX_BODY_BYTES + 1)),
    FakeRatesSession(error=requests.ConnectionError()),
])
def test_anAnswerThatIsNotEveryRate_isUnavailable(monkeypatch, session):
    install(monkeypatch, session)

    with pytest.raises(ServiceUnavailable):
        exchangeRates.fetchUsdRates()


def test_theRatesAPI_isCalledAtItsFixedURL_withoutRedirects(monkeypatch):
    seen = []

    class Recording(FakeRatesSession):
        def get(self, url, **kwargs):
            seen.append((url, kwargs))
            return super().get(url, **kwargs)

    install(monkeypatch, Recording())

    assert exchangeRates.fetchUsdRates() == TEST_USD_RATES
    [(url, kwargs)] = seen
    assert url == 'https://open.er-api.com/v6/latest/USD'
    assert kwargs['allow_redirects'] is False
    assert kwargs['timeout'] == exchangeRates.TIMEOUT_SECONDS

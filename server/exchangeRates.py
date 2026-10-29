"""exchangeRates.py — the day's US dollar exchange rates (hand-written, no
database), for pricing a delivery in the store's currency.

fetchUsdRates() reads ExchangeRate-API's open endpoint
(https://open.er-api.com/v6/latest/USD): free, no key, updated once a day,
and covering every StoreCurrency. One GET to a fixed URL through
httpSession() (which a test replaces), redirects off, with a short timeout.
Anything but a 2xx JSON body with "result": "success" and a positive rate
for every StoreCurrency is unavailable(), a 503 ("Delivery pricing is
unavailable right now"). actions/deliveryPricing.py caches the rates, so
this runs about once a day.
"""

import json
import logging
import math
import threading
import typing
from contextlib import closing

import requests

from objects import StoreCurrency
from services.__request import ServiceUnavailable

RATES_URL = 'https://open.er-api.com/v6/latest/USD'
RATES_SERVICE = 'delivery pricing'
TIMEOUT_SECONDS = 5
# The documented body is about 3 KB.
MAX_BODY_BYTES = 64_000
READ_CHUNK_BYTES = 8192
CURRENCIES = typing.get_args(StoreCurrency)

_session: requests.Session | None = None
_sessionLock = threading.Lock()


def unavailable() -> ServiceUnavailable:
    """The 503 of every rate failure (routeErrors)."""
    return ServiceUnavailable(RATES_SERVICE, 'rates')


def httpSession() -> requests.Session:
    """The one Session of every rate call, built once (gunicorn runs
    threads, hence the lock). trust_env is off: no .netrc or proxy
    variable applies."""
    global _session
    with _sessionLock:
        if _session is None:
            session = requests.Session()
            session.trust_env = False
            _session = session
        return _session


def _validRates(body) -> dict[str, float]:
    """The rate of every StoreCurrency in `body`, or unavailable()."""
    if not isinstance(body, dict) or body.get('result') != 'success':
        raise unavailable()
    rates = body.get('rates')
    if not isinstance(rates, dict):
        raise unavailable()
    valid = {}
    for currency in CURRENCIES:
        rate = rates.get(currency)
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or rate <= 0):
            raise unavailable()
        valid[currency] = float(rate)
    return valid


def fetchUsdRates() -> dict[str, float]:
    """Units of each StoreCurrency per US dollar, today; unavailable() when
    the API does not answer them (logged by class and status only)."""
    try:
        response = httpSession().get(RATES_URL, timeout=TIMEOUT_SECONDS,
                                     allow_redirects=False, stream=True)
        with closing(response):
            if response.status_code != 200:
                logging.error({'event': 'exchangeRatesRefused',
                               'status': response.status_code})
                raise unavailable()
            chunks, size = [], 0
            for chunk in response.iter_content(READ_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise unavailable()
                chunks.append(chunk)
            return _validRates(json.loads(b''.join(chunks)))
    except ServiceUnavailable:
        raise
    except (requests.RequestException, ValueError) as error:
        logging.error({'event': 'exchangeRatesFailed',
                       'error': type(error).__name__})
        raise unavailable() from None

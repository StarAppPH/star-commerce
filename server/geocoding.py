"""geocoding.py — a map pin's street address (hand-written, no database),
suggested to the admin when they pin their store.

reverseGeocode() asks Google's Geocoding API
(https://maps.googleapis.com/maps/api/geocode/json?latlng=...) with
GOOGLE_MAPS_API_KEY, a server key restricted to the Geocoding API. One GET
to a fixed URL through httpSession() (which a test replaces), redirects off,
with a short timeout. It only suggests: a missing key, a failed call, no
street-level result or an address too long for a store is None, never an
error, so a pin is never refused for it. Nothing logs the pin, the address
or the key (the key is in the URL, so never the URL either): errors are
logged by class and status only.
"""

import json
import logging
import os
import threading
from contextlib import closing

import requests

from objects import STORE_ADDRESS_MAX_LENGTH

GEOCODE_URL = 'https://maps.googleapis.com/maps/api/geocode/json'
KEY_VARIABLE = 'GOOGLE_MAPS_API_KEY'
TIMEOUT_SECONDS = 5
# A reverse geocode lists about 10 results: some tens of KB.
MAX_BODY_BYTES = 512_000
READ_CHUNK_BYTES = 8192
# A plus code ("8VXC+XX Cebu City") is no street address.
NOT_AN_ADDRESS = 'plus_code'

_session: requests.Session | None = None
_sessionLock = threading.Lock()


def httpSession() -> requests.Session:
    """The one Session of every geocoding call, built once (gunicorn runs
    threads, hence the lock). trust_env is off: no .netrc or proxy
    variable applies."""
    global _session
    with _sessionLock:
        if _session is None:
            session = requests.Session()
            session.trust_env = False
            _session = session
        return _session


def _key() -> str | None:
    key = (os.getenv(KEY_VARIABLE) or '').strip()
    if not key or not key.isascii() or not key.isprintable() or ' ' in key:
        return None
    return key


def _firstAddress(body) -> str | None:
    """The formatted address of the first street-level result in `body`."""
    if not isinstance(body, dict) or body.get('status') != 'OK':
        if isinstance(body, dict) and body.get('status') != 'ZERO_RESULTS':
            logging.error({'event': 'geocodingRefused',
                           'status': str(body.get('status'))[:40]})
        return None
    results = body.get('results')
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        types = result.get('types')
        if isinstance(types, list) and NOT_AN_ADDRESS in types:
            continue
        address = result.get('formatted_address')
        if isinstance(address, str) and address.strip():
            address = address.strip()
            return address if len(address) <= STORE_ADDRESS_MAX_LENGTH else None
    return None


def reverseGeocode(latitude: float, longitude: float) -> str | None:
    """The street address Google gives for the pin, or None."""
    key = _key()
    if key is None:
        return None
    try:
        response = httpSession().get(
            GEOCODE_URL,
            params={'latlng': f'{latitude:.7f},{longitude:.7f}',
                    'language': 'en', 'key': key},
            timeout=TIMEOUT_SECONDS, allow_redirects=False, stream=True)
        with closing(response):
            if response.status_code != 200:
                logging.error({'event': 'geocodingRefused',
                               'status': response.status_code})
                return None
            chunks, size = [], 0
            for chunk in response.iter_content(READ_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    logging.error({'event': 'geocodingBodyTooLarge'})
                    return None
                chunks.append(chunk)
            return _firstAddress(json.loads(b''.join(chunks)))
    except (requests.RequestException, ValueError) as error:
        logging.error({'event': 'geocodingFailed',
                       'error': type(error).__name__})
        return None

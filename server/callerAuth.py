"""Who may call the routes a machine calls: Pub/Sub pushes and sibling services.

Two kinds of route answer another machine, never a browser:
  - every Pub/Sub push handler (appPubSub.py), decorated @requirePushCaller;
  - every route declared @route_config(serviceOnly=True) (the routes in
    actions/ServiceToServiceActions.py), decorated @requireServiceCaller.
AppCreator emits the decorator under the route's @route line, and
test_callerAuth.py pins that it does.

Both require `Authorization: Bearer <Google-signed ID token>`:
  - Pub/Sub signs one for every push (pubSubAdmin.py configures the
    subscription's OIDC token with PUSH_SERVICE_ACCOUNTS[0], and the push
    endpoint as its audience);
  - a sibling's services/__request.py fetches one on Cloud Run for the
    sibling's URL (_authHeaders).
The token must be RS256, signed by one of Google's current certs, unexpired,
issued by accounts.google.com, carry a verified email in the allowlist, and
be minted for THIS request:
  - a push: `aud` is exactly https://<this request's host><path>, so a token
    for one handler, or for the same path on another service, is refused;
  - a service call: `aud` (a trailing / ignored) is https://<this host>.
Cloud Run's front end routes on the Host header, so a request whose Host is
another service's never reaches this one; binding to it accepts both forms of
this service's run.app URL with no URL configured anywhere.

A refusal is a 401 {"message": "Unauthorized", "status": 401} (Pub/Sub then
retries and dead-letters). The log carries why (a short reason), the route
and the caller's User-Agent: never the token or its claims.

Skipped in localdev only (tests, the local dev servers, devPubSub, which
only POSTs to loopback). Everywhere else it fails closed, except while
CALLER_AUTH_MODE=report is set on the service together with an unexpired
CALLER_AUTH_REPORT_UNTIL: then a refusal is logged and the request still
runs. That is the rollout phase (callers start sending tokens, the log shows
who still does not), and it ends by itself:
  - CALLER_AUTH_REPORT_UNTIL is a date (report mode runs through that day,
    UTC) or a datetime with its offset, at most REPORT_MAX_DAYS ahead;
  - after it, or when it is unset, unreadable (a datetime without an
    offset included) or further ahead than that, the check enforces, so
    report mode cannot be parked;
  - any other mode, or none, enforces.
Report mode logs a WARNING when the module loads (at startup), so a service
left in it shows up in the log.

callerVerified() tells a route whether THIS request's caller passed the
check: a request localdev or report mode let through without one did not.
A route that acts with its own credentials on what the caller sent (the
dead-letter dashboard's replay) asks it first.
"""
import datetime
import logging
import os
import threading
import time
from functools import wraps
from urllib.parse import urlsplit

import requests

# >>> star-app per-repo: callerAuth accounts
# The service accounts whose ID tokens are accepted. Empty (a new project)
# refuses every caller outside localdev, and pubSubAdmin.py refuses to run.
# PUSH_SERVICE_ACCOUNTS[0] is the one pubSubAdmin.py gives every push
# subscription's OIDC token; any other entry may replay a push (a
# dead-letter dashboard's runtime account).
PUSH_SERVICE_ACCOUNTS: tuple[str, ...] = ()
# The runtime service accounts of the sibling services (and their jobs) that
# call this one's serviceOnly routes.
SERVICE_CALLER_ACCOUNTS: tuple[str, ...] = ()
# <<< star-app per-repo: callerAuth accounts

MODE_ENV = 'CALLER_AUTH_MODE'
REPORT_MODE = 'report'
REPORT_UNTIL_ENV = 'CALLER_AUTH_REPORT_UNTIL'
REPORT_MAX_DAYS = 30

GOOGLE_CERTS_URL = 'https://www.googleapis.com/oauth2/v1/certs'
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
CLOCK_SKEW_SECONDS = 10
# Google rotates its signing keys over days; the certs are refetched hourly,
# at most once a minute however often a fetch fails or an unknown key id
# shows up, and the last good certs serve a failed refetch for a grace period.
CERTS_MAX_AGE_SECONDS = 3600
CERTS_MIN_FETCH_INTERVAL_SECONDS = 60
CERTS_GRACE_SECONDS = 6 * 3600
CERTS_FETCH_TIMEOUT = (5, 10)

UNAUTHORIZED_STATUS = 401
UNAUTHORIZED_MESSAGE = 'Unauthorized'

_certsLock = threading.Lock()
_certsCache = {'certs': None, 'fetchedAt': None, 'attemptedAt': None}


class CallerRejected(Exception):
    """The request carries no token this route accepts. `reason` is a short
    code that is safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validCerts(certs) -> bool:
    return (isinstance(certs, dict) and bool(certs) and all(
        isinstance(kid, str) and isinstance(pem, str)
        and 'BEGIN CERTIFICATE' in pem for kid, pem in certs.items()))


def _fetchCerts() -> dict:
    response = requests.get(GOOGLE_CERTS_URL, timeout=CERTS_FETCH_TIMEOUT)
    response.raise_for_status()
    certs = response.json()
    if not _validCerts(certs):
        raise ValueError('Google certs answer has the wrong shape')
    return certs


def _monotonic() -> float:
    return time.monotonic()


def _googleCerts(keyId: str) -> dict:
    """Google's signing certs, holding `keyId` when a fetch allows it."""
    now = _monotonic()
    with _certsLock:
        cache = _certsCache
        certs = cache['certs']
        age = None if certs is None else now - cache['fetchedAt']
        if age is not None and age < CERTS_MAX_AGE_SECONDS and keyId in certs:
            return certs
        attempted = cache['attemptedAt']
        if attempted is None or now - attempted >= CERTS_MIN_FETCH_INTERVAL_SECONDS:
            cache['attemptedAt'] = now
            try:
                certs = _fetchCerts()
                cache['certs'] = certs
                cache['fetchedAt'] = now
                return certs
            except Exception as e:
                logging.warning({'event': 'callerAuthCertsFetchFailed',
                                 'error': type(e).__name__})
        if age is not None and age < CERTS_MAX_AGE_SECONDS + CERTS_GRACE_SECONDS:
            return certs
    raise CallerRejected('certsUnavailable')


def _bearerToken(authorization: str | None) -> str:
    if not authorization:
        raise CallerRejected('missingToken')
    scheme, _, token = authorization.strip().partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token:
        raise CallerRejected('notBearer')
    return token


def _verifiedClaims(token: str) -> dict:
    from google.auth import jwt as googleJwt
    try:
        header = googleJwt.decode_header(token)
    except Exception:
        raise CallerRejected('malformedToken') from None
    if not isinstance(header, dict):
        raise CallerRejected('malformedToken')
    keyId = header.get('kid')
    if header.get('alg') != 'RS256' or not isinstance(keyId, str):
        raise CallerRejected('wrongAlgorithm')
    certs = _googleCerts(keyId)
    if keyId not in certs:
        raise CallerRejected('unknownKey')
    try:
        claims = googleJwt.decode(token,
                                  certs={keyId: certs[keyId]},
                                  verify=True,
                                  clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
    except Exception:
        raise CallerRejected('invalidToken') from None
    if not isinstance(claims, dict):
        raise CallerRejected('invalidToken')
    return claims


def _audienceMatches(audience, host: str, path: str, push: bool) -> bool:
    if not isinstance(audience, str) or not host:
        return False
    if push:
        return audience == f'https://{host}{path}'
    parts = urlsplit(audience)
    return (parts.scheme == 'https' and parts.netloc == host
            and parts.path in ('', '/') and not parts.query
            and not parts.fragment)


def verifyCaller(authorization: str | None, host: str, path: str,
                 allowedEmails, push: bool) -> dict:
    """The verified claims of the caller's ID token, or CallerRejected."""
    if not allowedEmails:
        raise CallerRejected('noAllowedCallers')
    claims = _verifiedClaims(_bearerToken(authorization))
    if claims.get('iss') not in GOOGLE_ISSUERS:
        raise CallerRejected('wrongIssuer')
    if not _audienceMatches(claims.get('aud'), host, path, push):
        raise CallerRejected('wrongAudience')
    if claims.get('email_verified') is not True:
        raise CallerRejected('emailNotVerified')
    if claims.get('email') not in allowedEmails:
        raise CallerRejected('callerNotAllowed')
    return claims


def _isLocalDev() -> bool:
    try:
        from AppConfig import Environment
        return Environment().getisLocalDevEnvironment()
    except Exception:
        return False


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _reportUntil() -> datetime.datetime | None:
    """When report mode ends, from CALLER_AUTH_REPORT_UNTIL; None when it is
    unset, unreadable, or more than REPORT_MAX_DAYS ahead."""
    value = (os.getenv(REPORT_UNTIL_ENV) or '').strip()
    try:
        if len(value) == len('YYYY-MM-DD'):
            day = datetime.date.fromisoformat(value)
            until = datetime.datetime.combine(
                day + datetime.timedelta(days=1), datetime.time(),
                datetime.timezone.utc)
        else:
            until = datetime.datetime.fromisoformat(value)
        if until.tzinfo is None or until - _now() > datetime.timedelta(
                days=REPORT_MAX_DAYS):
            return None
    # 9999-12-31 overflows: unreadable, never a crash at startup
    except (ValueError, OverflowError):
        return None
    return until


def _reportModeAsked() -> bool:
    return (os.getenv(MODE_ENV) or '').strip().lower() == REPORT_MODE


def _reportOnly() -> bool:
    if not _reportModeAsked():
        return False
    until = _reportUntil()
    return until is not None and _now() < until


def warnIfReportMode():
    """A WARNING while the service is set to report mode, whether it still
    applies (until when) or no longer does (expired, or no readable end)."""
    if not _reportModeAsked():
        return
    until = _reportUntil()
    if until is None:
        state = 'enforcing: no readable ' + REPORT_UNTIL_ENV
    elif _now() < until:
        state = 'refusals are logged, not enforced'
    else:
        state = 'enforcing: report mode expired'
    logging.warning({
        'event': 'callerAuthReportMode',
        'state': state,
        'reportUntil': None if until is None else until.isoformat(),
    })


def callerVerified() -> bool:
    """Whether this request's caller passed requirePushCaller or
    requireServiceCaller, not merely let through by localdev or report
    mode."""
    from flask import g
    return g.get('callerAuthVerified') is True


def _guard(push: bool):

    def decorator(func):

        @wraps(func)
        def wrapper(*args, **kwargs):
            if _isLocalDev():
                return func(*args, **kwargs)
            from flask import g, jsonify, request
            allowed = PUSH_SERVICE_ACCOUNTS if push else SERVICE_CALLER_ACCOUNTS
            try:
                verifyCaller(request.headers.get('Authorization'),
                             request.host, request.path, allowed, push)
            except CallerRejected as e:
                reportOnly = _reportOnly()
                logging.warning({
                    'event': 'callerRejected',
                    'function': func.__name__,
                    'caller': 'push' if push else 'service',
                    'reason': e.reason,
                    'mode': REPORT_MODE if reportOnly else 'enforce',
                    'userAgent': (request.headers.get('User-Agent') or '')[:120],
                })
                if not reportOnly:
                    return jsonify({'message': UNAUTHORIZED_MESSAGE,
                                    'status': UNAUTHORIZED_STATUS}), UNAUTHORIZED_STATUS
            else:
                g.callerAuthVerified = True
            return func(*args, **kwargs)

        wrapper.callerAuth = 'push' if push else 'service'
        return wrapper

    return decorator


requirePushCaller = _guard(push=True)
requireServiceCaller = _guard(push=False)

warnIfReportMode()

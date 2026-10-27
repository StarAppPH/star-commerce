"""The one HTTP client for calls to a sibling service.

Every call goes through one `requests.Session` (connections are reused) and
raises one of two exceptions, never a raw `requests` one:
  - ServiceError: the sibling answered with a non-2xx status. `message` is the
    `message` of its ApiResponse envelope (a sibling's 400 carries its own
    user-facing ValueError text), or None.
  - ServiceUnavailable: no answer at all (connection refused, DNS, timeout).
Neither puts the sibling's URL in its str(), and ServiceUnavailable is raised
`from None`, so the URL-bearing `requests` error never reaches a log or a user.
str(ServiceError) does not carry the sibling's message either (an old sibling
may still answer with raw exception text): only `.message` does, which
routeErrors reads for a 400. Never log an error's `.request` (the URL).
routeErrors.py turns a ServiceError a sibling answered with 400 into a 400
carrying the sibling's message, and every other one into a 503.

ServiceError is a requests.HTTPError (with `.response`) and ServiceUnavailable
a requests.RequestException, so code that catches those still catches these.

_authHeaders(baseUrl) is the one place a header every call carries is added:
on Cloud Run, a Google ID token for the sibling's URL (its audience), which
the sibling's callerAuth.py checks on a serviceOnly route. A loopback URL (a
local dev server) and a local run get none: a local machine's user
credentials cannot mint one. It is merged last, so a caller's `headers=` can
add a header but never replace one the hook sets.

_ensureTestService(baseUrl) is the one hook the test suite has on sibling
calls: in a test run (conftest sets STAR_TEST_SERVICES_AUTOSTART=1 and
STAR_TEST_SERVER_OWNER) under localdev, a call to a sibling's dev URL first
starts that sibling's server when nothing answers there
(server/testServices.py, which stops it when the run ends). Every other run
skips it. So a sibling call goes through
request(), never around it (test_testServices.py checks).
"""
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

import requests

from .__abstractService import Path
from .__getIsProductionEnv import getIsProductionEnv

# (connect, read) seconds: a read can never hang forever, and a slow sibling
# call is not cut short. A caller passes its own `timeout` to tighten it.
DEFAULT_TIMEOUT = (5, 60)

_session = requests.Session()


class ServiceError(requests.HTTPError):
    """A sibling answered `status` (not 2xx) to `route`."""

    def __init__(self, service: str, route: str, status: int,
                 message: str | None, response=None):
        self.service = service
        self.route = route
        self.status = status
        self.message = message
        super().__init__(f'{service} {route} answered {status}',
                         response=response)


class ServiceUnavailable(requests.RequestException):
    """`route` on a sibling did not answer (refused, unreachable, timed out)."""

    def __init__(self, service: str, route: str):
        self.service = service
        self.route = route
        super().__init__(f'{service} {route} did not answer')


# A cached ID token is refetched this long before it expires (Google's last
# an hour), so one never expires on its way to the sibling.
ID_TOKEN_REFRESH_MARGIN_SECONDS = 300

_idTokens: dict[str, tuple[str, float]] = {}


def _isLoopback(baseUrl: str) -> bool:
    host = urlsplit(baseUrl).hostname or ''
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _isCloudEnvironment() -> bool:
    from AppConfig import Environment
    return Environment().getIsCloudEnvironment()


def _fetchIdToken(audience: str) -> tuple[str, float]:
    """A Google ID token for `audience` (from the metadata server on Cloud
    Run) and its expiry. Raises when none can be minted."""
    import google.auth.transport.requests
    import google.oauth2.id_token
    from google.auth import jwt as googleJwt
    token = google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience)
    expiry = googleJwt.decode(token, verify=False)['exp']
    return token, float(expiry)


def _authHeaders(baseUrl: str) -> dict:
    """Headers every call to `baseUrl` carries besides Content-Type."""
    if _isLoopback(baseUrl) or not _isCloudEnvironment():
        return {}
    audience = baseUrl.rstrip('/')
    cached = _idTokens.get(audience)
    if cached is None or cached[1] - ID_TOKEN_REFRESH_MARGIN_SECONDS <= time.time():
        cached = _fetchIdToken(audience)
        _idTokens[audience] = cached
    return {'Authorization': f'Bearer {cached[0]}'}


def _ensureTestService(baseUrl: str) -> None:
    """In a test run, start the sibling at `baseUrl` if it is down. The
    run's conftest sets both variables (a dev server started by hand with the
    flag exported has no owner: it starts nothing). A test that swapped
    `_session` for a fake sends nothing: nothing to start."""
    if (os.environ.get('STAR_TEST_SERVICES_AUTOSTART') != '1'
            or not os.environ.get('STAR_TEST_SERVER_OWNER')
            or os.environ.get('ENVIRONMENT') != 'localdev'
            or not isinstance(_session, requests.Session)):
        return
    import testServices
    testServices.ensureRunning(baseUrl)


def _envelopeMessage(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    message = body.get('message') if isinstance(body, dict) else None
    return message if isinstance(message, str) and message else None


def request(path: Path,
            functionName: str,
            method: str,
            data: dict,
            useProd: bool = False,
            headers: dict | None = None,
            service: str | None = None,
            timeout=DEFAULT_TIMEOUT) -> dict:
    """Call `functionName` on the sibling at `path` and return its JSON body
    (the ApiResponse envelope). `service` names the sibling in errors;
    `headers` adds to the request's headers, never over _authHeaders'."""
    service = service or 'A connected service'
    if getIsProductionEnv():
        useProd = True
    else:
        useProd = useProd is True

    method = method.upper()
    if method == 'GET':
        data = None
    if method == 'POST':
        data = json.dumps(data, default=str)

    baseUrl = path.prod if useProd else path.dev
    if not useProd:
        _ensureTestService(baseUrl)
    requestHeaders = {"Content-Type": "application/json",
                      **(headers or {}), **_authHeaders(baseUrl)}

    try:
        response = _session.request(method,
                                    baseUrl + functionName,
                                    data=data,
                                    headers=requestHeaders,
                                    timeout=timeout)
    except requests.RequestException:
        raise ServiceUnavailable(service, functionName) from None
    if not response.ok:
        raise ServiceError(service, functionName, response.status_code,
                           _envelopeMessage(response), response=response)
    try:
        return response.json()
    except ValueError:
        raise ServiceError(service, functionName, response.status_code,
                           None, response=response) from None

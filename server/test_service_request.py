"""services/__request.py: how a call to a sibling service fails.

No network: the module's session is swapped for a fake that answers (or
raises) what each test needs. No database either.
"""
import importlib
import json
import time

import pytest
import requests

serviceRequest = importlib.import_module('services.__request')
from services.__abstractService import Path

SIBLING_URL = 'https://sibling-secret-host.example.run.app/'
PATH = Path(prod=SIBLING_URL, dev=SIBLING_URL)


class _FakeSession:

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _response(status, body: bytes, contentType='application/json'):
    response = requests.models.Response()
    response.status_code = status
    response._content = body
    response.headers['Content-Type'] = contentType
    response.url = SIBLING_URL + 'someRoute'
    return response


def _call(monkeypatch, answer, **kwargs):
    session = _FakeSession(answer)
    monkeypatch.setattr(serviceRequest, '_session', session)
    result = serviceRequest.request(PATH, 'someRoute', 'POST', {'a': 1},
                                    service='sibling', **kwargs)
    return result, session


def _raised(monkeypatch, answer):
    with pytest.raises(requests.RequestException) as info:
        _call(monkeypatch, answer)
    return info.value


def test_success_returnsTheEnvelope(monkeypatch):
    body = {'message': 'ok', 'status': 200, 'data': {'x': 1}}
    result, session = _call(monkeypatch,
                            _response(200, json.dumps(body).encode()))

    assert result == body
    (call, ) = session.calls
    assert call['method'] == 'POST'
    assert call['url'] == SIBLING_URL + 'someRoute'
    assert json.loads(call['data']) == {'a': 1}
    assert call['headers'] == {'Content-Type': 'application/json'}
    assert call['timeout'] == serviceRequest.DEFAULT_TIMEOUT


def test_theDefaultTimeout_neverHangsAndNeverCutsASlowCall():
    assert serviceRequest.DEFAULT_TIMEOUT == (5, 60)


def test_aCallerTimeoutAndHeadersAreSent(monkeypatch):
    _, session = _call(monkeypatch,
                       _response(200, b'{}'),
                       timeout=(5, 18),
                       headers={'X-Extra': '1'})

    (call, ) = session.calls
    assert call['timeout'] == (5, 18)
    assert call['headers'] == {
        'Content-Type': 'application/json',
        'X-Extra': '1'
    }


def test_authHeadersHookIsSentOnEveryCall(monkeypatch):
    monkeypatch.setattr(serviceRequest, '_authHeaders',
                        lambda baseUrl: {'Authorization': f'Bearer {baseUrl}'})
    _, session = _call(monkeypatch, _response(200, b'{}'))

    assert session.calls[0]['headers']['Authorization'] == (
        f'Bearer {SIBLING_URL}')


def test_aCallersHeaders_neverReplaceTheHooks(monkeypatch):
    """A caller can add a header (SalesApp sends X-Internal-Api-Key) but
    never swap the service token the hook sets."""
    monkeypatch.setattr(serviceRequest, '_authHeaders',
                        lambda baseUrl: {'Authorization': 'Bearer hook'})
    _, session = _call(monkeypatch,
                       _response(200, b'{}'),
                       headers={
                           'Authorization': 'Bearer caller',
                           'X-Internal-Api-Key': 'k'
                       })

    assert session.calls[0]['headers'] == {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer hook',
        'X-Internal-Api-Key': 'k'
    }


def test_a400_isAServiceErrorCarryingTheSiblingsMessage(monkeypatch):
    error = _raised(
        monkeypatch,
        _response(400, b'{"message": "Customer not found", "status": 400}'))

    assert isinstance(error, serviceRequest.ServiceError)
    assert isinstance(error, requests.HTTPError)
    assert (error.service, error.route, error.status,
            error.message) == ('sibling', 'someRoute', 400,
                               'Customer not found')
    assert error.response.json()['message'] == 'Customer not found'
    assert str(error) == 'sibling someRoute answered 400'
    assert 'Customer not found' not in str(error)
    assert 'sibling-secret-host' not in str(error)


@pytest.mark.parametrize('body', [
    b'{"message": 5, "status": 400}',
    b'{"message": "", "status": 400}',
    b'["Customer not found"]',
    b'"Customer not found"',
])
def test_a400WithoutAStringMessage_isAServiceErrorWithNoMessage(
        monkeypatch, body):
    error = _raised(monkeypatch, _response(400, body))

    assert isinstance(error, serviceRequest.ServiceError)
    assert (error.status, error.message) == (400, None)
    assert str(error) == 'sibling someRoute answered 400'


def test_a500WithAnHtmlBody_isAServiceErrorWithNoMessage(monkeypatch):
    error = _raised(monkeypatch,
                    _response(500, b'<html>oops</html>', 'text/html'))

    assert isinstance(error, serviceRequest.ServiceError)
    assert (error.status, error.message) == (500, None)
    assert str(error) == 'sibling someRoute answered 500'


def test_a200ThatIsNotJson_isAServiceError(monkeypatch):
    error = _raised(monkeypatch, _response(200, b'<html>', 'text/html'))

    assert isinstance(error, serviceRequest.ServiceError)
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize('transportError', [
    requests.ConnectTimeout(f'HTTPSConnectionPool(host={SIBLING_URL})'),
    requests.ReadTimeout(f'Read timed out {SIBLING_URL}'),
    requests.ConnectionError(f'Max retries exceeded {SIBLING_URL}'),
])
def test_noAnswer_isServiceUnavailableWithoutTheUrl(monkeypatch,
                                                     transportError):
    error = _raised(monkeypatch, transportError)

    assert type(error) is serviceRequest.ServiceUnavailable
    assert str(error) == 'sibling someRoute did not answer'
    assert error.__cause__ is None and error.__suppress_context__


def test_getSendsNoBody(monkeypatch):
    session = _FakeSession(_response(200, b'{}'))
    monkeypatch.setattr(serviceRequest, '_session', session)

    serviceRequest.request(PATH, 'someRoute', 'get', {'a': 1})

    assert session.calls[0]['method'] == 'GET'
    assert session.calls[0]['data'] is None


# ----------------------------- the caller's ID token ----------------------------- #


@pytest.fixture
def onCloudRun(monkeypatch):
    """A deployed service: _authHeaders mints (counted) ID tokens."""
    fetched = []

    def fetchIdToken(audience):
        fetched.append(audience)
        return f'token-{len(fetched)}', time.time() + 3600

    monkeypatch.setattr(serviceRequest, '_isCloudEnvironment', lambda: True)
    monkeypatch.setattr(serviceRequest, '_fetchIdToken', fetchIdToken)
    monkeypatch.setattr(serviceRequest, '_idTokens', {})
    return fetched


def test_onCloudRun_aCallCarriesAnIdTokenForTheSiblingsUrl(
        monkeypatch, onCloudRun):
    _, session = _call(monkeypatch, _response(200, b'{}'))

    assert session.calls[0]['headers']['Authorization'] == 'Bearer token-1'
    assert onCloudRun == [SIBLING_URL.rstrip('/')]


def test_theIdTokenIsReusedUntilItNearlyExpires(monkeypatch, onCloudRun):
    _call(monkeypatch, _response(200, b'{}'))
    _, session = _call(monkeypatch, _response(200, b'{}'))
    assert session.calls[0]['headers']['Authorization'] == 'Bearer token-1'
    assert len(onCloudRun) == 1

    audience = SIBLING_URL.rstrip('/')
    serviceRequest._idTokens[audience] = (
        'token-1',
        time.time() + serviceRequest.ID_TOKEN_REFRESH_MARGIN_SECONDS - 1)
    _, session = _call(monkeypatch, _response(200, b'{}'))
    assert session.calls[0]['headers']['Authorization'] == 'Bearer token-2'
    assert len(onCloudRun) == 2


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:5000/',
    'http://localhost:6100/',
    'http://[::1]:7000/',
])
def test_aLoopbackSibling_getsNoToken(monkeypatch, onCloudRun, url):
    session = _FakeSession(_response(200, b'{}'))
    monkeypatch.setattr(serviceRequest, '_session', session)
    serviceRequest.request(Path(prod=url, dev=url), 'someRoute', 'POST', {},
                           service='sibling')

    assert 'Authorization' not in session.calls[0]['headers']
    assert onCloudRun == []


def test_offCloudRun_noTokenIsFetched(monkeypatch):
    monkeypatch.setattr(serviceRequest, '_isCloudEnvironment', lambda: False)

    def fetchIdToken(audience):
        raise AssertionError('no token is fetched off Cloud Run')

    monkeypatch.setattr(serviceRequest, '_fetchIdToken', fetchIdToken)
    _, session = _call(monkeypatch, _response(200, b'{}'))

    assert 'Authorization' not in session.calls[0]['headers']


def test_onCloudRun_aTokenThatCannotBeMinted_failsTheCall(monkeypatch):
    monkeypatch.setattr(serviceRequest, '_isCloudEnvironment', lambda: True)
    monkeypatch.setattr(serviceRequest, '_idTokens', {})

    def fetchIdToken(audience):
        raise RuntimeError('metadata server unreachable')

    monkeypatch.setattr(serviceRequest, '_fetchIdToken', fetchIdToken)
    session = _FakeSession(_response(200, b'{}'))
    monkeypatch.setattr(serviceRequest, '_session', session)

    with pytest.raises(RuntimeError):
        serviceRequest.request(PATH, 'someRoute', 'POST', {},
                               service='sibling')
    assert session.calls == []

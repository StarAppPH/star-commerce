"""routeErrors.py: what a generated route or push handler answers and logs
when it raised, and that the generated app.py / appPubSub.py really do it.

No database: the routes are patched to raise before they would read one,
and the push handler's dedup store is a stand-in.
"""
import importlib
import json
import logging
import re

import pytest
import requests
from flask_jwt_extended import create_access_token
from pydantic import BaseModel, ValidationError

import pubSub as pubSubModule
import routeErrors
from ApiRequests import ApiRequests
from AppCreator import (STATUS_BAD_REQUEST, STATUS_FORBIDDEN,
                        STATUS_SERVER_ERROR, AppCreator)
from mongoDb import OutdatedDataError
from PubSubRequests import PubSubRequests
from services.__request import ServiceError, ServiceUnavailable

SECRET = 'rte-secret@example.com'
OUTER_SECRET = 'rte-outer-secret'
_TEST_JWT_SECRET = 'route-errors-test-jwt-secret-0123456789abcdef'
# The JWT identity _post signs in as, when the route it calls needs one.
_TEST_USER = 'rte-user'
_ERROR_ID = r' \(error id [0-9a-f]{12}\)'
SERVER_FAULT_MESSAGE = (r'Something went wrong on our side\. Please try '
                        r'again\.' + _ERROR_ID)


def _unavailableMessage(service: str) -> str:
    return (service + r' is unavailable right now\. Please try again\.' +
            _ERROR_ID)


class _Shape(BaseModel):
    # `message`: a field every objects module declares (ApiResponse).
    message: int


class _Keyed(BaseModel):
    message: dict[str, int]


def _raised(error: Exception) -> Exception:
    """`error` with a traceback, as a handler's except clause sees it."""
    try:
        raise error
    except Exception as caught:
        return caught


def _validationError() -> ValidationError:
    try:
        _Shape(message=SECRET)
    except ValidationError as e:
        return e


def _serviceError(status, message):
    return ServiceError('accounting', 'someRoute', status, message)


def _chained() -> Exception:
    try:
        try:
            raise KeyError(SECRET)
        except KeyError as inner:
            raise RuntimeError(OUTER_SECRET) from inner
    except RuntimeError as outer:
        return outer


# ----------------------------- the mapping ----------------------------- #


@pytest.mark.parametrize('error, status, expected, message', [
    (PermissionError('Not allowed'), STATUS_FORBIDDEN, STATUS_FORBIDDEN,
     'Not allowed'),
    (ValueError('Order not found'), STATUS_BAD_REQUEST, STATUS_BAD_REQUEST,
     'Order not found'),
    (OutdatedDataError('Your data is outdated.'), STATUS_BAD_REQUEST,
     STATUS_BAD_REQUEST, 'Your data is outdated.'),
    (_serviceError(400, 'Customer not found'), STATUS_BAD_REQUEST,
     STATUS_BAD_REQUEST, 'Customer not found'),
])
def test_aCallersError_isAnsweredWithItsMessage(error, status, expected,
                                                 message):
    failure = routeErrors.routeFailure(_raised(error), status=status)

    assert (failure.status, failure.message,
            failure.level) == (expected, message, logging.WARNING)


def test_aValidationError_namesTheFieldNeverTheValue():
    failure = routeErrors.routeFailure(_raised(_validationError()),
                                       status=STATUS_BAD_REQUEST)

    assert failure.status == STATUS_BAD_REQUEST
    assert failure.message.startswith('Invalid message: ')
    assert SECRET not in failure.message
    assert SECRET not in failure.traceback


def test_aValidationError_neverNamesADictKey():
    """A dict key in the error's location is the caller's data."""
    try:
        _Keyed(message={SECRET: 'not a number'})
    except ValidationError as e:
        message = routeErrors.validationMessage(e)

    assert message.startswith('Invalid message.*: ')
    assert SECRET not in message


def test_aValidationError_listsAtMostThreeFields():

    class Many(BaseModel):
        a: int
        b: int
        c: int
        d: int
        e: int

    try:
        Many()
    except ValidationError as e:
        message = routeErrors.validationMessage(e)

    assert message.startswith('Invalid ')
    assert message.count(': Field required') == 3
    assert message.endswith('; and 2 more')


@pytest.mark.parametrize('error', [
    KeyError(SECRET),
    TypeError(SECRET),
    RuntimeError(SECRET),
    Exception(SECRET),
    requests.ConnectionError(SECRET),
])
def test_aServerFault_isA500WithAnErrorIdAndNoText(error):
    failure = routeErrors.routeFailure(_raised(error),
                                       status=STATUS_BAD_REQUEST)

    assert failure.status == STATUS_SERVER_ERROR
    assert failure.level == logging.ERROR
    assert failure.message.startswith('Something went wrong on our side.')
    assert re.fullmatch(SERVER_FAULT_MESSAGE, failure.message)
    errorId = failure.message.rsplit('(error id ', 1)[1].rstrip(')')
    assert len(errorId) == 12
    assert f'(errorId {errorId})' in failure.traceback
    assert SECRET not in failure.message
    assert SECRET not in failure.traceback
    assert type(error).__qualname__ in failure.traceback


@pytest.mark.parametrize('error', [
    _serviceError(500, None),
    _serviceError(400, None),
    _serviceError(403, 'Forbidden'),
    ServiceUnavailable('accounting', 'someRoute'),
])
def test_aSiblingThatFailed_isA503NamingTheService(error):
    failure = routeErrors.routeFailure(_raised(error),
                                       status=STATUS_BAD_REQUEST)

    assert failure.status == routeErrors.STATUS_SERVICE_UNAVAILABLE
    assert failure.level == logging.ERROR
    assert failure.message.startswith(
        'Accounting is unavailable right now. Please try again. (error id ')
    assert re.fullmatch(_unavailableMessage('Accounting'), failure.message)


def test_aChainedCause_neverReachesTheTraceback():
    failure = routeErrors.routeFailure(_chained(), status=STATUS_BAD_REQUEST)

    assert SECRET not in failure.traceback
    assert OUTER_SECRET not in failure.traceback
    assert 'RuntimeError' in failure.traceback


def test_aPushWhoseEnvelopeNeverParsed_isA400():
    failure = routeErrors.routeFailure(_raised(KeyError('message')),
                                       status=STATUS_BAD_REQUEST,
                                       push=True,
                                       envelopeParsed=False)

    assert (failure.status,
            failure.message) == (STATUS_BAD_REQUEST, 'Malformed Pub/Sub push')
    assert failure.level == logging.WARNING


def test_aPushPermissionError_staysA403BeforeTheEnvelopeParsed():
    failure = routeErrors.routeFailure(_raised(PermissionError('No token')),
                                       status=STATUS_FORBIDDEN,
                                       push=True,
                                       envelopeParsed=False)

    assert (failure.status, failure.message) == (STATUS_FORBIDDEN, 'No token')


def test_aPushHandlerFault_afterTheEnvelopeParsed_isA500():
    failure = routeErrors.routeFailure(_raised(KeyError(SECRET)),
                                       status=STATUS_BAD_REQUEST,
                                       push=True,
                                       envelopeParsed=True)

    assert failure.status == STATUS_SERVER_ERROR


def test_theResponse_isTheEnvelopeWithTheStatus():
    from flask import Flask

    with Flask('routeErrorsTest').app_context():
        body, status = routeErrors.routeFailure(
            _raised(KeyError(SECRET)), status=STATUS_BAD_REQUEST).response()
        pushBody, _ = routeErrors.routeFailure(_raised(ValueError('bad')),
                                               status=STATUS_BAD_REQUEST,
                                               push=True).response()
        withData, _ = routeErrors.routeFailure(_raised(ValueError('bad')),
                                               status=STATUS_BAD_REQUEST,
                                               data={
                                                   'detail': 1
                                               }).response()

    assert status == STATUS_SERVER_ERROR
    assert set(body.get_json()) == {'message', 'status', 'data'}
    assert body.get_json()['status'] == STATUS_SERVER_ERROR
    assert body.get_json()['data'] is None
    assert pushBody.get_json()['traceback'].startswith(
        'Traceback (most recent call last):')
    assert 'bad' not in pushBody.get_json()['traceback']
    assert withData.get_json()['data'] == {'detail': 1}


# ------------------------- the generated handlers ------------------------- #


@pytest.mark.parametrize('kind', ['app', 'appPubSub'])
def test_generatedHandlerCode_logsEachFailureOnceWithoutItsText(kind):
    """Exactly the success log and one log per except branch, each of the
    latter the handler's entry plus failure.traceback: no second log, no
    exc_info (whose traceback carries the message), no raw error text."""
    creator = AppCreator(apiRequests=None)
    if kind == 'app':
        _, func = _publicRoute()
        params = creator._parameters(func)
    else:
        func = getattr(PubSubRequests, _pushRoute())
        params = creator._parameters(func)
    code = creator._function_code('fn', params, creator._route_meta(func),
                                  kind, 'POST', None)

    logged = [line.strip() for line in code.splitlines() if 'logging' in line]
    assert len(logged) == 3, logged
    assert logged[0].startswith('logging.log(failure.level, {')
    assert logged[1].startswith('logging.log(failure.level, {')
    assert logged[2].startswith('logging.info({')
    assert all("'traceback': failure.traceback})" in line
               for line in logged[:2])
    for banned in ('str(e)', 'print_exc', 'format_exc', 'exc_info',
                   'logging.exception', 'logging.error', 'traceback.'):
        assert banned not in code.replace('failure.traceback', ''), banned
    assert code.count('routeFailure(e, ') == 2


def test_routeErrors_neverLogsItself():
    """routeErrors answers; the handler logs. A log call here would be the
    second log line of every failure."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(routeErrors))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (f'{func.value.id}.{func.attr}' if isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) else getattr(
                    func, 'id', None))
        calls.append(name)
        assert not any(k.arg == 'exc_info' for k in node.keywords), name
    banned = {'print', 'traceback.print_exc', 'traceback.format_exc',
              'traceback.format_exception', 'logging.log', 'logging.error',
              'logging.warning', 'logging.info', 'logging.exception',
              'logging.critical', 'logging.debug'}
    assert not banned & set(calls), banned & set(calls)
    assert 'traceback.format_tb' in calls


def _isPublic(meta: dict) -> bool:
    return not meta['jwtRequired'] and not meta.get('jwtOptional')


def _publicRoute():
    """A POST route of the real app, as (name, func): a public one (no
    token needed) when the app has one, else a JWT route, which _post calls
    with a token (a route only another service calls last). Never a sign-in
    route (createAccessToken), whose handler logs no request data."""
    creator = AppCreator(apiRequests=None)
    jwtRoutes = []
    for name, func in creator._get_user_defined_methods('app').items():
        meta = creator._route_meta(func)
        if meta['httpMethod'] != 'POST' or meta['createAccessToken']:
            continue
        if _isPublic(meta):
            return name, func
        jwtRoutes.append((bool(meta['serviceOnly']), name, func))
    if not jwtRoutes:
        pytest.skip('this app has no POST route')
    _, name, func = min(jwtRoutes, key=lambda route: route[0])
    return name, func


def _post(flaskApp, name: str, func):
    """POST to the route `func` (as _publicRoute found it, before any
    monkeypatch): an empty body to a public route; to a JWT route a token
    minted under _TEST_JWT_SECRET for _TEST_USER, with that userId in the
    body when the route takes one (the handler refuses any other)."""
    creator = AppCreator(apiRequests=None)
    if _isPublic(creator._route_meta(func)):
        return flaskApp.test_client().post(f'/{name}', json={})
    with flaskApp.app_context():
        token = create_access_token(identity=_TEST_USER)
    body = ({'userId': _TEST_USER}
            if 'userId' in creator._parameters(func).names else {})
    return flaskApp.test_client().post(
        f'/{name}', json=body, headers={'Authorization': f'Bearer {token}'})


def _pushRoute() -> str:
    names = list(AppCreator(apiRequests=None)._get_user_defined_methods(
        'appPubSub'))
    if not names:
        pytest.skip('this app has no push handler')
    return names[0]


def _flaskApp(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setenv('JWT_SECRET_KEY', _TEST_JWT_SECRET)

    def _noGcp(*args, **kwargs):
        raise AssertionError('a test tried to create the Pub/Sub clients')

    monkeypatch.setattr(pubSubModule.pubSub, '_ensureClients', _noGcp)
    return importlib.import_module('app').app


def _routeLogs(caplog, route) -> list:
    return [
        r for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get('function') == route
    ]


@pytest.mark.parametrize('error, status', [
    (KeyError(SECRET), STATUS_SERVER_ERROR),
    (ServiceUnavailable('warehouse', 'x'),
     routeErrors.STATUS_SERVICE_UNAVAILABLE),
    (ValueError('Fix this'), STATUS_BAD_REQUEST),
    (PermissionError('Not yours'), STATUS_FORBIDDEN),
])
def test_http_theRealAppAnswersTheStatusOnTheWire(monkeypatch, caplog, error,
                                                  status):
    """Cloud Run counts what goes on the wire: a server fault is a real 5xx
    there, not a 400 envelope, and it is logged once without its text."""
    caplog.set_level(logging.INFO)
    flaskApp = _flaskApp(monkeypatch)
    name, func = _publicRoute()

    def _raise(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(ApiRequests, name, _raise)

    res = _post(flaskApp, name, func)

    body = res.get_json()
    assert res.status_code == body['status'] == status
    assert set(body) == {'message', 'status', 'data'}
    assert re.fullmatch(
        {
            STATUS_SERVER_ERROR: SERVER_FAULT_MESSAGE,
            routeErrors.STATUS_SERVICE_UNAVAILABLE:
            _unavailableMessage('Warehouse'),
            STATUS_BAD_REQUEST: re.escape('Fix this'),
            STATUS_FORBIDDEN: re.escape('Not yours'),
        }[status], body['message'])
    assert SECRET not in res.get_data(as_text=True)
    (log, ) = _routeLogs(caplog, name)
    assert log.msg['event'] == 'error'
    assert log.levelno == (logging.ERROR
                           if status >= STATUS_SERVER_ERROR else logging.WARNING)
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


class _NoDedupDb:
    """The push handler's dedup store (read/claim, record, release)."""

    def read(self, *args, **kwargs):
        return None

    def create(self, *args, **kwargs):
        return None

    def delete(self, *args, **kwargs):
        return 0


def test_http_aPushHandlerFault_isA500(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    flaskApp = _flaskApp(monkeypatch)
    name = _pushRoute()
    monkeypatch.setattr(importlib.import_module('appPubSub'), 'db',
                        _NoDedupDb())

    def _raise(self, *args, **kwargs):
        raise KeyError(SECRET)

    monkeypatch.setattr(PubSubRequests, name, _raise)

    res = flaskApp.test_client().post(f'/{name}',
                                      json={
                                          'message': {
                                              'data': {
                                                  'field': 1
                                              },
                                              'messageId': 'rte-1',
                                              'attributes': {}
                                          }
                                      })

    body = res.get_json()
    assert res.status_code == body['status'] == STATUS_SERVER_ERROR
    assert SECRET not in json.dumps(body)
    assert body['traceback'].startswith('Traceback (most recent call last):')
    (log, ) = _routeLogs(caplog, name)
    assert log.msg['messageId'] == 'rte-1'
    assert log.msg['messageKeys'] == ['field']
    assert log.levelno == logging.ERROR
    assert SECRET not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


# ------------------------- the param shape guard ------------------------- #


class _GuardBody(BaseModel):
    message: str


def _guardedRoute(self, items: list[str], body: _GuardBody | None = None,
                  lookup: dict[str, int] | None = None, name: str = 'x'):
    return {'items': items}


@pytest.mark.parametrize('annotation, shape', [
    (dict, ('dict', False)),
    (dict[str, int] | None, ('dict', True)),
    (_GuardBody, ('dict', False)),
    (list[str], ('list', False)),
    (list[_GuardBody] | None, ('list', True)),
    (str, None),
    (int | None, None),
    (list[str] | dict, None),
])
def test_shapeOfAnnotation(annotation, shape):
    from AppCreator import _shape_of_annotation
    assert _shape_of_annotation(annotation) == shape


def test_shapeParams_allowNoneOnlyWhenOptionalOrDefaulted():
    assert AppCreator(apiRequests=None)._shape_params(_guardedRoute) == [
        ('items', 'list', False), ('body', 'dict', True),
        ('lookup', 'dict', True)
    ]


@pytest.fixture
def guardedClient():
    from conftest import routeMeta
    from flask import Flask

    class _Api:
        calls = []

        def fn(self, items, body, lookup, name):
            _Api.calls.append(items)
            return {'items': items}

    creator = AppCreator(apiRequests=None)
    code = creator._function_code(
        'fn', creator._parameters(_guardedRoute), routeMeta(), 'app', 'POST',
        None, creator._scalar_params(_guardedRoute),
        creator._shape_params(_guardedRoute))
    app = Flask('shapeGuardTest')
    # The generated app.py's globals, so the handler finds every name its
    # imports give it (a repo's own except types among them).
    namespace = dict(vars(importlib.import_module('app')))
    namespace.update({'app': app, 'ApiRequests': _Api})
    exec(code, namespace)
    with app.app_context():
        yield app.test_client(), _Api.calls


@pytest.mark.parametrize('body, param', [
    ({}, 'items'),
    ({'items': 'not-a-list'}, 'items'),
    ({'items': {'a': 1}}, 'items'),
    ({'items': ['a'], 'body': ['not', 'an', 'object']}, 'body'),
    ({'items': ['a'], 'body': 'text'}, 'body'),
    ({'items': ['a'], 'lookup': [1]}, 'lookup'),
])
def test_aParamOfTheWrongShape_isA400(guardedClient, body, param):
    client, calls = guardedClient
    res = client.post('/fn', json=body)

    assert res.status_code == STATUS_BAD_REQUEST
    assert res.get_json()['message'] == f'Invalid {param}'
    assert calls == []


def test_aParamOfTheRightShape_reachesTheRoute(guardedClient):
    client, calls = guardedClient
    res = client.post('/fn',
                      json={
                          'items': ['a'],
                          'body': {
                              'message': 'm'
                          },
                          'lookup': None
                      })

    assert res.status_code == 200
    assert calls == [['a']]


# ---------------------- errors outside a route's try ---------------------- #


class _Unserializable:
    """jsonify cannot encode it, so the route's success response raises after
    its try (TypeError), and the app-wide handler answers."""

    def __repr__(self):
        return SECRET


def test_http_anErrorOutsideTheTry_isAnsweredAndLoggedOnce(
        monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    flaskApp = _flaskApp(monkeypatch)
    name, func = _publicRoute()
    monkeypatch.setattr(ApiRequests, name,
                        lambda self, *a, **k: {'value': _Unserializable()})

    res = _post(flaskApp, name, func)

    body = res.get_json()
    assert res.status_code == body['status'] == STATUS_SERVER_ERROR
    assert set(body) == {'message', 'status', 'data'}
    assert re.fullmatch(SERVER_FAULT_MESSAGE, body['message'])
    errors = [
        r for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get('event') == 'error'
    ]
    assert len(errors) == 1
    assert errors[0].msg['function'] == name
    assert errors[0].levelno == logging.ERROR
    assert SECRET not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


class _RefusesToRender:
    """Flask's JSON encoder calls __html__, so the route's success response
    raises a PermissionError after its try."""

    def __html__(self):
        raise PermissionError('Not yours')


def test_http_aPermissionErrorOutsideTheTry_isA403(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    flaskApp = _flaskApp(monkeypatch)
    name, func = _publicRoute()
    monkeypatch.setattr(ApiRequests, name,
                        lambda self, *a, **k: {'value': _RefusesToRender()})

    res = _post(flaskApp, name, func)

    body = res.get_json()
    assert res.status_code == body['status'] == STATUS_FORBIDDEN
    assert body['message'] == 'Not yours'
    errors = [
        r for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get('event') == 'error'
    ]
    assert len(errors) == 1
    assert errors[0].levelno == logging.WARNING


def test_http_aMissingRoute_staysA404(monkeypatch):
    flaskApp = _flaskApp(monkeypatch)

    res = flaskApp.test_client().post('/rte-no-such-route', json={})

    assert res.status_code == 404

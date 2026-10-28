"""HTTP envelopes of the generated Flask app (app.py).

Every other route test calls ApiRequests() directly, as DO_NOT_DO.md
requires. These are the exception it allows: they test the generated
TRANSPORT layer, which a direct call never reaches -
  - the ApiResponse envelope each kind of route answers with
    (createAccessToken, jwtRequired, public, GET): the ApiSuccess /
    ApiFailure the TS client is typed against, with status_code equal to
    body.status and no key outside ApiResponse;
  - the status mapping: a ValueError the route raises, a failed role check
    and a body userId that is not the JWT identity are 400 envelopes with
    data null; a PermissionError (sign-in) is a 403;
  - a POST whose body is not a JSON object is a 400 envelope, never a 500,
    and an object or a list for a scalar param is refused (a {"$ne": null}
    never reaches a query); a malformed Pub/Sub push is a 400 too;
  - the JWT gate: a missing, expired or malformed token is turned away with
    a {msg}-only body before the route runs; a JWT route that succeeds hands
    back a refreshed jwtToken, and a failed one hands back none;
  - logging: a route logs which of its params it was sent, never the
    values; a push handler logs the messageId and the message's keys;
  - app.py's config: outside localdev a short or placeholder
    JWT_SECRET_KEY is refused; a local run binds 127.0.0.1 unless
    LOCAL_BIND_HOST says otherwise, and then warns;
  - sign-in: the loginWithGoogle envelopes, and the sign-in payload (the ID
    token, a token fragment, a raw identity) never reaching a log record or
    stderr;
  - a missing optional body key reaches the route as None (the route maps
    it to its default), and a coordinate that is no number is a 400.
Response SHAPES stay on the direct-call tests (test_route_responses.py).

The REAL app.py is imported, not regenerated source, so what is tested is
what ships, and a stale app.py fails here. It is imported lazily inside each
test: pytest's log handlers are attached by then, so app.py's
logging.basicConfig is a no-op. Only _flask_app imports app.py (and
test_response_compression.py imports it from here), so the
JWT_SECRET_KEY set here is the one its config reads; tokens are minted
inside its app_context either way, so they always match its config.

Creating the Pub/Sub clients fails the test. Sign-in verification is mocked
at AuthHandler._decodeFirebaseIdToken (conftest's `firebase` fixture), so no
Google call is made.
"""

import datetime
import importlib
import json
import logging
import os
import subprocess
import sys
from http import HTTPStatus

import pytest
import pubSub as pubSubModule
from ApiRequests import ApiRequests
from AppCreator import (STATUS_BAD_REQUEST, STATUS_FORBIDDEN,
                        STATUS_JWT_REJECTED, STATUS_OK)
from actions.OrderActions import (INVALID_PERIOD, INVALID_STATUS,
                                  PERIOD_AND_DATES)
from actions.ProductActions import PRODUCT_LIST_DEFAULT_LIMIT
from conftest import (RAW_EMAIL, RAW_UID, STORE_DETAILS, TOKEN, TOKEN_UID,
                      addOrder, addProduct, createTestStore, createTestUser, guestAt,
                      modelKeys, wipeCollections)
from flask_jwt_extended import create_access_token, decode_token
from mongoDb import db
from objects import ApiResponse, MyOrderRow, StoreOrderRow, UserResponse
from roles import AllRoles
from utils import _ISO_DATE_MESSAGE

_TEST_JWT_SECRET = 'rrh-http-test-jwt-secret-0123456789abcdef'
FORGED_TOKEN = 'rrh-http-forged-firebase-token'
TOKEN_FRAGMENT = 'rrh-token-fragment'
SECRET_TITLE = 'rrh-a-value-no-log-may-quote'
ADMIN = 'rrh-admin'
NOBODY = 'rrh-no-role'

# The keys only some kinds of route add to their envelope.
TOKEN_KEYS = {'access_token', 'current_user', 'jwtToken'}
FAILURE_KEYS = {'message', 'status', 'data'}


def _no_gcp(*args, **kwargs):
    raise AssertionError('a test tried to create the Pub/Sub clients')


def _flask_app(monkeypatch):
    """The generated app, with Pub/Sub client creation turned into a test
    failure. JWT_SECRET_KEY only matters if this is the first import of
    app.py in the run."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setenv('JWT_SECRET_KEY', _TEST_JWT_SECRET)
    monkeypatch.setattr(pubSubModule.pubSub, '_ensureClients', _no_gcp)
    assert not pubSubModule.pubSub.isProductionEnvironment
    return importlib.import_module('app').app


def _token(flaskApp, identity, expiresDelta=None) -> str:
    with flaskApp.app_context():
        return create_access_token(identity=identity,
                                   expires_delta=expiresDelta)


def _identity(flaskApp, token) -> str:
    with flaskApp.app_context():
        return decode_token(token)['sub']


def _bearer(token) -> dict:
    return {'Authorization': f'Bearer {token}'}


def _as_sent(flaskApp, value):
    """`value` as the app's JSON provider puts it on the wire (datetimes
    as RFC 1123 strings)."""
    return json.loads(flaskApp.json.dumps(value))


def _success_body(res, route) -> dict:
    """The ApiSuccess envelope: status 200 in both places, only ApiResponse
    keys, no traceback, and the route's own success message."""
    body = res.get_json()
    assert res.status_code == body['status'] == STATUS_OK, body
    assert set(body) <= modelKeys(ApiResponse)
    assert 'traceback' not in body
    assert body['message'] == getattr(ApiRequests, route).successMessage
    return body


def _failure_body(res, status) -> dict:
    """A browser route's failure envelope: exactly message, status and
    data (so no token keys: a failed call never slides the session), with
    status_code equal to body.status."""
    body = res.get_json()
    assert res.status_code == body['status'] == status, body
    assert set(body) == FAILURE_KEYS
    return body


def _routeLogs(caplog, route) -> list:
    return [
        r.msg for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get('function') == route
    ]


def test_http_generatedRoutes_successEnvelopes(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(['Users', 'SampleTodos'])
    try:
        createTestUser(ADMIN)
        headers = _bearer(_token(flaskApp, ADMIN))

        # jwtRequired: the caller rides along, with a refreshed token.
        res = client.post('/getRoles', json={'userId': ADMIN}, headers=headers)

        body = _success_body(res, 'getRoles')
        assert TOKEN_KEYS & set(body) == {'current_user', 'jwtToken'}
        assert body['current_user'] == ADMIN
        assert _identity(flaskApp, body['jwtToken']) == ADMIN
        assert body['data'] == _as_sent(flaskApp,
                                        ApiRequests().getRoles(ADMIN))

        # The client signs its next call with the refreshed token.
        res = client.post('/getRoles',
                          json={'userId': ADMIN},
                          headers=_bearer(body['jwtToken']))
        _success_body(res, 'getRoles')

        # GET, no token in or out.
        res = client.get('/handshake')

        body = _success_body(res, 'handshake')
        assert not TOKEN_KEYS & set(body)
        assert body['data'] == ApiRequests().handshake()

        # Public POST; the log carries the body's keys, never its values.
        res = client.post('/createTodo', json={'title': SECRET_TITLE})

        body = _success_body(res, 'createTodo')
        assert not TOKEN_KEYS & set(body)
        # The page reads nothing from a mutation's answer: it sends nothing.
        assert body['data'] == {}
        assert db.read({'title': SECRET_TITLE}, 'SampleTodos', findOne=True)
        assert _routeLogs(caplog, 'createTodo') == [{
            'function': 'createTodo',
            'event': 'success',
            'requestKeys': ['title']
        }]
        # A key the route does not take is not logged either: the caller
        # chose its name.
        caplog.clear()
        res = client.post('/createTodo',
                          json={
                              'title': 'Rrh second',
                              SECRET_TITLE: 1
                          })
        _success_body(res, 'createTodo')
        assert _routeLogs(caplog, 'createTodo')[0]['requestKeys'] == ['title']
        assert all(SECRET_TITLE not in r.getMessage() for r in caplog.records)
        db.delete({'title': 'Rrh second'}, 'SampleTodos')
        assert all(SECRET_TITLE not in r.getMessage() for r in caplog.records)

        res = client.post('/getTodos')

        body = _success_body(res, 'getTodos')
        assert [todo['title'] for todo in body['data']['todos']
                ] == [SECRET_TITLE]
    finally:
        wipeCollections(['Users', 'SampleTodos'])


def test_http_generatedRoutes_failureEnvelopes(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(['Users', 'SampleTodos'])
    try:
        createTestUser(ADMIN)
        createTestUser(NOBODY, userType='unauthorized')

        # The JWT gate turns each of these away with a {msg}-only body,
        # which the generated client maps to its 401 ApiFailure.
        rejectedTokens = [
            None,
            _token(flaskApp, ADMIN, datetime.timedelta(seconds=-10)),
            'not-a-jwt',
        ]
        for token in rejectedTokens:
            res = client.post('/getRoles',
                              json={'userId': ADMIN},
                              headers=_bearer(token) if token else None)

            body = res.get_json()
            assert res.status_code in (STATUS_JWT_REJECTED,
                                       HTTPStatus.UNPROCESSABLE_ENTITY)
            assert set(body) == {'msg'}
            assert body['msg']

        # A token for someone other than the userId sent: the generated
        # identity check answers 400 before the route runs.
        res = client.post('/getRoles',
                          json={'userId': ADMIN},
                          headers=_bearer(_token(flaskApp, NOBODY)))

        assert _failure_body(res, STATUS_BAD_REQUEST) == {
            'message': 'Unauthorized access',
            'status': STATUS_BAD_REQUEST,
            'data': None
        }

        # The caller's own token, without the route's role: its message.
        res = client.post('/getRoles',
                          json={'userId': NOBODY},
                          headers=_bearer(_token(flaskApp, NOBODY)))

        assert _failure_body(res, STATUS_BAD_REQUEST) == {
            'message':
            AllRoles().getSpecificRole('canUpdateUserRole').unauthorizedMessage,
            'status': STATUS_BAD_REQUEST,
            'data': None
        }

        # A ValueError the route raises: a 400 with data null.
        res = client.post('/markTodoAsCompleted',
                          json={'todoId': 'rrh-no-such-todo'})

        assert _failure_body(res, STATUS_BAD_REQUEST) == {
            'message': 'Todo not found',
            'status': STATUS_BAD_REQUEST,
            'data': None
        }

        # A body that is not a JSON object reads as {}: the route runs with
        # its params None and answers the 400 envelope, never a 500.
        for kwargs in ({}, {
                'data': 'todoId=x',
                'content_type': 'text/plain'
        }, {
                'json': ['rrh-a', 'json', 'list']
        }):
            res = client.post('/markTodoAsCompleted', **kwargs)

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': 'Todo not found',
                'status': STATUS_BAD_REQUEST,
                'data': None
            }
        errorLogs = _routeLogs(caplog, 'markTodoAsCompleted')
        assert [log['requestKeys'] for log in errorLogs
                ] == [['todoId'], [], [], []]
        assert {log['event'] for log in errorLogs} == {'error'}
    finally:
        wipeCollections(['Users', 'SampleTodos'])


def test_http_loginWithGoogle_envelopesAndNeverLogsTheSignInPayload(
        monkeypatch, caplog, capsys, firebase, anotherUser):
    """A verified token signs in with an access_token; a malformed body is
    a generic 400; outside localdev a raw identity or a token that does not
    verify is refused with a 403. None of them mints a token, and the
    sign-in payload never reaches a log record, at any level.

    The caplog record check is the guard. stderr (where the generated
    except blocks print the traceback, and which Cloud Run ships as logs)
    is checked too, as a lower bound: capsys sees what goes through
    sys.stderr, not a handler bound to the real stream."""
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()

    res = client.post('/loginWithGoogle',
                      json={'firebaseUserObject': {
                          'idToken': TOKEN
                      }})

    body = _success_body(res, 'loginWithGoogle')
    assert TOKEN_KEYS & set(body) == {'access_token'}
    assert _identity(flaskApp, body['access_token']) == TOKEN_UID
    # The wire's datetimes are RFC 1123 strings, so the keys are pinned
    # (the direct-call tests validate the shape).
    assert set(body['data']) == modelKeys(UserResponse)
    assert body['data']['_id'] == TOKEN_UID

    # A malformed body: pydantic would quote it, the refusal does not.
    res = client.post('/loginWithGoogle',
                      json={'firebaseUserObject': {
                          'idToken': [TOKEN_FRAGMENT]
                      }})

    assert _failure_body(res, STATUS_BAD_REQUEST) == {
        'message': 'Invalid sign-in request',
        'status': STATUS_BAD_REQUEST,
        'data': None
    }

    # Not a cloud value: outside localdev is all the refusals need.
    monkeypatch.setenv('ENVIRONMENT', 'localprod')

    res = client.post('/loginWithGoogle',
                      json={
                          'firebaseUserObject': {
                              'uid': RAW_UID,
                              'email': RAW_EMAIL,
                              'displayName': 'Rrh Raw'
                          }
                      })

    assert _failure_body(res, STATUS_FORBIDDEN) == {
        'message': 'Firebase ID token is required',
        'status': STATUS_FORBIDDEN,
        'data': None
    }
    assert db.read({'email': RAW_EMAIL}, 'Users', findOne=True) is None

    res = client.post('/loginWithGoogle',
                      json={'firebaseUserObject': {
                          'idToken': FORGED_TOKEN
                      }})

    assert _failure_body(res, STATUS_FORBIDDEN) == {
        'message': 'Sign-in could not be verified, please sign in again',
        'status': STATUS_FORBIDDEN,
        'data': None
    }
    assert [token for token, _ in firebase['verified']
            ] == [TOKEN, FORGED_TOKEN]

    loginLogs = _routeLogs(caplog, 'loginWithGoogle')
    assert {log['event'] for log in loginLogs} == {'success', 'error'}
    assert all(set(log) <= {'function', 'event', 'traceback'}
               for log in loginLogs)
    rejected = [
        r.msg for r in caplog.records if isinstance(r.msg, dict)
        and r.msg.get('event') == 'firebaseIdTokenRejected'
    ]
    assert rejected == [{
        'event': 'firebaseIdTokenRejected',
        'reason': 'ValueError'
    }]
    stderr = capsys.readouterr().err
    for sent in (TOKEN, FORGED_TOKEN, TOKEN_FRAGMENT, RAW_UID, RAW_EMAIL):
        for record in caplog.records:
            assert sent not in record.getMessage()
        assert sent not in stderr


def test_http_aScalarParamRefusesAnObjectOrAList(monkeypatch, caplog):
    """deleteTodo's todoId is a str: {"$ne": null} would be a Mongo operator
    matching every todo (a delete_many). The handler refuses an object or a
    list for any scalar param with a 400 before the route runs."""
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(['SampleTodos'])
    try:
        for title in ('Rrh one', 'Rrh two'):
            ApiRequests().createTodo(title)

        for todoId in ({'$ne': None}, ['rrh-a'], {'$gt': ''}):
            res = client.post('/deleteTodo', json={'todoId': todoId})

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': 'Invalid todoId',
                'status': STATUS_BAD_REQUEST,
                'data': None
            }
        assert len(db.read({}, 'SampleTodos')) == 2

        # A str still goes through, and a scalar of another type is the
        # route's own business (no strict coercion).
        res = client.post('/deleteTodo', json={'todoId': 'rrh-no-such-todo'})
        assert _success_body(res, 'deleteTodo')['data'] == {}
        assert len(db.read({}, 'SampleTodos')) == 2
    finally:
        wipeCollections(['SampleTodos'])


def test_http_aMalformedPubSubPush_isA400(monkeypatch, caplog):
    """The push envelope is parsed inside the try: a body without
    message.data/messageId is a 400 envelope (GCP retries, then
    dead-letters), never a 500. The log carries the messageId and the
    message's keys, never its values."""
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()

    for body in ({}, {'message': {}}, ['rrh-not-an-envelope']):
        res = client.post('/ping', json=body)

        payload = res.get_json()
        assert res.status_code == payload['status'] == STATUS_BAD_REQUEST
        assert payload['data'] is None
    logs = _routeLogs(caplog, 'ping')
    assert [log['event'] for log in logs] == ['error'] * 3
    assert all(log['messageId'] is None and log['messageKeys'] is None
               for log in logs)

    caplog.clear()
    message = {'secretField': SECRET_TITLE}
    res = client.post('/ping',
                      json={
                          'message': {
                              'data': message,
                              'messageId': None,
                              'attributes': {}
                          }
                      })

    assert res.status_code == STATUS_OK
    assert _routeLogs(caplog, 'ping') == [{
        'function': 'ping',
        'event': 'success',
        'messageId': None,
        'messageKeys': ['secretField']
    }]
    assert all(SECRET_TITLE not in r.getMessage() for r in caplog.records)


def test_http_localBindHost_isLoopbackUnlessOptedIn(monkeypatch, caplog,
                                                     capsys):
    """localdev accepts a raw sign-in identity: a local run is reachable
    from this machine only, unless server/.env sets LOCAL_BIND_HOST, which
    is then announced loudly."""
    _flask_app(monkeypatch)
    appModule = importlib.import_module('app')

    for value in (None, '', '  '):
        if value is None:
            monkeypatch.delenv('LOCAL_BIND_HOST', raising=False)
        else:
            monkeypatch.setenv('LOCAL_BIND_HOST', value)
        assert appModule._localBindHost() == '127.0.0.1'
    assert 'reachable from your network' not in capsys.readouterr().out

    monkeypatch.setenv('LOCAL_BIND_HOST', '0.0.0.0')
    with caplog.at_level(logging.WARNING):
        assert appModule._localBindHost() == '0.0.0.0'
    assert 'reachable from your network' in capsys.readouterr().out
    assert {'event': 'localBindHostExposed', 'host': '0.0.0.0'} in [
        r.msg for r in caplog.records
    ]

    with open(appModule.__file__, encoding='utf-8') as f:
        source = f.read()
    assert 'host=_localBindHost()' in source
    assert "host='0.0.0.0', port=8080" in source  # Cloud Run only


def _importAppIn(environment: str, jwtSecretKey: str):
    env = dict(os.environ,
               ENVIRONMENT=environment,
               JWT_SECRET_KEY=jwtSecretKey)
    return subprocess.run([sys.executable, '-c', 'import app'],
                          cwd=os.path.dirname(os.path.abspath(__file__)),
                          env=env,
                          capture_output=True,
                          text=True,
                          timeout=120)


def test_http_appRefusesAWeakJwtSecretOutsideLocaldev():
    """Anyone who knows the signing key can mint a token for any user: in
    every environment but localdev, app.py refuses the placeholder or a key
    under 32 characters. (localprod here: it reads the local Mongo only.)"""
    for weak in ('change-me-to-a-secret-key', 'x' * 31):
        result = _importAppIn('localprod', weak)

        assert result.returncode != 0
        assert 'JWT_SECRET_KEY must be a random secret' in result.stderr

    assert _importAppIn('localprod', 'x' * 32).returncode == 0
    assert _importAppIn('localdev', 'short').returncode == 0


STORE_COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Customers', 'Products']
SENT_STORE = {**STORE_DETAILS, 'name': 'Rrh Bakery'}


def test_http_createUserStore_withoutCurrency_storesPHP(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        variants = [('rrh-store-a', 'Rrh Alpha', {}),
                    ('rrh-store-b', 'Rrh Beta', {'currency': None}),
                    ('rrh-store-c', 'Rrh Gamma', {'openingHours': ''})]
        for adminId, name, extra in variants:
            createTestUser(adminId)

            res = client.post('/createUserStore',
                              json={**SENT_STORE, 'name': name,
                                    'userId': adminId, **extra},
                              headers=_bearer(_token(flaskApp, adminId)))

            data = _success_body(res, 'createUserStore')['data']
            assert data['name'] == name
            assert data['currency'] == 'PHP'
            assert data['openingHours'] is None
            assert data['location'] is None
            stored = db.read({'ownerUserId': adminId}, 'Stores', findOne=True)
            assert stored['currency'] == 'PHP'
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_listStoreProducts_withoutLimit_returnsAtMostTheDefault(
        monkeypatch, firebase):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        for number in range(PRODUCT_LIST_DEFAULT_LIMIT + 2):
            addProduct(store['_id'], f'Item {number:02d}', 10)
        customerId = guestAt(firebase, store, 'rrh-guest')
        headers = _bearer(_token(flaskApp, customerId))
        body = {'userId': customerId, 'storeId': store['_id']}

        for extra, expected in (({}, PRODUCT_LIST_DEFAULT_LIMIT),
                                ({'limit': None}, PRODUCT_LIST_DEFAULT_LIMIT),
                                ({'limit': 3}, 3)):
            res = client.post('/listStoreProducts', json={**body, **extra},
                              headers=headers)

            data = _success_body(res, 'listStoreProducts')['data']
            assert len(data['products']) == expected
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_setUserStoreLocation_savesTheLocation(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        createTestStore(ADMIN)

        res = client.post('/setUserStoreLocation',
                          json={'userId': ADMIN, 'latitude': 14.5,
                                'longitude': 121.0},
                          headers=_bearer(_token(flaskApp, ADMIN)))

        data = _success_body(res, 'setUserStoreLocation')['data']
        assert data['location'] == {'latitude': 14.5, 'longitude': 121.0}
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_setUserStoreLocation_aNonNumberOrHugeLatitude_isA400(
        monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        createTestStore(ADMIN)
        headers = _bearer(_token(flaskApp, ADMIN))
        huge = '1' + '0' * 400

        for latitude in ('NaN', 'Infinity', huge, '1e999', 'true', '"14.6"',
                         '91', 'null', None):
            text = ('{"userId": "%s", "longitude": 10%s}' % (
                ADMIN, '' if latitude is None else f', "latitude": {latitude}'))

            res = client.post('/setUserStoreLocation', data=text,
                              content_type='application/json',
                              headers=headers)

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': 'The latitude is a number from -90 to 90',
                'status': STATUS_BAD_REQUEST,
                'data': None
            }
        stored = db.read({}, 'Stores', findOne=True)
        assert stored['location'] is None
        assert stored['_version'] == 0
    finally:
        wipeCollections(STORE_COLLECTIONS)


SENT_PRODUCT = {'name': 'Rrh Pandesal', 'price': 5, 'inventory': 100}
PRICE_MESSAGE = 'The price is a number from 0 to 10000000'


def _postJson(client, route, headers, text):
    return client.post(route, data=text, content_type='application/json',
                       headers=headers)


def test_http_addStoreProducts_acceptsDictItems_refusesNonLists(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        createTestStore(ADMIN)
        headers = _bearer(_token(flaskApp, ADMIN))

        res = client.post('/addStoreProducts',
                          json={'userId': ADMIN, 'products': [
                              SENT_PRODUCT,
                              {**SENT_PRODUCT, 'name': 'Rrh Tea',
                               'isAvailable': False}]},
                          headers=headers)

        data = _success_body(res, 'addStoreProducts')['data']
        assert [p['name'] for p in data['products']] == ['Rrh Pandesal',
                                                         'Rrh Tea']
        assert [p['isAvailable'] for p in data['products']] == [True, False]
        assert len(db.read({}, 'Products')) == 2

        refused = [
            ({'a': 1}, 'Invalid products'),
            ('x', 'Invalid products'),
            (None, 'Invalid products'),
            ([], 'Send 1 to 50 products at a time'),
            ([dict(SENT_PRODUCT, name=f'Rrh {n}') for n in range(51)],
             'Send 1 to 50 products at a time'),
            ([5], 'Product 1: send it as an object'),
            (['x'], 'Product 1: send it as an object'),
            ([{**SENT_PRODUCT, 'name': 'Rrh New', 'price': '5'}],
             'Product 1: ' + PRICE_MESSAGE[:1].lower() + PRICE_MESSAGE[1:]),
        ]
        for products, message in refused:
            res = client.post('/addStoreProducts',
                              json={'userId': ADMIN, 'products': products},
                              headers=headers)

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': message, 'status': STATUS_BAD_REQUEST,
                'data': None}
        res = client.post('/addStoreProducts', json={'userId': ADMIN},
                          headers=headers)
        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == 'Invalid products'
        assert len(db.read({}, 'Products')) == 2
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_addStoreProduct_missingKeys_andOddPrices(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        createTestStore(ADMIN)
        headers = _bearer(_token(flaskApp, ADMIN))

        res = client.post('/addStoreProduct',
                          json={'userId': ADMIN, **SENT_PRODUCT},
                          headers=headers)

        data = _success_body(res, 'addStoreProduct')['data']
        assert data['isAvailable'] is True
        assert data['description'] is None
        res = client.post('/addStoreProduct',
                          json={'userId': ADMIN, **SENT_PRODUCT,
                                'name': 'Rrh Null', 'isAvailable': None},
                          headers=headers)
        assert _success_body(res, 'addStoreProduct')['data'][
            'isAvailable'] is True

        res = client.post('/addStoreProduct',
                          json={'userId': ADMIN, 'price': 1, 'inventory': 1},
                          headers=headers)
        assert _failure_body(res, STATUS_BAD_REQUEST)['message'] == (
            'The name is 1 to 80 characters')
        res = client.post('/addStoreProduct',
                          json={'userId': ADMIN, 'name': 'Rrh X',
                                'inventory': 1}, headers=headers)
        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == PRICE_MESSAGE

        # A price that is no number is a 400 on the wire, never a 500.
        for price in ('NaN', 'Infinity', '-Infinity', '1e999', 'true', '"5"',
                      'null', '1' + '0' * 400):
            text = ('{"userId": "%s", "name": "Rrh Odd", "inventory": 1, '
                    '"price": %s}' % (ADMIN, price))
            res = _postJson(client, '/addStoreProduct', headers, text)

            assert _failure_body(res, STATUS_BAD_REQUEST)[
                'message'] == PRICE_MESSAGE
        text = ('{"userId": "%s", "name": "Rrh Zero", "inventory": 1, '
                '"price": -0.0}' % ADMIN)
        res = _postJson(client, '/addStoreProduct', headers, text)
        assert str(_success_body(res, 'addStoreProduct')['data'][
            'price']) == '0.0'
        assert str(db.read({'name': 'Rrh Zero'}, 'Products',
                           findOne=True)['price']) == '0.0'
        assert db.read({'name': 'Rrh Odd'}, 'Products') == []
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_updateStoreProduct_missingKeys(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        product = addProduct(store['_id'], 'Rrh Pandesal', 5,
                             description='Soft', inventory=10)
        headers = _bearer(_token(flaskApp, ADMIN))

        def post(**fields):
            return client.post('/updateStoreProduct',
                               json={'userId': ADMIN,
                                     'productId': product['_id'], **fields},
                               headers=headers)

        data = _success_body(post(price=7), 'updateStoreProduct')['data']
        assert (data['name'], data['description'], data['price'],
                data['inventory'], data['isAvailable']) == (
                    'Rrh Pandesal', 'Soft', 7.0, 10, True)
        # A blank name means unchanged.
        data = _success_body(post(name='', price=8),
                             'updateStoreProduct')['data']
        assert (data['name'], data['price']) == ('Rrh Pandesal', 8.0)

        assert _failure_body(post(), STATUS_BAD_REQUEST)['message'] == (
            'Send at least one field to change')
        res = client.post('/updateStoreProduct',
                          json={'userId': ADMIN, 'price': 1}, headers=headers)
        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == 'Product not found'
        res = client.post('/updateStoreProduct',
                          json={'userId': ADMIN, 'productId': {'$ne': None},
                                'price': 1}, headers=headers)
        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == 'Invalid productId'
        assert _failure_body(post(isAvailable='no'), STATUS_BAD_REQUEST)[
            'message'] == 'isAvailable is true or false'
        stored = db.read({'_id': product['_id']}, 'Products', findOne=True)
        assert stored['_version'] == 2
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_listMyStoreProducts_searchAndLimit(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        for number in range(PRODUCT_LIST_DEFAULT_LIMIT + 2):
            addProduct(store['_id'], f'Rrh Item {number:02d}', 1,
                       isAvailable=number % 2 == 0)
        headers = _bearer(_token(flaskApp, ADMIN))

        def post(**fields):
            return client.post('/listMyStoreProducts',
                               json={'userId': ADMIN, **fields},
                               headers=headers)

        data = _success_body(post(), 'listMyStoreProducts')['data']
        assert len(data['products']) == PRODUCT_LIST_DEFAULT_LIMIT
        assert any(not p['isAvailable'] for p in data['products'])
        assert all('reservedInventory' not in p for p in data['products'])
        data = _success_body(post(limit=None, search='item 1'),
                             'listMyStoreProducts')['data']
        assert [p['name'] for p in data['products']] == [
            'Rrh Item 10', 'Rrh Item 11']

        for search in (5, True):
            assert _failure_body(post(search=search), STATUS_BAD_REQUEST)[
                'message'] == 'The search must be text'
        for search in ({'$ne': None}, ['a']):
            assert _failure_body(post(search=search), STATUS_BAD_REQUEST)[
                'message'] == 'Invalid search'
    finally:
        wipeCollections(STORE_COLLECTIONS)


def test_http_listStoreProducts_aNonStringSearch_isA400(monkeypatch, firebase):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        addProduct(store['_id'], 'Rrh Pandesal', 5)
        customerId = guestAt(firebase, store, 'rrh-guest-search')

        res = client.post('/listStoreProducts',
                          json={'userId': customerId, 'storeId': store['_id'],
                                'search': 5},
                          headers=_bearer(_token(flaskApp, customerId)))

        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == 'The search must be text'
    finally:
        wipeCollections(STORE_COLLECTIONS)


@pytest.mark.parametrize('route, body', [
    ('addStoreProduct', SENT_PRODUCT),
    ('addStoreProducts', {'products': [SENT_PRODUCT]}),
    ('updateStoreProduct', {'productId': 'rrh-any', 'price': 1}),
    ('listMyStoreProducts', {}),
])
def test_http_adminProductRoutes_refuseAForeignUserId(monkeypatch, route,
                                                      body):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(STORE_COLLECTIONS)
    try:
        createTestStore(ADMIN)

        res = client.post('/' + route,
                          json={'userId': 'rrh-someone-else', **body},
                          headers=_bearer(_token(flaskApp, ADMIN)))

        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == 'Unauthorized access'
        assert db.read({}, 'Products') == []
    finally:
        wipeCollections(STORE_COLLECTIONS)


ORDER_COLLECTIONS = STORE_COLLECTIONS + ['Orders']
ORDER_SENTINEL = 'ZQ-secret-7'
ORDER_LIMIT = 'limit is a whole number from 1 to 50'
ORDER_ITEMS = [{'productId': 'p1', 'name': 'Ube Cake', 'price': 650,
                'quantity': 2}]


@pytest.mark.parametrize('route, body, param', [
    ('getStoreOrders', {'status': {'$ne': 'x'}}, 'status'),
    ('getStoreOrders', {'status': ['pending']}, 'status'),
    ('getStoreOrders', {'period': {'$ne': 'x'}}, 'period'),
    ('getStoreOrders', {'period': ['today']}, 'period'),
    ('getStoreOrders', {'startDate': {'$gt': ''}}, 'startDate'),
    ('getStoreOrders', {'endDate': ['2026-03-10']}, 'endDate'),
    ('getStoreOrders', {'limit': {'$gt': 0}}, 'limit'),
    ('getMyOrders', {'status': {'$ne': 'x'}}, 'status'),
    ('getMyOrders', {'status': ['pending']}, 'status'),
    ('getMyOrders', {'storeId': {'$ne': None}}, 'storeId'),
    ('getMyOrders', {'storeId': ['a']}, 'storeId'),
    ('getMyOrders', {'limit': ['1']}, 'limit'),
])
def test_http_orderRoutes_anObjectOrListParam_isA400(monkeypatch, firebase,
                                                     route, body, param):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        customerId = guestAt(firebase, store, 'rrh-guest-orders')
        addOrder(store['_id'], customerId, ORDER_ITEMS)
        caller = ADMIN if route == 'getStoreOrders' else customerId
        sent = {'userId': caller, 'storeId': store['_id'], **body}

        res = client.post('/' + route, json=sent,
                          headers=_bearer(_token(flaskApp, caller)))

        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == f'Invalid {param}'
    finally:
        wipeCollections(ORDER_COLLECTIONS)


def test_http_getStoreOrders_period_andItsRefusals(monkeypatch):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = addOrder(store['_id'], 'rrh-customer', ORDER_ITEMS,
                          createdAt=now)['_id']
        addOrder(store['_id'], 'rrh-customer', ORDER_ITEMS,
                 createdAt=now - datetime.timedelta(days=30))
        headers = _bearer(_token(flaskApp, ADMIN))

        def post(**fields):
            return client.post('/getStoreOrders',
                               json={'userId': ADMIN, **fields},
                               headers=headers)

        data = _success_body(post(period='last7Days'),
                             'getStoreOrders')['data']
        assert [o['_id'] for o in data['orders']] == [recent]
        assert set(data['orders'][0]) == modelKeys(StoreOrderRow)
        # Missing optional keys reach the route as None.
        assert len(_success_body(post(), 'getStoreOrders')['data'][
            'orders']) == 2
        for fields, message in [
            ({'period': ORDER_SENTINEL}, INVALID_PERIOD),
            ({'status': ORDER_SENTINEL}, INVALID_STATUS),
            ({'period': 'today', 'startDate': '2026-03-10'},
             PERIOD_AND_DATES),
            ({'startDate': ORDER_SENTINEL}, _ISO_DATE_MESSAGE),
            ({'limit': True}, ORDER_LIMIT),
            ({'limit': ORDER_SENTINEL}, ORDER_LIMIT),
        ]:
            body = _failure_body(post(**fields), STATUS_BAD_REQUEST)
            assert body['message'] == message
            assert ORDER_SENTINEL not in body['message']
    finally:
        wipeCollections(ORDER_COLLECTIONS)


def test_http_getMyOrders_envelope_limit_andOwnOrdersOnly(monkeypatch,
                                                          firebase):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        customerId = guestAt(firebase, store, 'rrh-guest-mine')
        otherId = guestAt(firebase, store, 'rrh-guest-theirs')
        mine = addOrder(store['_id'], customerId, ORDER_ITEMS)['_id']
        addOrder(store['_id'], otherId, ORDER_ITEMS)
        headers = _bearer(_token(flaskApp, customerId))

        def post(**fields):
            return client.post('/getMyOrders',
                               json={'userId': customerId,
                                     'storeId': store['_id'], **fields},
                               headers=headers)

        data = _success_body(post(), 'getMyOrders')['data']
        assert [o['_id'] for o in data['orders']] == [mine]
        assert set(data['orders'][0]) == modelKeys(MyOrderRow)
        assert _success_body(post(storeId='rrh-unknown'), 'getMyOrders')[
            'data'] == {'orders': []}
        for fields, message in [
            ({'status': ORDER_SENTINEL}, INVALID_STATUS),
            ({'limit': True}, ORDER_LIMIT),
        ]:
            body = _failure_body(post(**fields), STATUS_BAD_REQUEST)
            assert body['message'] == message
    finally:
        wipeCollections(ORDER_COLLECTIONS)


def test_http_orderRoutes_refuseTheOtherRole_with403(monkeypatch, firebase):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        customerId = guestAt(firebase, store, 'rrh-guest-role')
        addOrder(store['_id'], customerId, ORDER_ITEMS)

        asCustomer = client.post(
            '/getStoreOrders', json={'userId': customerId},
            headers=_bearer(_token(flaskApp, customerId)))
        asAdmin = client.post(
            '/getMyOrders', json={'userId': ADMIN, 'storeId': store['_id']},
            headers=_bearer(_token(flaskApp, ADMIN)))

        assert _failure_body(asCustomer, STATUS_FORBIDDEN)[
            'message'] == 'Only a store admin can do this'
        assert _failure_body(asAdmin, STATUS_FORBIDDEN)[
            'message'] == 'Only a customer can do this'
    finally:
        wipeCollections(ORDER_COLLECTIONS)

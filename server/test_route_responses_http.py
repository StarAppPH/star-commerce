"""HTTP envelopes of the generated Flask app (app.py).

Every other route test calls ApiRequests() directly, as DO_NOT_DO.md
requires. These are the exception it allows: they test the generated
TRANSPORT layer, which a direct call never reaches -
  - the ApiResponse envelope each kind of route answers with
    (createAccessToken, jwtRequired, public): the ApiSuccess / ApiFailure
    the TS client is typed against, with status_code equal to body.status
    and no key outside ApiResponse;
  - the status mapping: a ValueError the route raises, a failed role check
    (getRoles' roleAccess: the role's unauthorizedMessage) and a body
    userId that is not the JWT identity are 400 envelopes with data null;
    a PermissionError (sign-in, the other role's routes) is a 403;
  - a POST whose body is not a JSON object (empty, text/plain, a JSON
    list) is a 400 envelope, never a 500, and an object or a list for a
    scalar param is refused (a {"$ne": null} never reaches a query); a
    malformed Pub/Sub push is a 400 too;
  - the JWT gate: a missing, expired or malformed token is turned away with
    a {msg}-only body before the route runs; a JWT route that succeeds hands
    back a refreshed jwtToken the next call is signed with, and a failed one
    hands back none;
  - logging: a route logs which of its params it was sent, never the
    values; a push handler logs the messageId and the message's keys;
  - app.py's config: outside localdev a short or placeholder
    JWT_SECRET_KEY is refused; a local run binds 127.0.0.1 unless
    LOCAL_BIND_HOST says otherwise, and then warns;
  - sign-in: the loginWithGoogle envelopes, and the sign-in payload (the ID
    token, a token fragment, a raw identity) never reaching a log record or
    stderr;
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
from conftest import (RAW_EMAIL, RAW_UID, TOKEN, TOKEN_UID,
                      createTestStore, createTestUser, guestAt,
                      modelKeys, wipeCollections)
from flask_jwt_extended import create_access_token, decode_token
from mongoDb import db
from objects import ApiResponse, UserResponse
from roles import AllRoles

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


def test_http_aJwtRouteThatSucceeds_handsBackARefreshedToken(monkeypatch,
                                                             caplog):
    """getRoles (jwtRequired, roleAccess): the caller rides along with a
    refreshed jwtToken the next call is signed with, and the log carries
    the route's own params it was sent, never their values."""
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(['Users'])
    try:
        createTestUser(ADMIN)

        res = client.post('/getRoles', json={'userId': ADMIN},
                          headers=_bearer(_token(flaskApp, ADMIN)))

        body = _success_body(res, 'getRoles')
        assert TOKEN_KEYS & set(body) == {'current_user', 'jwtToken'}
        assert body['current_user'] == ADMIN
        assert _identity(flaskApp, body['jwtToken']) == ADMIN
        assert body['data'] == ApiRequests().getRoles(ADMIN)

        # The client signs its next call with the refreshed token; a key
        # the route does not take is not logged (the caller chose its name).
        caplog.clear()
        res = client.post('/getRoles',
                          json={'userId': ADMIN, SECRET_TITLE: 1},
                          headers=_bearer(body['jwtToken']))

        _success_body(res, 'getRoles')
        assert _routeLogs(caplog, 'getRoles') == [{
            'function': 'getRoles',
            'event': 'success',
            'requestKeys': ['userId']
        }]
        assert all(SECRET_TITLE not in r.getMessage() for r in caplog.records)
    finally:
        wipeCollections(['Users'])


def test_http_jwtGateAndRoleCheck_failureEnvelopes(monkeypatch):
    """The JWT gate turns a missing, expired or garbage token away with a
    {msg}-only body before the route runs (the generated client maps it to
    its 401 ApiFailure); a token for someone other than the body's userId,
    and a caller without the route's role, are 400 envelopes with data
    null and no token."""
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(['Users'])
    try:
        createTestUser(ADMIN)
        createTestUser(NOBODY, userType='unauthorized')

        for token in [
                None,
                _token(flaskApp, ADMIN, datetime.timedelta(seconds=-10)),
                'not-a-jwt',
        ]:
            res = client.post('/getRoles',
                              json={'userId': ADMIN},
                              headers=_bearer(token) if token else None)

            body = res.get_json()
            assert res.status_code in (STATUS_JWT_REJECTED,
                                       HTTPStatus.UNPROCESSABLE_ENTITY)
            assert set(body) == {'msg'}
            assert body['msg']

        res = client.post('/getRoles',
                          json={'userId': ADMIN},
                          headers=_bearer(_token(flaskApp, NOBODY)))

        assert _failure_body(res, STATUS_BAD_REQUEST) == {
            'message': 'Unauthorized access',
            'status': STATUS_BAD_REQUEST,
            'data': None
        }

        # The caller's own token, without the route's role (roleAccess):
        # the role's own message.
        res = client.post('/getRoles',
                          json={'userId': NOBODY},
                          headers=_bearer(_token(flaskApp, NOBODY)))

        assert _failure_body(res, STATUS_BAD_REQUEST) == {
            'message':
            AllRoles().getSpecificRole('canUpdateUserRole').unauthorizedMessage,
            'status': STATUS_BAD_REQUEST,
            'data': None
        }
    finally:
        wipeCollections(['Users'])


NOT_A_JSON_OBJECT = [{}, {'data': 'userId=x', 'content_type': 'text/plain'},
                     {'json': ['rrh-a', 'json', 'list']}]


def test_http_aBodyThatIsNotAJsonObject_isA400Envelope(monkeypatch, caplog,
                                                        firebase):
    """An empty body, text/plain or a JSON list reads as {}: the route
    runs with its params None and answers a 400 envelope, never a 500.
    listMyConversations (a JWT route) refuses the missing userId as not
    the caller's; devLogin (public) runs and finds no user."""
    caplog.set_level(logging.INFO)
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        headers = _bearer(_token(flaskApp, guestAt(firebase, store,
                                                   'rrh-guest-body')))

        for kwargs in NOT_A_JSON_OBJECT:
            res = client.post('/listMyConversations', headers=headers,
                              **kwargs)

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': 'Unauthorized access',
                'status': STATUS_BAD_REQUEST,
                'data': None
            }

            res = client.post('/devLogin', **kwargs)

            assert _failure_body(res, STATUS_BAD_REQUEST) == {
                'message': 'User not found',
                'status': STATUS_BAD_REQUEST,
                'data': None
            }
        errorLogs = _routeLogs(caplog, 'listMyConversations')
        assert [log['requestKeys'] for log in errorLogs] == [[], [], []]
        assert {log['event'] for log in errorLogs} == {'error'}
    finally:
        wipeCollections(ORDER_COLLECTIONS)


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
ORDER_COLLECTIONS = STORE_COLLECTIONS + ['Orders']


@pytest.mark.parametrize('route, body, param', [
    ('listMyConversations', {'storeId': {'$ne': None}}, 'storeId'),
    ('listMyConversations', {'storeId': ['a']}, 'storeId'),
    ('listMyConversations', {'before': {'$gt': ''}}, 'before'),
    ('listMyConversations', {'before': ['2026-03-10']}, 'before'),
    ('listMyConversations', {'limit': {'$gt': 0}}, 'limit'),
    ('getConversationTurns', {'conversationId': {'$ne': None}},
     'conversationId'),
    ('getConversationTurns', {'conversationId': ['a']}, 'conversationId'),
    ('getConversationTurns', {'beforeSeq': {'$gt': 0}}, 'beforeSeq'),
    ('getConversationTurns', {'beforeSeq': ['1']}, 'beforeSeq'),
    ('getConversationTurns', {'limit': ['1']}, 'limit'),
])
def test_http_anObjectOrListParam_isA400(monkeypatch, firebase, route, body,
                                         param):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        customerId = guestAt(firebase, store, 'rrh-guest-orders')
        sent = {'userId': customerId, 'storeId': store['_id'],
                'conversationId': 'rrh-conversation', **body}

        res = client.post('/' + route, json=sent,
                          headers=_bearer(_token(flaskApp, customerId)))

        assert _failure_body(res, STATUS_BAD_REQUEST)[
            'message'] == f'Invalid {param}'
    finally:
        wipeCollections(ORDER_COLLECTIONS)


def test_http_routes_refuseTheOtherRole_with403(monkeypatch, firebase):
    flaskApp = _flask_app(monkeypatch)
    client = flaskApp.test_client()
    wipeCollections(ORDER_COLLECTIONS)
    try:
        store = createTestStore(ADMIN)
        customerId = guestAt(firebase, store, 'rrh-guest-role')

        asCustomer = client.post(
            '/getUserStore', json={'userId': customerId},
            headers=_bearer(_token(flaskApp, customerId)))
        asAdmin = client.post(
            '/listMyConversations',
            json={'userId': ADMIN, 'storeId': store['_id']},
            headers=_bearer(_token(flaskApp, ADMIN)))

        assert _failure_body(asCustomer, STATUS_FORBIDDEN)[
            'message'] == 'Only a store admin can do this'
        assert _failure_body(asAdmin, STATUS_FORBIDDEN)[
            'message'] == 'Only a customer can do this'
    finally:
        wipeCollections(ORDER_COLLECTIONS)


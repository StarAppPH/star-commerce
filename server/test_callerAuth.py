"""callerAuth.py: only a Google-signed caller reaches a machine's route.

Covers:
  - the verifier: a token is accepted only when RS256, signed by a cert
    Google serves, unexpired, from accounts.google.com, with a verified
    email in the allowlist and an audience naming THIS request (a push: its
    exact URL; a service call: its host). Anything else is a 401 whose log
    names the reason, never the token;
  - Google's certs: cached, refetched at most once a minute, the last good
    ones kept through a failed fetch for a grace period, and an answer of
    the wrong shape refused;
  - localdev skips the check; CALLER_AUTH_MODE=report logs a refusal and
    runs the route until CALLER_AUTH_REPORT_UNTIL, which it requires, and
    warns at startup; any other mode enforces; callerVerified() is True
    only for a caller that passed;
  - the generated code: every push handler is @requirePushCaller, every
    serviceOnly route (exactly the routes of ServiceToServiceActions.py) is
    @requireServiceCaller and no other route is.

No network and no database: the certs fetch is stubbed with a key pair
made here, and tokens are signed with it.
"""
import ast
import datetime
import inspect
import logging
import time

import flask
import pytest

import AppCreator
import callerAuth
from route_config import route_config

# >>> star-app per-repo: callerAuth expected accounts
# The accounts this repo's callerAuth.py accepts, restated: a push or a
# sibling's call is refused outside localdev unless its account is listed
# there, and an empty PUSH_SERVICE_ACCOUNTS refuses every push.
EXPECTED_PUSH_SERVICE_ACCOUNTS: tuple[str, ...] = (
    'star-commerce-push@star-commerce-6797.iam.gserviceaccount.com',
)
EXPECTED_SERVICE_CALLER_ACCOUNTS: tuple[str, ...] = ()
# <<< star-app per-repo: callerAuth expected accounts

KEY_ID = 'test-key'
HOST = 'svc-abc123-de.a.run.app'
PUSH_ACCOUNT = 'push@test-project.iam.gserviceaccount.com'
SERVICE_ACCOUNT = 'sibling@test-project.iam.gserviceaccount.com'
OTHER_ACCOUNT = 'someone@test-project.iam.gserviceaccount.com'

_keys = {}


def _keyPair():
    """(private PEM, public PEM), PKCS#1, made once per run with whichever
    RSA library google-auth itself uses here."""
    if not _keys:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            key = rsa.generate_private_key(public_exponent=65537,
                                           key_size=2048)
            private = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption())
            public = key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.PKCS1)
        except ImportError:
            import rsa
            public, private = rsa.newkeys(2048)
            private, public = private.save_pkcs1(), public.save_pkcs1()
        _keys['private'] = private.decode()
        _keys['public'] = public.decode()
    return _keys['private'], _keys['public']


def _token(keyId=KEY_ID, header=None, **overrides):
    from google.auth import crypt, jwt
    now = int(time.time())
    claims = {
        'iss': 'https://accounts.google.com',
        'aud': f'https://{HOST}/handler',
        'email': PUSH_ACCOUNT,
        'email_verified': True,
        'iat': now,
        'exp': now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    signer = crypt.RSASigner.from_string(_keyPair()[0], key_id=keyId)
    return jwt.encode(signer, claims, header=header).decode()


@pytest.fixture
def cloud(monkeypatch):
    """A deployed service: cloudprod, enforcing, the test accounts allowed
    and Google's certs (counted) served from the test key pair."""
    fetches = []

    def fetchCerts():
        fetches.append(True)
        return {KEY_ID: _keyPair()[1]}

    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    monkeypatch.delenv(callerAuth.MODE_ENV, raising=False)
    monkeypatch.delenv(callerAuth.REPORT_UNTIL_ENV, raising=False)
    monkeypatch.setattr(callerAuth, 'PUSH_SERVICE_ACCOUNTS', (PUSH_ACCOUNT, ))
    monkeypatch.setattr(callerAuth, 'SERVICE_CALLER_ACCOUNTS',
                        (SERVICE_ACCOUNT, ))
    monkeypatch.setattr(callerAuth, '_fetchCerts', fetchCerts)
    monkeypatch.setattr(callerAuth, '_certsCache', {
        'certs': None,
        'fetchedAt': None,
        'attemptedAt': None
    })
    return fetches


def _client(decorator):
    app = flask.Flask(__name__)
    ran = []

    @app.route('/handler', methods=['POST'])
    @decorator
    def handler():
        ran.append(True)
        return {'ran': True}

    return app.test_client(), ran


def _post(decorator, token=None, authorization=None):
    client, ran = _client(decorator)
    headers = {}
    if authorization is not None:
        headers['Authorization'] = authorization
    elif token is not None:
        headers['Authorization'] = f'Bearer {token}'
    response = client.post('/handler',
                           headers=headers,
                           base_url=f'https://{HOST}')
    return response, ran


def _assertRefused(response, ran):
    assert response.status_code == 401
    assert response.get_json() == {'message': 'Unauthorized', 'status': 401}
    assert ran == []


# ----------------------------- push handlers ----------------------------- #


def test_aPushWithAValidToken_runs(cloud):
    response, ran = _post(callerAuth.requirePushCaller, _token())

    assert response.status_code == 200
    assert ran == [True]


@pytest.mark.parametrize('authorization, reason', [
    (None, 'missingToken'),
    ('', 'missingToken'),
    ('Basic abc', 'notBearer'),
    ('Bearer ', 'notBearer'),
    ('Bearer not.a.jwt', 'malformedToken'),
])
def test_aPushWithoutABearerToken_isRefused(cloud, caplog, authorization,
                                             reason):
    client, ran = _client(callerAuth.requirePushCaller)
    headers = {} if authorization is None else {'Authorization': authorization}
    with caplog.at_level(logging.WARNING):
        response = client.post('/handler',
                               headers=headers,
                               base_url=f'https://{HOST}')

    _assertRefused(response, ran)
    (record, ) = [r for r in caplog.records
                  if isinstance(r.msg, dict)
                  and r.msg.get('event') == 'callerRejected']
    assert record.msg['reason'] == reason
    assert record.msg['function'] == 'handler'
    assert record.msg['caller'] == 'push'
    assert record.msg['mode'] == 'enforce'


@pytest.mark.parametrize('overrides, reason', [
    ({'aud': f'https://{HOST}/otherHandler'}, 'wrongAudience'),
    ({'aud': 'https://other-abc123-de.a.run.app/handler'}, 'wrongAudience'),
    ({'aud': f'http://{HOST}/handler'}, 'wrongAudience'),
    ({'aud': f'https://{HOST}'}, 'wrongAudience'),
    ({'iss': 'https://evil.example.com'}, 'wrongIssuer'),
    ({'email': OTHER_ACCOUNT}, 'callerNotAllowed'),
    ({'email': SERVICE_ACCOUNT}, 'callerNotAllowed'),
    ({'email': None}, 'callerNotAllowed'),
    ({'email_verified': False}, 'emailNotVerified'),
    ({'email_verified': None}, 'emailNotVerified'),
    ({'email_verified': 'true'}, 'emailNotVerified'),
    ({'exp': int(time.time()) - 3600, 'iat': int(time.time()) - 7200},
     'invalidToken'),
    ({'exp': None}, 'invalidToken'),
])
def test_aPushWhoseTokenIsNotForThisHandler_isRefused(cloud, caplog,
                                                      overrides, reason):
    with caplog.at_level(logging.WARNING):
        response, ran = _post(callerAuth.requirePushCaller,
                              _token(**overrides))

    _assertRefused(response, ran)
    assert [r.msg['reason'] for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'callerRejected'] == [reason]


def test_aTokenSignedByAnotherKey_isRefused(cloud):
    token = _token()
    header, payload, signature = token.split('.')
    forged = '.'.join([header, payload, signature[::-1]])

    response, ran = _post(callerAuth.requirePushCaller, forged)

    _assertRefused(response, ran)


def test_aTokenSignedByAnotherKeyUnderTheSameKeyId_isRefused(cloud, caplog):
    from google.auth import crypt, jwt
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        otherKey = rsa.generate_private_key(
            public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()).decode()
    except ImportError:
        import rsa
        otherKey = rsa.newkeys(2048)[1].save_pkcs1().decode()
    now = int(time.time())
    token = jwt.encode(
        crypt.RSASigner.from_string(otherKey, key_id=KEY_ID), {
            'iss': 'https://accounts.google.com',
            'aud': f'https://{HOST}/handler',
            'email': PUSH_ACCOUNT,
            'email_verified': True,
            'iat': now,
            'exp': now + 3600,
        }).decode()

    with caplog.at_level(logging.WARNING):
        response, ran = _post(callerAuth.requirePushCaller, token)

    _assertRefused(response, ran)
    assert _reasons(caplog) == ['invalidToken']


def test_theBareGoogleIssuer_isAccepted(cloud):
    """Google signs ID tokens with either issuer form."""
    response, ran = _post(callerAuth.requirePushCaller,
                          _token(iss='accounts.google.com'))

    assert response.status_code == 200
    assert ran == [True]


@pytest.mark.parametrize('scheme', ['bearer', 'BEARER'])
def test_theBearerScheme_isCaseInsensitive(cloud, scheme):
    response, ran = _post(callerAuth.requirePushCaller,
                          authorization=f'{scheme} {_token()}')

    assert response.status_code == 200
    assert ran == [True]


def test_aTokenWithAnUnknownKeyId_isRefused(cloud):
    response, ran = _post(callerAuth.requirePushCaller,
                          _token(keyId='rotated-away'))

    _assertRefused(response, ran)


def test_anUnsignedToken_isRefused(cloud):
    import base64
    import json

    def part(value):
        return base64.urlsafe_b64encode(
            json.dumps(value).encode()).rstrip(b'=').decode()

    now = int(time.time())
    token = '.'.join([
        part({'alg': 'none', 'kid': KEY_ID}),
        part({'iss': 'https://accounts.google.com',
              'aud': f'https://{HOST}/handler', 'email': PUSH_ACCOUNT,
              'email_verified': True, 'iat': now, 'exp': now + 3600}), ''
    ])

    response, ran = _post(callerAuth.requirePushCaller, token)

    _assertRefused(response, ran)


def _reasons(caplog):
    return [r.msg['reason'] for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'callerRejected']


def test_aTokenClaimingAnotherAlgorithm_isRefused(cloud, caplog):
    """google-auth verifies any RS* header with RSA-SHA256, so the header's
    claim is checked here."""
    with caplog.at_level(logging.WARNING):
        response, ran = _post(callerAuth.requirePushCaller,
                              _token(header={'alg': 'RS512'}))

    _assertRefused(response, ran)
    assert _reasons(caplog) == ['wrongAlgorithm']


def test_anEmptyAllowlist_refusesEveryCaller(cloud, monkeypatch, caplog):
    monkeypatch.setattr(callerAuth, 'PUSH_SERVICE_ACCOUNTS', ())
    with caplog.at_level(logging.WARNING):
        response, ran = _post(callerAuth.requirePushCaller, _token())

    _assertRefused(response, ran)
    assert _reasons(caplog) == ['noAllowedCallers']


def test_theLogNeverCarriesTheTokenOrItsClaims(cloud, caplog):
    token = _token(email=OTHER_ACCOUNT)
    with caplog.at_level(logging.DEBUG):
        _post(callerAuth.requirePushCaller, token)

    logged = caplog.text
    assert token not in logged
    assert token.split('.')[1] not in logged
    assert OTHER_ACCOUNT not in logged


# ----------------------------- service routes ----------------------------- #


def test_aServiceCallWithAValidToken_runs(cloud):
    for audience in (f'https://{HOST}', f'https://{HOST}/'):
        response, ran = _post(callerAuth.requireServiceCaller,
                              _token(aud=audience, email=SERVICE_ACCOUNT))

        assert response.status_code == 200
        assert ran == [True]


@pytest.mark.parametrize('overrides', [
    {'aud': 'https://other-abc123-de.a.run.app', 'email': SERVICE_ACCOUNT},
    {'aud': f'https://{HOST}/handler', 'email': SERVICE_ACCOUNT},
    {'aud': f'https://{HOST}/?x=1', 'email': SERVICE_ACCOUNT},
    {'aud': f'http://{HOST}', 'email': SERVICE_ACCOUNT},
    {'aud': f'https://{HOST}', 'email': PUSH_ACCOUNT},
    {'aud': f'https://{HOST}', 'email': OTHER_ACCOUNT},
    {'aud': f'https://{HOST}', 'email': SERVICE_ACCOUNT,
     'email_verified': False},
])
def test_aServiceCallWhoseTokenIsNotForThisService_isRefused(cloud, overrides):
    response, ran = _post(callerAuth.requireServiceCaller, _token(**overrides))

    _assertRefused(response, ran)


def test_aServiceCallWithoutAToken_isRefused(cloud):
    response, ran = _post(callerAuth.requireServiceCaller)

    _assertRefused(response, ran)


# ----------------------------- modes ----------------------------- #


def test_localdev_skipsTheCheck(cloud, monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    for decorator in (callerAuth.requirePushCaller,
                      callerAuth.requireServiceCaller):
        response, ran = _post(decorator)

        assert response.status_code == 200
        assert ran == [True]


def test_anEnvironmentThatCannotBeRead_enforces(cloud, monkeypatch):
    """An AppConfig that raises reads as not localdev: the check runs."""
    import AppConfig

    class _Unreadable:

        def __init__(self):
            raise RuntimeError('ENVIRONMENT cannot be read')

    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setattr(AppConfig, 'Environment', _Unreadable)

    response, ran = _post(callerAuth.requirePushCaller)

    _assertRefused(response, ran)


@pytest.mark.parametrize('environment', ['localprod', 'clouddev', 'cloudprod'])
def test_everyOtherEnvironment_enforces(cloud, monkeypatch, environment):
    monkeypatch.setenv('ENVIRONMENT', environment)

    response, ran = _post(callerAuth.requirePushCaller)

    _assertRefused(response, ran)


def test_reportMode_logsTheRefusalAndRunsTheRoute(cloud, monkeypatch, caplog):
    monkeypatch.setenv(callerAuth.MODE_ENV, ' Report ')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, _FUTURE_DAY)
    client, ran = _client(callerAuth.requirePushCaller)
    with caplog.at_level(logging.WARNING):
        response = client.post('/handler',
                               base_url=f'https://{HOST}',
                               headers={'User-Agent': 'legacy-caller/1.0'})

    assert response.status_code == 200
    assert ran == [True]
    (record, ) = [r for r in caplog.records
                  if isinstance(r.msg, dict)
                  and r.msg.get('event') == 'callerRejected']
    assert record.msg['mode'] == 'report'
    assert record.msg['reason'] == 'missingToken'
    assert record.msg['userAgent'] == 'legacy-caller/1.0'


@pytest.mark.parametrize('mode', ['', 'off', 'disabled', 'enforce', 'reports'])
def test_anyOtherMode_enforces(cloud, monkeypatch, mode):
    monkeypatch.setenv(callerAuth.MODE_ENV, mode)

    response, ran = _post(callerAuth.requirePushCaller)

    _assertRefused(response, ran)


_FUTURE_DAY = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
_UNTIL_DAY = '2030-06-15'


def _at(monkeypatch, isoInstant):
    monkeypatch.setattr(callerAuth, '_now',
                        lambda: datetime.datetime.fromisoformat(isoInstant))


@pytest.mark.parametrize('now, runs', [
    ('2030-06-15T23:59:59+00:00', True),
    ('2030-06-16T00:00:00+00:00', False),
    ('2030-07-01T00:00:00+00:00', False),
])
def test_reportMode_runsThroughItsUntilDay_thenEnforces(cloud, monkeypatch,
                                                        now, runs):
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, _UNTIL_DAY)
    _at(monkeypatch, now)

    response, ran = _post(callerAuth.requirePushCaller)

    if runs:
        assert response.status_code == 200
        assert ran == [True]
    else:
        _assertRefused(response, ran)


@pytest.mark.parametrize('now, runs', [
    ('2030-06-15T11:59:59+08:00', True),
    ('2030-06-15T12:00:00+08:00', False),
])
def test_reportMode_untilADatetime_endsAtThatInstant(cloud, monkeypatch, now,
                                                    runs):
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, '2030-06-15T04:00:00Z')
    _at(monkeypatch, now)

    response, ran = _post(callerAuth.requirePushCaller)

    assert (response.status_code == 200) is runs
    assert ran == ([True] if runs else [])


@pytest.mark.parametrize('until', [
    None, '', 'soon', '2030-13-01', '15/06/2030', '2030-06-15T04:00:00'
])
def test_reportMode_withoutAReadableUntil_enforces(cloud, monkeypatch, until):
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    if until is not None:
        monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, until)

    response, ran = _post(callerAuth.requirePushCaller)

    _assertRefused(response, ran)


@pytest.mark.parametrize('until, runs', [
    ('2030-06-30', True),
    ('2030-07-01T00:00:00+00:00', True),
    ('2030-07-01', False),
    ('2030-07-01T00:00:01+00:00', False),
    ('9999-12-31', False),
    ('9999-12-31T23:00:00-05:00', False),
])
def test_reportMode_endingMoreThan30DaysAhead_enforces(cloud, monkeypatch,
                                                      until, runs):
    """Report mode cannot be parked: an end more than REPORT_MAX_DAYS ahead
    (or one too far to compute) reads as none."""
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, until)
    _at(monkeypatch, '2030-06-01T00:00:00+00:00')

    response, ran = _post(callerAuth.requirePushCaller)

    assert (response.status_code == 200) is runs
    assert ran == ([True] if runs else [])


def test_loadingTheModule_withAnOverflowingUntil_starts(cloud, monkeypatch,
                                                        caplog):
    import importlib.util
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, '9999-12-31')
    spec = importlib.util.spec_from_file_location('callerAuthAtStartup',
                                                  callerAuth.__file__)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert [r.msg['state'] for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'callerAuthReportMode'
            ] == ['enforcing: no readable CALLER_AUTH_REPORT_UNTIL']


def _startupWarnings(caplog):
    callerAuth.warnIfReportMode()
    return [r for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'callerAuthReportMode']


@pytest.mark.parametrize('until, now, state', [
    (_UNTIL_DAY, '2030-06-01T00:00:00+00:00',
     'refusals are logged, not enforced'),
    (_UNTIL_DAY, '2030-06-16T00:00:00+00:00', 'enforcing: report mode expired'),
    ('soon', '2030-06-01T00:00:00+00:00',
     'enforcing: no readable CALLER_AUTH_REPORT_UNTIL'),
])
def test_reportMode_warnsAtStartup(cloud, monkeypatch, caplog, until, now,
                                   state):
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, until)
    _at(monkeypatch, now)
    with caplog.at_level(logging.WARNING):
        (record, ) = _startupWarnings(caplog)

    assert record.levelno == logging.WARNING
    assert record.msg['state'] == state


def test_loadingTheModuleInReportMode_warns(cloud, monkeypatch, caplog):
    """The warning comes at startup: loading callerAuth logs it."""
    import importlib.util
    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, _FUTURE_DAY)
    spec = importlib.util.spec_from_file_location('callerAuthAtStartup',
                                                  callerAuth.__file__)
    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert [r.msg['state'] for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'callerAuthReportMode'
            ] == ['refusals are logged, not enforced']


def test_enforceMode_warnsNothingAtStartup(cloud, caplog):
    with caplog.at_level(logging.WARNING):
        assert _startupWarnings(caplog) == []


def _verifiedFlag(decorator, token=None):
    app = flask.Flask(__name__)
    seen = []

    @app.route('/handler', methods=['POST'])
    @decorator
    def handler():
        seen.append(callerAuth.callerVerified())
        return {}

    headers = {} if token is None else {'Authorization': f'Bearer {token}'}
    app.test_client().post('/handler', headers=headers,
                           base_url=f'https://{HOST}')
    return seen


def test_callerVerified_onlyForACallerThatPassed(cloud, monkeypatch):
    assert _verifiedFlag(callerAuth.requirePushCaller, _token()) == [True]

    monkeypatch.setenv(callerAuth.MODE_ENV, 'report')
    monkeypatch.setenv(callerAuth.REPORT_UNTIL_ENV, _FUTURE_DAY)
    assert _verifiedFlag(callerAuth.requirePushCaller) == [False]
    assert _verifiedFlag(callerAuth.requirePushCaller,
                         _token(email=OTHER_ACCOUNT)) == [False]

    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    assert _verifiedFlag(callerAuth.requirePushCaller, _token()) == [False]


# ----------------------------- Google's certs ----------------------------- #


class _Clock:

    def __init__(self, monkeypatch):
        self.now = 1_000_000.0
        monkeypatch.setattr(callerAuth, '_monotonic', lambda: self.now)


def test_theCertsAreCachedForAnHour(cloud, monkeypatch):
    clock = _Clock(monkeypatch)
    callerAuth._googleCerts(KEY_ID)
    clock.now += callerAuth.CERTS_MAX_AGE_SECONDS - 1
    callerAuth._googleCerts(KEY_ID)
    assert len(cloud) == 1

    clock.now += 2
    callerAuth._googleCerts(KEY_ID)
    assert len(cloud) == 2


def test_anUnknownKeyId_refetchesAtMostOnceAMinute(cloud, monkeypatch):
    clock = _Clock(monkeypatch)
    callerAuth._googleCerts(KEY_ID)
    for _ in range(3):
        clock.now += 10
        assert 'new-key' not in callerAuth._googleCerts('new-key')
    assert len(cloud) == 1

    clock.now += callerAuth.CERTS_MIN_FETCH_INTERVAL_SECONDS
    callerAuth._googleCerts('new-key')
    assert len(cloud) == 2


def test_aFailedRefetch_keepsTheLastGoodCertsForTheGracePeriod(
        cloud, monkeypatch):
    clock = _Clock(monkeypatch)
    callerAuth._googleCerts(KEY_ID)

    def failing():
        raise OSError('network down')

    monkeypatch.setattr(callerAuth, '_fetchCerts', failing)
    clock.now += callerAuth.CERTS_MAX_AGE_SECONDS + 1
    assert KEY_ID in callerAuth._googleCerts(KEY_ID)

    clock.now += callerAuth.CERTS_GRACE_SECONDS
    with pytest.raises(callerAuth.CallerRejected) as info:
        callerAuth._googleCerts(KEY_ID)
    assert info.value.reason == 'certsUnavailable'


def test_noCertsAtAll_refusesTheCaller(cloud, monkeypatch):

    def failing():
        raise OSError('network down')

    monkeypatch.setattr(callerAuth, '_fetchCerts', failing)

    response, ran = _post(callerAuth.requirePushCaller, _token())

    _assertRefused(response, ran)


@pytest.mark.parametrize('answer', [
    [],
    {},
    {'kid': 42},
    {'kid': 'not a certificate'},
    'text',
])
def test_aCertsAnswerOfTheWrongShape_isRefused(monkeypatch, answer):

    class _Response:

        def raise_for_status(self):
            pass

        def json(self):
            return answer

    monkeypatch.setattr(callerAuth.requests, 'get',
                        lambda *args, **kwargs: _Response())

    with pytest.raises(ValueError):
        callerAuth._fetchCerts()


# ----------------------------- generated code ----------------------------- #


def _creator():
    return AppCreator.AppCreator(apiRequests=None)


def _routedFunctions(code: str, blueprint: str) -> dict:
    """name -> decorator list of every function routed on `blueprint`."""
    routed = {}
    for node in ast.parse(code).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        first = node.decorator_list[0] if node.decorator_list else None
        if (isinstance(first, ast.Call)
                and isinstance(first.func, ast.Attribute)
                and first.func.attr == 'route'
                and isinstance(first.func.value, ast.Name)
                and first.func.value.id == blueprint):
            routed[node.name] = node.decorator_list
    return routed


def _decoratorBelowRoute(decorators) -> str | None:
    if len(decorators) < 2 or not isinstance(decorators[1], ast.Name):
        return None
    return decorators[1].id


def _serviceOnlyRoutes() -> set:
    creator = _creator()
    return {
        name
        for name, func in creator._get_user_defined_methods('app').items()
        if creator._route_meta(func).get('serviceOnly')
    }


def _assertPushHandlersGuarded(code: str):
    handlers = _routedFunctions(code, 'main')
    unguarded = sorted(name for name, decorators in handlers.items()
                       if _decoratorBelowRoute(decorators) != 'requirePushCaller')
    assert not unguarded, (
        f'push handlers without @requirePushCaller under their route: '
        f'{unguarded}')
    return handlers


def _assertServiceRoutesGuarded(code: str):
    routes = _routedFunctions(code, 'app')
    serviceOnly = _serviceOnlyRoutes()
    guarded = {name for name, decorators in routes.items()
               if _decoratorBelowRoute(decorators) == 'requireServiceCaller'}
    assert guarded == serviceOnly & set(routes)
    assert serviceOnly <= set(routes)
    others = sorted(name for name, decorators in routes.items()
                    if any(isinstance(d, ast.Name)
                           and d.id in ('requirePushCaller',
                                        'requireServiceCaller')
                           for d in decorators) and name not in serviceOnly)
    assert not others


def test_everyGeneratedPushHandler_requiresAPushCaller():
    handlers = _assertPushHandlersGuarded(
        _creator()._all_functions_code('appPubSub'))
    assert set(handlers) == set(_creator()._get_user_defined_methods('appPubSub'))


def test_theCommittedAppPubSub_requiresAPushCallerOnEveryHandler():
    code = (AppCreator.SERVER_DIR / 'appPubSub.py').read_text(encoding='utf-8')
    _assertPushHandlersGuarded(code)
    assert 'from callerAuth import requirePushCaller' in code


def test_theCommittedApp_requiresAServiceCallerOnExactlyTheServiceOnlyRoutes():
    code = (AppCreator.SERVER_DIR / 'app.py').read_text(encoding='utf-8')
    _assertServiceRoutesGuarded(code)
    assert 'from callerAuth import requireServiceCaller' in code


def test_theServiceOnlyRoutes_areExactlyTheServiceToServiceActionsRoutes():
    creator = _creator()
    routes = {
        name: func
        for name, func in creator._get_user_defined_methods('app').items()
        if getattr(func, 'httpMethod', None) is not None
    }
    inServiceToService = {
        name for name, func in routes.items()
        if getattr(func, '__module__', '').split('.')[-1]
        == 'ServiceToServiceActions'
    }
    assert _serviceOnlyRoutes() == inServiceToService


# ----------------------- serviceOnly takes no user ------------------------ #

# jwtOptional only where route_config has it.
_HAS_JWT_OPTIONAL = 'jwtOptional' in inspect.signature(route_config).parameters
_SERVICE_ONLY_CONFLICTS = [
    {'serviceOnly': 'yes'},
    {'serviceOnly': 1},
    {'serviceOnly': True, 'jwtRequired': True},
    {'serviceOnly': True, 'createAccessToken': True},
] + ([{'serviceOnly': True, 'jwtOptional': True}] if _HAS_JWT_OPTIONAL else [])


@pytest.mark.parametrize(
    'flags',
    _SERVICE_ONLY_CONFLICTS + [{'serviceOnly': True, 'roleAccess': 'anyRole'}],
    ids=str)
def test_routeConfig_refusesServiceOnlyWithAUserFlag(flags):

    # A login route's name: route_config takes createAccessToken on no other
    def loginWithGoogle():
        return {}

    with pytest.raises(ValueError, match='serviceOnly'):
        route_config(httpMethod='POST', **flags)(loginWithGoogle)


def test_routeConfig_takesAPlainServiceOnlyRoute():

    def fn():
        return {}

    assert route_config(httpMethod='POST', serviceOnly=True)(fn).serviceOnly


@pytest.mark.parametrize('flags', _SERVICE_ONLY_CONFLICTS, ids=str)
def test_appCreator_refusesServiceOnlyWithAUserFlag(flags):
    from conftest import routeMeta
    creator = AppCreator.AppCreator(apiRequests=None)

    with pytest.raises(ValueError, match='serviceOnly'):
        creator._validate_route_meta('fn', routeMeta(**flags), 'ApiRequests')


def test_appCreator_takesAPlainServiceOnlyRoute():
    from conftest import routeMeta
    from objects import NoContentResponse
    # A ResponseModel of its own: routeMeta's default model is a database
    # model in some repos, which _validate_route_meta refuses for any route.
    AppCreator.AppCreator(apiRequests=None)._validate_route_meta(
        'fn', routeMeta(serviceOnly=True, responseModel=NoContentResponse),
        'ApiRequests')


# ------------------------- this repo's accounts -------------------------- #


def _beforeCloudSetup():
    """True only in a create-star-app project whose star-app.json (at the
    repository root, the parent of server/) names no GCP project yet
    (cloud.gcp.projectId), or whose service was torn down (cloud.tornDown):
    cloud-setup writes PUSH_SERVICE_ACCOUNTS[0] once the project exists and
    cloud-teardown empties it again. A repo without star-app.json (one
    deployed before the record existed) or with an unreadable one is
    deployed."""
    import json
    from pathlib import Path
    record = Path(__file__).absolute().parent.parent / 'star-app.json'
    try:
        cloud = json.loads(record.read_text(encoding='utf-8')).get('cloud')
        if cloud is None:
            return True
        if cloud.get('tornDown') is True:
            return True
        return not (cloud.get('gcp') or {}).get('projectId')
    except (OSError, ValueError, AttributeError):
        return False


def test_thisReposAccounts_areTheExpectedOnes():
    assert callerAuth.PUSH_SERVICE_ACCOUNTS == EXPECTED_PUSH_SERVICE_ACCOUNTS
    assert (callerAuth.SERVICE_CALLER_ACCOUNTS ==
            EXPECTED_SERVICE_CALLER_ACCOUNTS)
    if _beforeCloudSetup():
        pytest.skip('star-app.json has no cloud.gcp.projectId yet: '
                    'create-star-app cloud-setup writes the push account')
    assert callerAuth.PUSH_SERVICE_ACCOUNTS, (
        "callerAuth.py's per-repo block lists no push account: every push "
        'is refused outside localdev')

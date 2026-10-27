"""The route that signs a caller in, and the routes that answer without a JWT.

Covers:
  - loginWithGoogle trusts only a VERIFIED Firebase ID token outside
    localdev: no token is refused, the uid (the Users _id), email and name
    come from the token (a raw identity sent next to it is ignored), and the
    token must carry a verified email from a Google sign-in. A token that
    does not verify, or whose audience, issuer or subject is not this
    project's, is refused with a generic message that quotes neither the
    token nor the verifier's error, and only the error's class name is
    logged. A login that trusts the client's identity lets anyone POST any
    uid and get that user's session.
  - An unknown verified uid creates a user with no role; the first user of
    an empty Users collection is made superAdmin, and that is decided from
    one _id, not by reading the whole collection, through an atomic one-time
    claim that a racing second sign-in cannot also win.
  - The raw identity is honoured in localdev only (tests, local sign-in),
    and so are devLogin and devGetUserList.
  - Google's signing certs are cached; a fetch is attempted at most once a
    minute, a failed one keeps the last good certs for a grace period, and
    an answer of the wrong shape is refused. Only an RS256 token with a key
    id the certs hold is decoded.
  - The auth surface is pinned: which routes answer without a JWT, and
    which JWT routes check no role.
  - The Firebase Auth emulator's unsigned (alg "none") tokens sign in only
    in localdev with FIREBASE_AUTH_EMULATOR_HOST on a loopback host, and
    still need this project's audience and issuer, a subject and no expiry;
    a cloud environment refuses them even with the variable set.
"""

import logging
import os

import pytest

import AuthHandler as AuthHandlerModule
from ApiRequests import ApiRequests
from AuthHandler import DEFAULT_FIREBASE_PROJECT_ID, verifyFirebaseIdToken
from conftest import (OTHER_UID, RAW_EMAIL, RAW_UID, TOKEN, TOKEN_EMAIL,
                      TOKEN_UID, apiRoutes, assertExactShape,
                      assertMatchesModel)
from conftest import firebaseClaims as _claims
from mongoDb import db
from objects import UserResponse, DevUserListResponse, FirebaseUserObject
from sharedConstants import FIREBASE_PROJECT_ID

# Routes that answer a caller holding no JWT.
PUBLIC_ROUTES = {
    # Sign-in: a verified Firebase ID token, or a raw identity in localdev
    # only.
    'loginWithGoogle',
    # localdev only (refused everywhere else): the dev user picker and
    # signing in as one of its users.
    'devGetUserList',
    'devLogin',
    # A customer opening a store link: a verified anonymous or phone
    # Firebase token, never a Google one (CustomerActions).
    'startGuestSession',
}

# JWT routes with no roleAccess: any signed-in user may call them. Pinned so a
# new one is a decision, not an accident.
JWT_ROUTES_WITHOUT_ROLE_ACCESS = {
    # Admin side: the caller must be a Users document (requireAdmin) and
    # reaches only the store it owns.
    'getUserStore',
    'createUserStore',
    # The map component pins the admin's store: requireAdmin, and only the
    # store the caller owns.
    'setUserStoreLocation',
    # Customer side: the caller must be a Customers document
    # (requireCustomer).
    'verifyCustomerPhone',
    'listStoreProducts',
    # The admin's products: requireAdmin, and only the products of the
    # store the caller owns (ownerQuery).
    'addStoreProduct',
    'addStoreProducts',
    'updateStoreProduct',
    'listMyStoreProducts',
}


def _storedUser(userId: str):
    return db.read({'_id': userId}, 'Users', findOne=True)


def _hasAnyRole(user: dict) -> bool:
    return any(role['value'] for role in user['roles'])


@pytest.mark.parametrize('environment', ['localprod', 'clouddev', 'cloudprod'])
def test_loginWithGoogle_withoutAToken_isRefusedOutsideLocaldev(
        monkeypatch, anotherUser, environment):
    """Outside localdev a raw uid proves nothing: before this check, anyone
    could POST any uid and get that user's session."""
    monkeypatch.setenv('ENVIRONMENT', environment)

    with pytest.raises(PermissionError, match='Firebase ID token is required'):
        ApiRequests().loginWithGoogle({
            'uid': OTHER_UID,
            'email': RAW_EMAIL,
            'displayName': 'Aa Raw'
        })
    with pytest.raises(PermissionError, match='Firebase ID token is required'):
        ApiRequests().loginWithGoogle({'uid': RAW_UID, 'email': RAW_EMAIL})

    assert _storedUser(RAW_UID) is None


@pytest.mark.parametrize('environment', ['localdev', 'cloudprod'])
def test_loginWithGoogle_signsUpTheTokensAccount(monkeypatch, firebase,
                                                 anotherUser, environment):
    monkeypatch.setenv('ENVIRONMENT', environment)

    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assertMatchesModel(UserResponse, res)
    assert res['_id'] == TOKEN_UID
    assert res['email'] == TOKEN_EMAIL
    assert res['name'] == 'Aa Token User'
    assert not _hasAnyRole(res)
    assert _storedUser(TOKEN_UID)['email'] == TOKEN_EMAIL
    assert firebase['verified'] == [(TOKEN, DEFAULT_FIREBASE_PROJECT_ID)]


def test_loginWithGoogle_signsInTheExistingUserOfTheTokensUid(
        monkeypatch, firebase, anotherUser):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    firebase['claims'] = _claims(sub=OTHER_UID, email='aa-other@test.com')

    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert res['_id'] == OTHER_UID
    assert res['name'] == 'Aa Other'
    assert res['userType'] == 'unauthorized'


def test_loginWithGoogle_ignoresARawIdentityNextToAToken(
        monkeypatch, firebase, anotherUser):
    """The token decides whose account it is, whatever else the client
    sends."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    res = ApiRequests().loginWithGoogle(
        FirebaseUserObject(idToken=TOKEN,
                           uid=OTHER_UID,
                           email=RAW_EMAIL,
                           displayName='Aa Raw'))

    assert res['_id'] == TOKEN_UID
    assert res['email'] == TOKEN_EMAIL
    assert res['name'] == 'Aa Token User'


@pytest.mark.parametrize('claims', [
    _claims(email_verified=False),
    _claims(email_verified='true'),
    {key: value for key, value in _claims().items() if key != 'email_verified'},
    _claims(email=None),
    _claims(firebase={'sign_in_provider': 'password'}),
    _claims(firebase={}),
    {key: value for key, value in _claims().items() if key != 'firebase'},
],
                         ids=[
                             'emailNotVerified', 'emailVerifiedNotTrue',
                             'noEmailVerified', 'noEmail', 'passwordProvider',
                             'noProvider', 'noFirebaseClaim'
                         ])
def test_loginWithGoogle_refusesATokenWithoutAVerifiedGoogleEmail(
        monkeypatch, firebase, anotherUser, claims):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    firebase['claims'] = claims

    with pytest.raises(PermissionError):
        ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert _storedUser(TOKEN_UID) is None


@pytest.mark.parametrize('claims', [
    _claims(aud='another-project'),
    _claims(iss='https://securetoken.google.com/another-project'),
    _claims(iss='https://accounts.google.com'),
    _claims(sub=''),
    _claims(sub=None),
    _claims(sub='x' * 129),
    {key: value for key, value in _claims().items() if key != 'sub'},
],
                         ids=[
                             'otherAudience', 'otherIssuer', 'googleIssuer',
                             'emptySubject', 'nullSubject', 'longSubject',
                             'noSubject'
                         ])
def test_loginWithGoogle_refusesAnotherProjectsOrSubjectlessToken(
        monkeypatch, caplog, firebase, anotherUser, claims):
    """verify_firebase_token checks the signature, expiry and audience;
    the issuer and subject are checked here, as firebase-admin does."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    firebase['claims'] = claims

    with caplog.at_level(logging.WARNING):
        with pytest.raises(PermissionError,
                           match='Sign-in could not be verified'):
            ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert _storedUser(TOKEN_UID) is None
    assert any(
        isinstance(r.msg, dict) and r.msg == {
            'event': 'firebaseIdTokenRejected',
            'reason': 'claims'
        } for r in caplog.records)


def test_loginWithGoogle_refusesATokenThatDoesNotVerify(
        monkeypatch, caplog, firebase, anotherUser):
    """The message is generic: it reaches the client and the logs, so it
    quotes neither the token nor the verifier's error, and that error is
    not chained on. Only the error's class name is logged."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    forged = 'aa-forged-token'

    with caplog.at_level(logging.WARNING):
        with pytest.raises(PermissionError) as excinfo:
            ApiRequests().loginWithGoogle({'idToken': forged})

    message = str(excinfo.value)
    assert message == 'Sign-in could not be verified, please sign in again'
    assert forged not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rejected = [
        r.msg for r in caplog.records if isinstance(r.msg, dict)
        and r.msg.get('event') == 'firebaseIdTokenRejected'
    ]
    assert rejected == [{
        'event': 'firebaseIdTokenRejected',
        'reason': 'ValueError'
    }]
    assert all(forged not in r.getMessage() for r in caplog.records)
    assert _storedUser(TOKEN_UID) is None


def test_loginWithGoogle_firstVerifiedUserIsSuperAdmin(monkeypatch, firebase,
                                                       emptyUsers):
    """Kept as it was, but only through a VERIFIED token: the first user of
    an empty Users collection is the business owner."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert res['_id'] == TOKEN_UID
    assert res['userType'] == 'superAdmin'
    assert all(role['value'] for role in res['roles'])


def test_loginWithGoogle_withoutATokenOnAnEmptyUsersCollection_createsNobody(
        monkeypatch, emptyUsers):
    """The superAdmin first user cannot be claimed with a raw identity."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    with pytest.raises(PermissionError):
        ApiRequests().loginWithGoogle({
            'uid': RAW_UID,
            'email': RAW_EMAIL,
            'displayName': 'Aa Raw'
        })

    assert db.read({}, 'Users') == []


def test_loginWithGoogle_signUpReadsOneUserToDecideTheFirst(
        monkeypatch, firebase, anotherUser):
    """Whether the sign-up is the business owner is decided from one _id,
    not by reading the whole Users collection."""
    reads = []
    realRead = AuthHandlerModule.db.read

    def recordingRead(query, collection, *args, **kwargs):
        reads.append((query, collection, kwargs))
        return realRead(query, collection, *args, **kwargs)

    monkeypatch.setattr(AuthHandlerModule.db, 'read', recordingRead)
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert not _hasAnyRole(res)
    assert ({}, 'Users', {'projection': {'_id': 1}, 'findOne': True}) in reads
    assert not any(query == {} and not kwargs.get('findOne')
                   for query, collection, kwargs in reads
                   if collection == 'Users')


def test_loginWithGoogle_inLocaldevWithoutAToken_usesTheRawIdentity(
        monkeypatch, anotherUser):
    """Signing in locally without a Firebase project, and the tests. The
    client's extra Firebase user keys are dropped."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    res = ApiRequests().loginWithGoogle({
        'uid': RAW_UID,
        'email': RAW_EMAIL,
        'displayName': 'Aa Raw',
        'photoURL': 'https://example.com/photo.png',
        'stsTokenManager': {
            'refreshToken': 'aa-refresh'
        },
    })

    assertMatchesModel(UserResponse, res)
    assert res['_id'] == RAW_UID
    assert res['email'] == RAW_EMAIL
    assert res['name'] == 'Aa Raw'
    assert not _hasAnyRole(res)


@pytest.mark.parametrize('userObject', [{
    'displayName': 'Aa No Email',
    'uid': RAW_UID
}, {
    'displayName': 'Aa No Uid',
    'email': RAW_EMAIL
}],
                         ids=['noEmail', 'noUid'])
def test_loginWithGoogle_inLocaldevWithoutAToken_needsUidAndEmail(
        monkeypatch, anotherUser, userObject):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    with pytest.raises(ValueError, match='uid and email are required'):
        ApiRequests().loginWithGoogle(userObject)

    assert _storedUser(RAW_UID) is None


@pytest.mark.parametrize('envValue, expected', [
    (None, DEFAULT_FIREBASE_PROJECT_ID),
    ('', DEFAULT_FIREBASE_PROJECT_ID),
    ('   ', DEFAULT_FIREBASE_PROJECT_ID),
    (' other-project ', 'other-project'),
])
def test_verifyFirebaseIdToken_checksTheProjectFromTheEnv(
        monkeypatch, envValue, expected):
    """google-auth verifies the audience it is handed, with the cached
    certs: the project from FIREBASE_PROJECT_ID, or the client's own project
    when it is unset or blank. The issuer must name the same project."""
    from google.auth import jwt as google_jwt

    if envValue is None:
        monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    else:
        monkeypatch.setenv('FIREBASE_PROJECT_ID', envValue)
    audiences = []
    certs = {'aa-key': 'aa-cert'}

    def decode(idToken, certs=None, audience=None, **kwargs):
        assert certs == {'aa-key': 'aa-cert'}
        audiences.append(audience)
        return _claims(aud=audience,
                       iss=f'https://securetoken.google.com/{audience}')

    monkeypatch.setattr(google_jwt, 'decode_header',
                        lambda idToken: {'kid': 'aa-key', 'alg': 'RS256'})
    monkeypatch.setattr(AuthHandlerModule, '_firebaseCerts',
                        lambda keyId: certs if keyId == 'aa-key' else {})
    monkeypatch.setattr(google_jwt, 'decode', decode)

    claims = verifyFirebaseIdToken(TOKEN)

    assert audiences == [expected]
    assert claims['uid'] == TOKEN_UID
    assert claims['aud'] == expected


def test_defaultFirebaseProject_isTheClientsProject():
    """The client signs in to this project; a token from any other project
    does not verify. Both read sharedConstants.FIREBASE_PROJECT_ID."""
    assert DEFAULT_FIREBASE_PROJECT_ID == FIREBASE_PROJECT_ID
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                        'client', 'lib', 'firebase.ts')
    with open(path, encoding='utf-8') as f:
        source = f.read()
    assert 'projectId: FIREBASE_PROJECT_ID,' in source
    assert ('import { FIREBASE_PROJECT_ID } from '
            '"@/lib/generated/serverConstants";') in source


def test_verifyFirebaseIdToken_warnsWhileTheProjectIsThePlaceholder(
        monkeypatch, caplog):
    """A new project ships with the placeholder project id: no token
    verifies against it (fail closed), and the log says why."""
    monkeypatch.setattr(AuthHandlerModule, 'DEFAULT_FIREBASE_PROJECT_ID',
                        AuthHandlerModule.FIREBASE_PROJECT_PLACEHOLDER)
    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)

    def decode(idToken, projectId):
        # No real token carries the placeholder as its audience.
        raise ValueError('Token has wrong audience')

    monkeypatch.setattr(AuthHandlerModule, '_decodeFirebaseIdToken', decode)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(PermissionError):
            verifyFirebaseIdToken(TOKEN)

    assert {'event': 'firebaseProjectNotConfigured'} in [
        r.msg for r in caplog.records
    ]

    monkeypatch.setenv('FIREBASE_PROJECT_ID', 'aa-configured-project')
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(PermissionError):
            verifyFirebaseIdToken(TOKEN)
    assert {'event': 'firebaseProjectNotConfigured'} not in [
        r.msg for r in caplog.records
    ]


def test_loginWithGoogle_refusesAnOverlongToken(monkeypatch, firebase,
                                                anotherUser):
    """An ID token is about a kilobyte: a far longer one is refused before
    it reaches the verifier."""
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    with pytest.raises(ValueError, match='^Invalid sign-in request$'):
        ApiRequests().loginWithGoogle({'idToken': 'x' * 5000})
    assert firebase['verified'] == []


def test_signIn_aLostSuperAdminClaim_getsNoRole(monkeypatch, firebase,
                                               emptyUsers):
    """Two first sign-ins race on an empty Users collection: both read it
    empty, but only the one whose insert of the claim document succeeds is
    made superAdmin. Here the other already claimed it."""
    from AuthHandler import BOOTSTRAP_COLLECTION, SUPER_ADMIN_CLAIM_ID
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    db.create({
        '_id': SUPER_ADMIN_CLAIM_ID,
        '_version': 0,
        'userId': 'aa-racing-user'
    }, BOOTSTRAP_COLLECTION)

    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})

    assert res['_id'] == TOKEN_UID
    assert res['userType'] is None
    assert not _hasAnyRole(res)


def test_signIn_theFirstUserClaimsSuperAdminOnce(monkeypatch, firebase,
                                                emptyUsers):
    """The claim records who won it, and outlives an emptied Users: the
    bootstrap never re-opens by itself."""
    from AuthHandler import BOOTSTRAP_COLLECTION, SUPER_ADMIN_CLAIM_ID
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    assert ApiRequests().loginWithGoogle({'idToken': TOKEN
                                          })['userType'] == 'superAdmin'
    claim = db.read({'_id': SUPER_ADMIN_CLAIM_ID},
                    BOOTSTRAP_COLLECTION,
                    findOne=True)
    assert claim['userId'] == TOKEN_UID

    db.delete({'_id': TOKEN_UID}, 'Users')
    assert db.read({}, 'Users') == []
    firebase['claims'] = _claims(sub=OTHER_UID, email='aa-other@test.com')
    res = ApiRequests().loginWithGoogle({'idToken': TOKEN})
    assert res['_id'] == OTHER_UID
    assert not _hasAnyRole(res)


def test_loginRoutes_areTheOnlyRoutesThatMintAToken():
    assert {
        name
        for name, func in apiRoutes().items() if func.createAccessToken
    } == {'loginWithGoogle', 'devLogin', 'startGuestSession'}


@pytest.mark.parametrize('environment', ['localprod', 'clouddev', 'cloudprod'])
def test_devRoutes_areRefusedOutsideLocaldev(monkeypatch, anotherUser,
                                             environment):
    """devGetUserList hands out every user's _id and devLogin signs in as
    any of them without a credential: both answer in localdev only."""
    monkeypatch.setenv('ENVIRONMENT', environment)

    with pytest.raises(PermissionError, match='only available in localdev'):
        ApiRequests().devGetUserList()
    with pytest.raises(PermissionError, match='only available in localdev'):
        ApiRequests().devLogin(OTHER_UID)


def test_devRoutes_inLocaldev_listAndSignInTheUsers(monkeypatch,
                                                    anotherUser):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    res = ApiRequests().devGetUserList()

    assertExactShape(DevUserListResponse, res)
    assert [user['_id'] for user in res['users']] == [OTHER_UID]
    assert ApiRequests().devLogin(OTHER_UID)['_id'] == OTHER_UID
    with pytest.raises(ValueError, match='^User not found$'):
        ApiRequests().devLogin('aa-no-such-user')


def test_authSurface_routesWithoutAJwt():
    """A route added without jwtRequired answers anyone: it must be added
    here on purpose."""
    assert {
        name
        for name, func in apiRoutes().items() if not func.jwtRequired
    } == PUBLIC_ROUTES


def test_authSurface_jwtRoutesWithoutARoleCheck():
    """A JWT route without roleAccess answers every signed-in user: it must
    be added here on purpose."""
    assert {
        name
        for name, func in apiRoutes().items()
        if func.jwtRequired and not func.roleAccess
    } == JWT_ROUTES_WITHOUT_ROLE_ACCESS


# ----------------------------- Firebase cert cache ----------------------------- #


class _CertsResponse:

    def __init__(self, certs, cacheControl):
        self.certs = certs
        self.headers = {'Cache-Control': cacheControl} if cacheControl else {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.certs


@pytest.fixture
def certsServer(monkeypatch):
    """Google's cert endpoint and the clock, faked: `state['certs']` is
    what the next fetch answers, `state['fetches']` the timeouts of the
    fetches so far, `state['now']` the monotonic clock. The cache starts
    empty."""
    import types
    state = {'certs': {'aa-key-1': 'aa-cert-1'}, 'fetches': [], 'now': 1000.0,
             'cacheControl': 'public, max-age=600, must-revalidate',
             'fail': False}

    def get(url, timeout=None):
        assert url == AuthHandlerModule.FIREBASE_CERTS_URL
        state['fetches'].append(timeout)
        if state['fail']:
            raise ConnectionError('Google is unreachable')
        certs = state['certs']
        return _CertsResponse(
            dict(certs) if isinstance(certs, dict) else certs,
            state['cacheControl'])

    monkeypatch.setattr(AuthHandlerModule, 'requests',
                        types.SimpleNamespace(get=get))
    monkeypatch.setattr(AuthHandlerModule, 'time',
                        types.SimpleNamespace(monotonic=lambda: state['now']))
    monkeypatch.setitem(AuthHandlerModule._firebaseCertsCache, 'certs', None)
    monkeypatch.setitem(AuthHandlerModule._firebaseCertsCache, 'expiresAt',
                        0.0)
    monkeypatch.setitem(AuthHandlerModule._firebaseCertsCache, 'fetchedAt',
                        0.0)
    monkeypatch.setitem(AuthHandlerModule._firebaseCertsCache, 'attemptedAt',
                        None)
    return state


def test_firebaseCerts_areFetchedOnceUntilTheyExpire(certsServer):
    """Fetched with a 10 s timeout, then served from the cache for the
    response's max-age; fetched again once that has passed."""
    certs = AuthHandlerModule._firebaseCerts('aa-key-1')
    certsServer['now'] += 599
    assert AuthHandlerModule._firebaseCerts('aa-key-1') == certs
    assert certsServer['fetches'] == [10]

    certsServer['now'] += 1
    AuthHandlerModule._firebaseCerts('aa-key-1')
    assert len(certsServer['fetches']) == 2


def test_firebaseCerts_defaultToAnHourWithoutAMaxAge(certsServer):
    certsServer['cacheControl'] = None
    AuthHandlerModule._firebaseCerts('aa-key-1')
    certsServer['now'] += 3599
    AuthHandlerModule._firebaseCerts('aa-key-1')
    assert len(certsServer['fetches']) == 1
    certsServer['now'] += 1
    AuthHandlerModule._firebaseCerts('aa-key-1')
    assert len(certsServer['fetches']) == 2


def test_firebaseCerts_refetchAnUnknownKeyAtMostOncePerMinute(certsServer):
    """Google rotated its keys: a token signed with a key the cache lacks
    refetches them. A forged key id cannot make every attempt a fetch."""
    AuthHandlerModule._firebaseCerts('aa-key-1')
    certsServer['certs'] = {'aa-key-2': 'aa-cert-2'}

    certsServer['now'] += 30
    assert 'aa-key-2' not in AuthHandlerModule._firebaseCerts('aa-key-2')
    assert len(certsServer['fetches']) == 1

    certsServer['now'] += 30
    assert AuthHandlerModule._firebaseCerts('aa-key-2') == {
        'aa-key-2': 'aa-cert-2'
    }
    assert len(certsServer['fetches']) == 2


def test_firebaseCerts_aFailedFetchKeepsTheLastGoodCertsForAGracePeriod(
        certsServer, caplog):
    """Google down: the expired certs keep verifying for the grace period
    (a failed attempt is retried at most once a minute, and logged by
    class name), then nothing does."""
    certs = AuthHandlerModule._firebaseCerts('aa-key-1')
    certsServer['fail'] = True

    certsServer['now'] += 600
    with caplog.at_level(logging.WARNING):
        assert AuthHandlerModule._firebaseCerts('aa-key-1') == certs
    assert {
        'event': 'firebaseCertsFetchFailed',
        'reason': 'ConnectionError'
    } in [r.msg for r in caplog.records]
    certsServer['now'] += 30
    assert AuthHandlerModule._firebaseCerts('aa-key-1') == certs
    assert len(certsServer['fetches']) == 2

    certsServer['now'] = (
        AuthHandlerModule._firebaseCertsCache['expiresAt'] +
        AuthHandlerModule.FIREBASE_CERTS_STALE_GRACE_SECONDS)
    with pytest.raises(RuntimeError, match='certs are unavailable'):
        AuthHandlerModule._firebaseCerts('aa-key-1')

    certsServer['fail'] = False
    certsServer['now'] += 60
    assert AuthHandlerModule._firebaseCerts('aa-key-1') == certs


@pytest.mark.parametrize('answer', [{}, [], {'aa-key-1': 7}, 'certs'],
                         ids=['empty', 'list', 'notPem', 'string'])
def test_firebaseCerts_refuseAnAnswerOfTheWrongShape(certsServer, answer):
    certsServer['certs'] = answer

    with pytest.raises(RuntimeError, match='certs are unavailable'):
        AuthHandlerModule._firebaseCerts('aa-key-1')
    assert AuthHandlerModule._firebaseCertsCache['certs'] is None


@pytest.mark.parametrize('header', [
    {'kid': 'aa-key-1', 'alg': 'HS256'},
    {'kid': 'aa-key-1', 'alg': 'none'},
    {'alg': 'RS256'},
    {'kid': 7, 'alg': 'RS256'},
    {'kid': 'aa-unknown-key', 'alg': 'RS256'},
], ids=['hs256', 'none', 'noKid', 'intKid', 'unknownKid'])
def test_decodeFirebaseIdToken_refusesAnUnexpectedHeader(
        certsServer, monkeypatch, header):
    """Only an RS256 token whose key id the certs hold is decoded."""
    from google.auth import jwt as google_jwt
    monkeypatch.setattr(google_jwt, 'decode_header', lambda idToken: header)
    monkeypatch.setattr(google_jwt, 'decode', lambda *a, **k: _claims())

    with pytest.raises(ValueError):
        AuthHandlerModule._decodeFirebaseIdToken(TOKEN, 'aa-project')


def test_decodeFirebaseIdToken_verifiesWithTheCachedCerts(
        certsServer, monkeypatch):
    """The token's key id picks the certs; google-auth's jwt.decode checks
    the signature, expiry and audience, as verify_firebase_token did."""
    from google.auth import jwt as google_jwt
    decoded = []

    def decode(idToken, certs=None, audience=None, **kwargs):
        decoded.append((idToken, certs, audience, kwargs))
        return {'sub': TOKEN_UID}

    monkeypatch.setattr(google_jwt, 'decode_header',
                        lambda idToken: {'kid': 'aa-key-1', 'alg': 'RS256'})
    monkeypatch.setattr(google_jwt, 'decode', decode)

    for _ in range(2):
        assert AuthHandlerModule._decodeFirebaseIdToken(
            TOKEN, 'aa-project') == {'sub': TOKEN_UID}

    assert decoded == [(TOKEN, {'aa-key-1': 'aa-cert-1'}, 'aa-project', {})] * 2
    assert len(certsServer['fetches']) == 1


# ----------------------------- A malformed sign-in body ----------------------------- #


@pytest.mark.parametrize('firebaseUserObject', [
    {'idToken': ['aa-token-fragment']},
    {'uid': {'aa-token-fragment': 1}},
    'aa-token-fragment',
])
def test_loginWithGoogle_refusesAMalformedBodyWithoutQuotingIt(
        firebaseUserObject):
    """pydantic's message quotes the input it rejects (token fragments):
    the refusal is generic."""
    with pytest.raises(ValueError, match='^Invalid sign-in request$') as e:
        ApiRequests().loginWithGoogle(firebaseUserObject)
    assert 'aa-token-fragment' not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


# ----------------------- The Firebase Auth emulator (localdev) ----------------------- #
# The emulator's ID tokens are unsigned (alg "none"): anyone can write one.
# AuthHandler accepts one only in localdev with FIREBASE_AUTH_EMULATOR_HOST on
# a loopback host, and still checks its audience, issuer, subject and expiry.


def _segment(value: dict) -> str:
    import base64
    import json
    return base64.urlsafe_b64encode(
        json.dumps(value).encode('utf-8')).rstrip(b'=').decode('ascii')


def _emulatorToken(signature: str = '', **overrides) -> str:
    """What the Auth emulator issues for a Google sign-in: header.payload.
    with no signature."""
    import time
    now = int(time.time())
    claims = _claims(iat=now, exp=now + 3600, auth_time=now)
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    return (f"{_segment({'alg': 'none', 'typ': 'JWT'})}."
            f'{_segment(claims)}.{signature}')


@pytest.fixture
def emulatorMode(monkeypatch):
    """localdev with the Auth emulator on 127.0.0.1; Google's verifier must
    not be reached by an unsigned token."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setenv('FIREBASE_AUTH_EMULATOR_HOST', '127.0.0.1:9099')
    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    signed = []

    def decode(idToken, projectId):
        signed.append(idToken)
        raise ValueError('not a signed token')

    monkeypatch.setattr(AuthHandlerModule, '_decodeFirebaseIdToken', decode)
    return signed


@pytest.mark.parametrize('host', ['127.0.0.1:9099', 'localhost:9099',
                                  ' 127.0.0.1:9199 '])
def test_emulatorToken_isAcceptedInLocaldevWithALoopbackEmulatorHost(
        monkeypatch, emulatorMode, host):
    monkeypatch.setenv('FIREBASE_AUTH_EMULATOR_HOST', host)

    claims = verifyFirebaseIdToken(_emulatorToken())

    assert claims['uid'] == TOKEN_UID
    assert claims['aud'] == DEFAULT_FIREBASE_PROJECT_ID
    assert emulatorMode == []


def test_emulatorToken_signsUpThroughLoginWithGoogle(emulatorMode,
                                                     anotherUser):
    res = ApiRequests().loginWithGoogle({'idToken': _emulatorToken()})

    assertMatchesModel(UserResponse, res)
    assert res['_id'] == TOKEN_UID
    assert res['email'] == TOKEN_EMAIL
    assert not _hasAnyRole(res)


@pytest.mark.parametrize('environment', ['clouddev', 'cloudprod', 'localprod'])
def test_emulatorToken_isRefusedOutsideLocaldevEvenWithTheEmulatorHostSet(
        monkeypatch, emulatorMode, environment):
    """A deployed service with FIREBASE_AUTH_EMULATOR_HOST set by mistake
    must not let anyone sign in as anyone."""
    monkeypatch.setenv('ENVIRONMENT', environment)

    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(_emulatorToken())
    assert not AuthHandlerModule.emulatorTokensAccepted()


@pytest.mark.parametrize('host', [
    '10.0.0.5:9099', '0.0.0.0:9099', 'example.com:9099',
    '127.0.0.1.example.com:9099', 'localhost.example.com:9099',
    '127.0.0.1', 'localhost', 'http://127.0.0.1:9099', '127.0.0.1:9099/x',
    '[::1]:9099', ''
])
def test_emulatorToken_isRefusedWithANonLoopbackEmulatorHost(
        monkeypatch, emulatorMode, host):
    monkeypatch.setenv('FIREBASE_AUTH_EMULATOR_HOST', host)

    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(_emulatorToken())


def test_emulatorToken_isRefusedWithoutTheEmulatorHost(monkeypatch,
                                                       emulatorMode):
    monkeypatch.delenv('FIREBASE_AUTH_EMULATOR_HOST', raising=False)

    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(_emulatorToken())
    # It went to Google's verifier, which refuses alg "none".
    assert len(emulatorMode) == 1


@pytest.mark.parametrize('overrides', [
    {'aud': 'aa-other-project'},
    {'iss': 'https://securetoken.google.com/aa-other-project'},
    {'iss': None},
    {'sub': ''},
    {'sub': None},
    {'sub': 'x' * 129},
    {'exp': 1},
    {'exp': None},
    {'exp': True},
    {'iat': 4102444800},
    {'iat': None},
], ids=['aud', 'iss', 'noIss', 'emptySub', 'noSub', 'longSub', 'expired',
        'noExp', 'boolExp', 'futureIat', 'noIat'])
def test_emulatorToken_stillNeedsThisProjectASubjectAndNoExpiry(
        emulatorMode, overrides):
    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(_emulatorToken(**overrides))


def test_emulatorToken_withASignature_isNotTreatedAsUnsigned(emulatorMode):
    """header.payload.signature with alg none goes to Google's verifier
    (which refuses it), never to the emulator path."""
    token = _emulatorToken(signature='aa-signature')

    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(token)
    assert emulatorMode == [token]


def test_emulatorMode_keepsVerifyingSignedTokens(monkeypatch, firebase):
    """In emulator mode a real (signed) token is verified as always."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setenv('FIREBASE_AUTH_EMULATOR_HOST', '127.0.0.1:9099')

    assert verifyFirebaseIdToken(TOKEN)['uid'] == TOKEN_UID
    assert firebase['verified'] == [(TOKEN, DEFAULT_FIREBASE_PROJECT_ID)]
    with pytest.raises(PermissionError):
        verifyFirebaseIdToken('aa-not-a-token')


def test_emulatorMode_isLoggedAtStartUp(monkeypatch, caplog):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    monkeypatch.setenv('FIREBASE_AUTH_EMULATOR_HOST', '127.0.0.1:9099')
    with caplog.at_level(logging.WARNING):
        AuthHandlerModule._warnAboutEmulatorMode()
    assert [r.msg['event'] for r in caplog.records] == [
        'firebaseAuthEmulatorMode']

    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        AuthHandlerModule._warnAboutEmulatorMode()
    assert [r.msg['event'] for r in caplog.records] == [
        'firebaseAuthEmulatorHostIgnored']

    monkeypatch.delenv('FIREBASE_AUTH_EMULATOR_HOST')
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        AuthHandlerModule._warnAboutEmulatorMode()
    assert caplog.records == []


def test_demoProject_warnsOutsideEmulatorModeOnly(monkeypatch, caplog,
                                                  emulatorMode):
    """A demo- project exists only in the emulator: anywhere else no token
    verifies against it, and the log says why."""
    monkeypatch.setenv('FIREBASE_PROJECT_ID', 'demo-aa-project')

    with caplog.at_level(logging.WARNING):
        verifyFirebaseIdToken(_emulatorToken(
            aud='demo-aa-project',
            iss='https://securetoken.google.com/demo-aa-project'))
    assert {'event': 'firebaseProjectNotConfigured'} not in [
        r.msg for r in caplog.records
    ]

    monkeypatch.delenv('FIREBASE_AUTH_EMULATOR_HOST')
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(PermissionError):
            verifyFirebaseIdToken(TOKEN)
    assert {'event': 'firebaseProjectNotConfigured'} in [
        r.msg for r in caplog.records
    ]


@pytest.mark.parametrize('payload', [
    '{"exp": Infinity, "iat": 1}',
    '{"exp": 4102444800, "iat": NaN}',
    '{"exp": 4102444800, "iat": -Infinity}',
], ids=['infiniteExp', 'nanIat', 'negativeInfiniteIat'])
def test_emulatorToken_refusesNonFiniteTimes(emulatorMode, payload):
    """json.loads takes Infinity and NaN; a token carrying one must not
    pass the expiry and issue-time checks."""
    import base64
    claims = payload[:-1] + (
        f', "aud": "{DEFAULT_FIREBASE_PROJECT_ID}", "iss": '
        f'"https://securetoken.google.com/{DEFAULT_FIREBASE_PROJECT_ID}", '
        f'"sub": "{TOKEN_UID}"}}')
    body = base64.urlsafe_b64encode(claims.encode()).rstrip(b'=').decode()
    token = f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{body}."

    with pytest.raises(PermissionError):
        verifyFirebaseIdToken(token)
    assert not AuthHandlerModule._isTimestamp(float('inf'))
    assert not AuthHandlerModule._isTimestamp(float('nan'))

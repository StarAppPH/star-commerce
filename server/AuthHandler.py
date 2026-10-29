"""Sign-in: verifying the Firebase ID token the client signs in with, and
finding or creating the user it proves.

loginWithGoogle (ApiRequests.py) trusts only a VERIFIED token outside
localdev. The client sends `{idToken}`; the uid (the Users _id), email and
name come from the token's claims, never from what the client says.
"""
import base64
import binascii
import datetime
import json
import logging
import math
import os
import re
import threading
import time

import requests
from pymongo.errors import DuplicateKeyError

from AppConfig import Environment
from actions.StoreActions import createEmptyStore
from mongoDb import db
from objects import User
from roles import AllRoles, UserRoles
from sharedConstants import FIREBASE_PROJECT_ID

# The Firebase project the client signs in with: sharedConstants'
# FIREBASE_PROJECT_ID, which client/lib/firebase.ts reads too
# (test_auth_surface.py checks both). Set the FIREBASE_PROJECT_ID environment
# variable to override it per environment.
DEFAULT_FIREBASE_PROJECT_ID = FIREBASE_PROJECT_ID
# The value projects before create-star-app 1.38 shipped with: no token
# verifies against it, so sign-in fails closed until it is set (and a warning
# says why).
FIREBASE_PROJECT_PLACEHOLDER = 'YOUR_PROJECT'
# A new project ships with a "demo-" project id, which exists only in the
# local Firebase Auth emulator: outside emulator mode no token verifies
# against it either.
DEMO_PROJECT_PREFIX = 'demo-'

# The Firebase Auth emulator (create-star-app dev) issues UNSIGNED ID tokens
# (alg "none"): anyone can write one for any uid. One is accepted only while
# ALL of these hold, read again on every sign-in:
#   - ENVIRONMENT is localdev (AppConfig.Environment); clouddev, cloudprod
#     and localprod refuse them even with the variable below set;
#   - FIREBASE_AUTH_EMULATOR_HOST is a loopback host (127.0.0.1 or
#     localhost) and a port: the emulator runs on this computer;
#   - the token's header is alg "none" with an empty signature, its audience
#     and issuer name this project (FIREBASE_PROJECT_ID, the same project a
#     real token must name), its subject is set and it has not expired.
# A signed (RS256) token is verified against Google's keys as always.
EMULATOR_HOST_VARIABLE = 'FIREBASE_AUTH_EMULATOR_HOST'
EMULATOR_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost'})
_EMULATOR_HOST_RE = re.compile(r'^(?P<host>[A-Za-z0-9.]+):(?P<port>[0-9]{1,5})$')
# How far in the future an emulator token's iat may be (clock skew).
EMULATOR_CLOCK_SKEW_SECONDS = 300

# The collection and document of the one-time superAdmin claim: the first
# sign-in on an empty Users collection inserts it, and only the sign-in whose
# insert succeeds becomes superAdmin. The unique _id makes the claim atomic,
# so two first sign-ins racing each other cannot both win.
BOOTSTRAP_COLLECTION = 'Bootstrap'
SUPER_ADMIN_CLAIM_ID = 'superAdmin'


def _firebaseProjectId() -> str:
    """FIREBASE_PROJECT_ID, or the default when it is unset or blank."""
    return ((os.getenv('FIREBASE_PROJECT_ID') or '').strip()
            or DEFAULT_FIREBASE_PROJECT_ID)


def _isLocalDevEnvironment() -> bool:
    try:
        return Environment().getisLocalDevEnvironment()
    except Exception:
        return False


def _emulatorHostIsLoopback() -> bool:
    match = _EMULATOR_HOST_RE.match(
        (os.getenv(EMULATOR_HOST_VARIABLE) or '').strip())
    return bool(match) and match.group('host') in EMULATOR_LOOPBACK_HOSTS


def emulatorTokensAccepted() -> bool:
    """Whether an unsigned Auth emulator token may sign in: localdev, with
    FIREBASE_AUTH_EMULATOR_HOST on a loopback host. Anything else (a cloud
    environment, localprod, no variable, another host) refuses them."""
    return _isLocalDevEnvironment() and _emulatorHostIsLoopback()


def _warnAboutEmulatorMode() -> None:
    """Once, at start-up: say that unsigned emulator tokens are accepted, or
    that the variable is set where it is ignored."""
    if not os.getenv(EMULATOR_HOST_VARIABLE):
        return
    if emulatorTokensAccepted():
        logging.warning({
            'event': 'firebaseAuthEmulatorMode',
            'detail': 'unsigned Firebase Auth emulator tokens are accepted '
                      '(localdev only)'
        })
    else:
        logging.warning({
            'event': 'firebaseAuthEmulatorHostIgnored',
            'detail': f'{EMULATOR_HOST_VARIABLE} is set but ignored: '
                      'emulator tokens need localdev and a loopback host'
        })


def _projectNotConfigured(projectId: str) -> bool:
    """No token can verify against this project: the old placeholder, or a
    demo- project outside emulator mode."""
    return (projectId == FIREBASE_PROJECT_PLACEHOLDER
            or (projectId.startswith(DEMO_PROJECT_PREFIX)
                and not emulatorTokensAccepted()))


def _refuseJsonConstant(name: str):
    raise ValueError(f'non-standard JSON constant {name}')


def _base64urlJson(segment: str):
    """A base64url JSON segment; NaN, Infinity and -Infinity (which
    json.loads takes by default) are refused."""
    padded = segment + '=' * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode('ascii')),
                      parse_constant=_refuseJsonConstant)


def _isUnsignedToken(idToken: str) -> bool:
    """The token is `header.payload.` with a header of alg "none" (what the
    Auth emulator issues). Anything unreadable is not."""
    parts = idToken.split('.') if isinstance(idToken, str) else []
    if len(parts) != 3 or parts[2] != '':
        return False
    try:
        header = _base64urlJson(parts[0])
    except (ValueError, binascii.Error, UnicodeError):
        return False
    return isinstance(header, dict) and header.get('alg') == 'none'


def _isTimestamp(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _decodeEmulatorIdToken(idToken: str, projectId: str) -> dict:
    """The claims of an unsigned Auth emulator token once its audience
    (`projectId`), expiry and issue time check out. Raises unless emulator
    tokens are accepted here (emulatorTokensAccepted); the caller checks the
    issuer and subject as for a real token."""
    if not emulatorTokensAccepted():
        raise PermissionError('emulator tokens are not accepted here')
    claims = _base64urlJson(idToken.split('.')[1])
    if not isinstance(claims, dict):
        raise ValueError('unexpected emulator token payload')
    now = time.time()
    if not _isTimestamp(claims.get('exp')) or claims['exp'] <= now:
        raise ValueError('emulator token expired')
    if (not _isTimestamp(claims.get('iat'))
            or claims['iat'] > now + EMULATOR_CLOCK_SKEW_SECONDS):
        raise ValueError('emulator token issued in the future')
    if claims.get('aud') != projectId:
        raise ValueError('emulator token has the wrong audience')
    return claims


# Google's public keys for Firebase ID tokens (what google-auth's
# verify_firebase_token fetches on every call). They rotate about daily and
# are served with a Cache-Control max-age, so they are cached for that long
# (an hour when the header gives none), and fetched with a short timeout: a
# slow Google endpoint must not hold a sign-in for the requests default.
FIREBASE_CERTS_URL = ('https://www.googleapis.com/robot/v1/metadata/x509/'
                      'securetoken@system.gserviceaccount.com')
FIREBASE_CERTS_TIMEOUT_SECONDS = 10
FIREBASE_CERTS_DEFAULT_TTL_SECONDS = 3600
# A fetch (refreshing expired certs, or a key id the cache does not hold,
# which Google rotating its keys causes) is ATTEMPTED at most this often,
# whether the last attempt succeeded or not: a forged key id, or Google being
# down, must not turn every sign-in attempt into a fetch.
FIREBASE_CERTS_MIN_REFETCH_SECONDS = 60
# While Google is unreachable, certs that expired up to this long ago keep
# verifying (Google keeps a rotated key valid well past its max-age), so an
# outage does not stop every sign-in at once.
FIREBASE_CERTS_STALE_GRACE_SECONDS = 6 * 3600
_MAX_AGE_RE = re.compile(r'max-age=(\d+)')

_firebaseCertsLock = threading.Lock()
_firebaseCertsCache = {
    'certs': None,
    'expiresAt': 0.0,
    'fetchedAt': 0.0,
    'attemptedAt': None
}


def _fetchFirebaseCerts() -> tuple[dict, float]:
    """Google's current Firebase signing certs (key id -> PEM) and how many
    seconds they may be cached for. Raises on an answer that is not a
    non-empty map of key ids to certificates."""
    response = requests.get(FIREBASE_CERTS_URL,
                            timeout=FIREBASE_CERTS_TIMEOUT_SECONDS)
    response.raise_for_status()
    certs = response.json()
    if not (isinstance(certs, dict) and certs and all(
            isinstance(keyId, str) and isinstance(cert, str)
            for keyId, cert in certs.items())):
        raise ValueError('unexpected Firebase certs response')
    maxAge = _MAX_AGE_RE.search(response.headers.get('Cache-Control', ''))
    ttl = (int(maxAge.group(1))
           if maxAge else FIREBASE_CERTS_DEFAULT_TTL_SECONDS)
    return certs, ttl


def _firebaseCerts(keyId: str | None) -> dict:
    """The cached certs. A fetch is attempted once they expire, or when
    `keyId` is not among them, but at most once per
    FIREBASE_CERTS_MIN_REFETCH_SECONDS. A failed fetch keeps the last good
    certs for up to FIREBASE_CERTS_STALE_GRACE_SECONDS past their expiry;
    with none left, raises."""
    with _firebaseCertsLock:
        now = time.monotonic()
        cache = _firebaseCertsCache
        stale = cache['certs'] is None or now >= cache['expiresAt']
        unknownKey = (cache['certs'] is not None
                      and keyId not in cache['certs'])
        mayAttempt = (cache['attemptedAt'] is None
                      or now - cache['attemptedAt'] >=
                      FIREBASE_CERTS_MIN_REFETCH_SECONDS)
        if (stale or unknownKey) and mayAttempt:
            cache['attemptedAt'] = now
            try:
                certs, ttl = _fetchFirebaseCerts()
                cache.update(certs=certs, expiresAt=now + ttl, fetchedAt=now)
            except Exception as e:
                logging.warning({
                    'event': 'firebaseCertsFetchFailed',
                    'reason': type(e).__name__
                })
        if (cache['certs'] is None or now >=
                cache['expiresAt'] + FIREBASE_CERTS_STALE_GRACE_SECONDS):
            raise RuntimeError('Firebase signing certs are unavailable')
        return cache['certs']


def _decodeFirebaseIdToken(idToken: str, projectId: str) -> dict:
    """The claims of `idToken` once its signature (Google's securetoken
    keys, cached), expiry and audience (`projectId`) verify. Raises on
    anything else, including a header that is not RS256 with a key id.
    This is google-auth's verify_firebase_token with the certs cached:
    google-auth is what firebase-admin verifies with, used directly so the
    server needs no firebase-admin."""
    from google.auth import jwt as google_jwt

    header = google_jwt.decode_header(idToken)
    keyId = header.get('kid')
    if header.get('alg') != 'RS256' or not isinstance(keyId, str):
        raise ValueError('not an RS256 token with a key id')
    certs = _firebaseCerts(keyId)
    if keyId not in certs:
        raise ValueError('unknown key id')
    return google_jwt.decode(idToken,
                             certs={keyId: certs[keyId]},
                             audience=projectId)


def verifyFirebaseIdToken(idToken: str) -> dict:
    """The decoded claims of a Firebase ID token the client signed in with,
    with `uid` set to the token's subject (as firebase-admin's
    verify_id_token returns them).

    Raises PermissionError on anything that does not verify: a forged or
    expired token, another project's token (audience or issuer), a token
    with no subject, or Google's keys being unreachable. The message is
    generic on purpose: it reaches the client and the logs, and neither the
    token nor the verifier's exception text may (the original exception is
    not chained on either). Only the exception's class name is logged."""
    projectId = _firebaseProjectId()
    if _projectNotConfigured(projectId):
        # Fail closed (no token carries this audience), but say why.
        logging.warning({'event': 'firebaseProjectNotConfigured'})
    claims = None
    try:
        if emulatorTokensAccepted() and _isUnsignedToken(idToken):
            claims = _decodeEmulatorIdToken(idToken, projectId)
        else:
            claims = _decodeFirebaseIdToken(idToken, projectId)
    except Exception as e:
        logging.warning({
            'event': 'firebaseIdTokenRejected',
            'reason': type(e).__name__
        })
    # verify_firebase_token checks the audience only; firebase-admin also
    # requires this issuer and a non-empty subject of at most 128 characters.
    if (isinstance(claims, dict)
            and claims.get('aud') == projectId
            and claims.get('iss') == f'https://securetoken.google.com/{projectId}'
            and isinstance(claims.get('sub'), str)
            and 0 < len(claims['sub']) <= 128):
        return {**claims, 'uid': claims['sub']}
    if claims is not None:
        logging.warning({
            'event': 'firebaseIdTokenRejected',
            'reason': 'claims'
        })
    raise PermissionError('Sign-in could not be verified, please sign in again')


_warnAboutEmulatorMode()


def _holdsSuperAdminClaim(firebaseUserId: str) -> bool:
    """Whether the superAdmin claim document names `firebaseUserId`: it
    won the claim on an earlier sign-in whose Users insert then failed."""
    claim = db.read({'_id': SUPER_ADMIN_CLAIM_ID}, BOOTSTRAP_COLLECTION,
                    projection={'userId': 1}, findOne=True)
    return claim is not None and claim.get('userId') == firebaseUserId


def _claimSuperAdmin(firebaseUserId: str) -> bool:
    """Whether `firebaseUserId` wins the one-time superAdmin claim: True
    for the one sign-in whose insert of the claim document succeeds, or
    that already holds it (_holdsSuperAdminClaim: the claim is written
    outside the sign-up's transaction, so a failed sign-up keeps it). The
    claim outlives the Users it was made for, so emptying Users later never
    re-opens it (delete the claim document on purpose to re-run the
    bootstrap)."""
    try:
        db.create(
            {
                '_id': SUPER_ADMIN_CLAIM_ID,
                '_version': 0,
                'userId': firebaseUserId,
                'claimedAt': datetime.datetime.now(tz=datetime.timezone.utc),
            }, BOOTSTRAP_COLLECTION)
    except DuplicateKeyError:
        return _holdsSuperAdminClaim(firebaseUserId)
    return True


def signInIdentity(firebaseUserId: str, email: str, displayName: str) -> dict:
    """The stored Users document of `firebaseUserId` (the Users _id),
    created on first sign-in. A new user gets no role, except the first
    user of an empty Users collection, who is made superAdmin (the business
    owner) when it wins the atomic bootstrap claim (_claimSuperAdmin).
    Whether any user exists is decided from one _id, not by reading the
    whole collection.

    A new user is an admin, so it also gets its store, empty (not set up:
    createUserStore fills it in), and the store's policy, every answer
    unset: one transaction writes all three. On a database without
    transactions a sign-in that stops between the writes leaves a user
    without them, which createUserStore makes up for.

    The identity must already be trusted: a verified token's claims, or the
    raw identity loginWithGoogle accepts in localdev only."""
    stored = db.read({'_id': firebaseUserId}, 'Users', findOne=True)
    if stored is not None:
        return stored
    firstUser = db.read({}, 'Users', projection={'_id': 1},
                        findOne=True) is None
    user = User(_id=firebaseUserId,
                name=displayName or email,
                email=email,
                createdAt=datetime.datetime.now(tz=datetime.timezone.utc),
                roles=UserRoles(AllRoles().getAllRoles()),
                _version=0)
    # A retry after a failed sign-up still holds the claim it won, even
    # once another user signed up in between (Users no longer empty).
    if (_claimSuperAdmin(firebaseUserId)
            if firstUser else _holdsSuperAdminClaim(firebaseUserId)):
        user.setRole('superAdmin')

    def createAll(session):
        created = db.create(user.model_dump(by_alias=True), 'Users',
                            session=session)
        createEmptyStore(firebaseUserId, user.createdAt, session)
        return created

    return db.createTransaction(createAll)

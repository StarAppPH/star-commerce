"""photoStorage.py — product photos in Firebase Storage (hand-written).

The bucket is the Firebase project's: FIREBASE_STORAGE_BUCKET, else
<FIREBASE_PROJECT_ID>.firebasestorage.app. In localdev every call goes to
the local Storage emulator, STORAGE_EMULATOR_HOST or
FIREBASE_STORAGE_EMULATOR_HOST (create-star-app dev sets the second) on a
loopback host, and without one it is a 503: localdev never reaches a real
bucket. Every other environment ignores both.

- objectPathFromUrl(): the object path of a Firebase download URL of our
  bucket (https://firebasestorage.googleapis.com/v0/b/<bucket>/o/<path>,
  plus the emulator's http://<host>/v0/b/... in localdev only). Any other
  scheme, host or bucket is refused.
- readOwnImages(): JPEGs or PNGs of at most MAX_IMAGE_BYTES each (and
  PHOTO_REFERENCES_MAX_TOTAL_BYTES together) under the caller's own
  users/<uid>/ folder, read by bucket and path through the
  Storage SDK. Nothing is ever fetched by URL, so no request can make the
  server fetch an address of its choosing. HEIC/HEIF is refused by name
  and by type.
- saveStorePhoto(): writes stores/<storeId>/photos/<photoId>.jpg, which
  storage.rules lets anyone get (never list) and only the server (admin
  credentials) write, and returns its download URL.

A refusal names the field (imageUrls[0]), never a URL or a path. The client
is one per process, built on first use under a lock (it is no Mongo client:
the connection budget is unchanged). localdev never builds one with real
credentials: without a loopback emulator, every call is a 503. A Storage failure is a 503
("Photo storage is unavailable right now"), logged by class only.
"""

import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from AppConfig import Environment
from services.__request import ServiceUnavailable

BUCKET_VARIABLE = 'FIREBASE_STORAGE_BUCKET'
EMULATOR_VARIABLES = ('STORAGE_EMULATOR_HOST', 'FIREBASE_STORAGE_EMULATOR_HOST')
# The emulator only on this machine: 127.0.0.1, localhost or [::1].
EMULATOR_LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
EMULATOR_HOST_PATTERN = re.compile(
    r'(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})')
DOWNLOAD_HOST = 'firebasestorage.googleapis.com'
STORAGE_API_ENDPOINT = 'https://storage.googleapis.com'
# /v0/b/<bucket>/o/<the object path, URL-encoded: its / are %2F>
DOWNLOAD_PATH_PATTERN = re.compile(r'/v0/b/([^/]+)/o/([^/]+)')
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# The reference photos of one call together (checked before any download).
PHOTO_REFERENCES_MAX_TOTAL_BYTES = 25 * 1024 * 1024
REFERENCES_TOO_LARGE = ('The photos are over 25 MB together: send fewer or '
                        'smaller photos')
IMAGE_CONTENT_TYPES = ('image/jpeg', 'image/png')
HEIC_CONTENT_TYPES = ('image/heic', 'image/heif')
HEIC_EXTENSIONS = ('.heic', '.heif')
STORE_PHOTO_CONTENT_TYPE = 'image/jpeg'
DOWNLOAD_TOKEN_KEY = 'firebaseStorageDownloadTokens'
# What routeErrors' 503 names: "Photo storage is unavailable right now".
STORAGE_SERVICE = 'photo storage'
CONVERT_PHOTO = 'Convert the photo to JPG or PNG before uploading'
# Each Storage request waits at most STORAGE_TIMEOUT_SECONDS, and the SDK's
# retries stop after STORAGE_RETRY_SECONDS, both cut to the time the photo
# call has left (its deadline).
STORAGE_TIMEOUT_SECONDS = 15
STORAGE_RETRY_SECONDS = 20

_client = None
_clientLock = threading.Lock()


def _isLocalDev() -> bool:
    return Environment().getisLocalDevEnvironment()


def emulatorHost() -> str | None:
    """The Storage emulator's host:port in localdev (STORAGE_EMULATOR_HOST,
    else FIREBASE_STORAGE_EMULATOR_HOST, without a scheme), on a loopback
    host only (as AuthHandler.emulatorTokensAccepted); else None."""
    if not _isLocalDev():
        return None
    for variable in EMULATOR_VARIABLES:
        host = (os.getenv(variable) or '').strip()
        if host:
            host = re.sub(r'^https?://', '', host).rstrip('/').lower()
            return host if EMULATOR_HOST_PATTERN.fullmatch(host) else None
    return None


def bucketName() -> str:
    """FIREBASE_STORAGE_BUCKET, else the Firebase project's default
    bucket."""
    from AuthHandler import _firebaseProjectId
    return ((os.getenv(BUCKET_VARIABLE) or '').strip()
            or f'{_firebaseProjectId()}.firebasestorage.app')


def unavailable() -> ServiceUnavailable:
    return ServiceUnavailable(STORAGE_SERVICE, 'photos')


def _localEmulatorHost() -> str:
    """The emulator's host:port in localdev, which never reaches a real
    bucket: with no emulator variable, or one on a host that is no
    loopback, it is unavailable() (a WARNING names the variable, never its
    value)."""
    host = emulatorHost()
    if host is None:
        setVariables = [v for v in EMULATOR_VARIABLES if os.getenv(v)]
        logging.warning({
            'event': 'photoStorageNoLocalEmulator',
            'variable': setVariables[0] if setVariables else None,
            'detail': 'localdev reaches Storage only through the emulator '
                      'on a loopback host',
        })
        raise unavailable()
    return host


def _storageErrors() -> tuple:
    """What a failed Storage call raises: an API error, a transport error
    or missing credentials."""
    import requests
    from google.api_core.exceptions import GoogleAPIError
    from google.auth.exceptions import GoogleAuthError
    return (GoogleAPIError, GoogleAuthError, requests.RequestException)


def _limits(deadline: float) -> dict:
    """The timeout and retry of one Storage request, cut to the time left
    before `deadline` (time.monotonic()); past it, unavailable()."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise unavailable()
    from google.cloud.storage.retry import DEFAULT_RETRY
    return {'timeout': min(STORAGE_TIMEOUT_SECONDS, remaining),
            'retry': DEFAULT_RETRY.with_timeout(
                min(STORAGE_RETRY_SECONDS, remaining))}


def _storageFailure(error: Exception) -> ServiceUnavailable:
    logging.warning({'event': 'photoStorageError',
                     'errorClass': type(error).__name__})
    return unavailable()


def _bucket():
    """Our bucket, through the one Storage client of the process: in
    localdev the emulator's (anonymous) only, and unavailable() without one
    (_localEmulatorHost), so localdev never builds a client with real
    credentials; elsewhere Google's with the service's own credentials
    (google.auth.default)."""
    global _client
    # Checked on every call, before the cached client is used.
    host = _localEmulatorHost() if _isLocalDev() else None
    with _clientLock:
        if _client is None:
            from google.cloud import storage
            if host is not None:
                from google.auth.credentials import AnonymousCredentials
                from AuthHandler import _firebaseProjectId
                _client = storage.Client(
                    project=_firebaseProjectId(),
                    credentials=AnonymousCredentials(),
                    client_options={'api_endpoint': f'http://{host}'})
            else:
                # The endpoint and the credentials are both named outright:
                # with STORAGE_EMULATOR_HOST set, the SDK would otherwise
                # send its calls to that host, with anonymous credentials.
                import google.auth
                credentials, project = google.auth.default()
                _client = storage.Client(
                    project=project,
                    credentials=credentials,
                    client_options={'api_endpoint': STORAGE_API_ENDPOINT})
        return _client.bucket(bucketName())


def objectPathFromUrl(url, label: str) -> str:
    """The object path in our bucket of the download URL `url` (`label`
    names it in a refusal, which never quotes it)."""
    notOurs = ValueError(f"{label} is not a photo uploaded to this app")
    if not isinstance(url, str):
        raise ValueError(f'{label} must be text')
    try:
        parts = urlsplit(url.strip())
        parts.port  # a malformed port raises ValueError
    except ValueError:
        raise notOurs from None
    if parts.username is not None or parts.password is not None:
        raise notOurs
    # No userinfo (refused above), so the netloc is host[:port].
    hostPort = parts.netloc.lower()
    emulator = emulatorHost()
    if parts.scheme == 'https' and hostPort == DOWNLOAD_HOST:
        pass
    elif (emulator is not None and parts.scheme == 'http'
          and hostPort in _emulatorNames(emulator)):
        pass
    else:
        raise notOurs
    match = DOWNLOAD_PATH_PATTERN.fullmatch(parts.path)
    if match is None or unquote(match.group(1)) != bucketName():
        raise notOurs
    return unquote(match.group(2))


def _emulatorNames(emulator: str) -> set[str]:
    """The host:port names of the emulator: its port on each loopback
    host."""
    port = emulator.rpartition(':')[2]
    return {f'{host}:{port}' for host in EMULATOR_LOOPBACK_HOSTS}


def _isHeic(name: str) -> bool:
    return name.lower().endswith(HEIC_EXTENSIONS)


def ownImagePath(uid: str, path: str, label: str) -> str:
    """`path`, refused unless it is a file under users/<uid>/: no empty,
    "." or ".." segment, no backslash or control character, no other
    user's folder. A HEIC/HEIF name is refused (CONVERT_PHOTO)."""
    notYours = ValueError(f'{label} must be a photo you uploaded')
    if (not isinstance(uid, str) or not uid or '/' in uid
            or not isinstance(path, str) or '\\' in path
            or any(unicodedata.category(c) == 'Cc' for c in path)):
        raise notYours
    segments = path.split('/')
    if (len(segments) < 3 or segments[:2] != ['users', uid]
            or any(s in ('', '.', '..') for s in segments)):
        raise notYours
    if _isHeic(path):
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    return path


def _ownImageBlob(uid: str, path: str, label: str, deadline: float):
    """The blob of the photo at `path` under the caller `uid`'s own folder
    (ownImagePath), with its content type: it must exist, be a JPEG or PNG
    and be at most MAX_IMAGE_BYTES."""
    path = ownImagePath(uid, path, label)
    blob = _bucket().get_blob(path, **_limits(deadline))
    if blob is None:
        raise ValueError(f'{label} was not found: upload it again')
    contentType = (blob.content_type or '').split(';')[0].strip().lower()
    if contentType in HEIC_CONTENT_TYPES:
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    if contentType not in IMAGE_CONTENT_TYPES:
        raise ValueError(f'{label} must be a JPG or PNG photo')
    if blob.size is None or blob.size > MAX_IMAGE_BYTES:
        raise ValueError(f'{label} is over 10 MB')
    return blob, contentType


def readOwnImages(uid: str, photos: list[tuple[str, str]],
                  deadline: float) -> list[tuple[bytes, str]]:
    """The bytes and content type of each photo (its label and path) under
    the caller `uid`'s own folder (_ownImageBlob), read before `deadline`
    (time.monotonic()). Every photo's metadata is checked before any is
    downloaded, their sizes together included
    (PHOTO_REFERENCES_MAX_TOTAL_BYTES)."""
    from google.api_core.exceptions import NotFound
    label = None
    try:
        blobs = []
        for label, path in photos:
            blobs.append((label, *_ownImageBlob(uid, path, label, deadline)))
        total = sum(blob.size for _, blob, _ in blobs)
        if total > PHOTO_REFERENCES_MAX_TOTAL_BYTES:
            raise ValueError(REFERENCES_TOO_LARGE)
        read = []
        for label, blob, contentType in blobs:
            # At most one byte past the limit, should it have grown since.
            data = blob.download_as_bytes(end=MAX_IMAGE_BYTES,
                                          **_limits(deadline))
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(f'{label} is over 10 MB')
            read.append((data, contentType))
    except NotFound:
        raise ValueError(f'{label} was not found: upload it again') from None
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    return read


def storePhotoPath(storeId: str, photoId: str) -> str:
    return f'stores/{storeId}/photos/{photoId}.jpg'


def downloadUrl(path: str, token: str) -> str:
    """The Firebase download URL of `path` in our bucket with the download
    token `token` (the emulator's in localdev when it is set)."""
    host = emulatorHost()
    base = f'http://{host}' if host is not None else f'https://{DOWNLOAD_HOST}'
    return (f'{base}/v0/b/{quote(bucketName(), safe="")}/o/'
            f'{quote(path, safe="")}?alt=media&token={token}')


def saveStorePhoto(storeId: str, photoId: str, data: bytes,
                   deadline: float) -> str:
    """Write the JPEG `data` to storePhotoPath(storeId, photoId) with a
    fresh random download token, before `deadline` (time.monotonic()), and
    return its download URL. Create-only (if_generation_match=0: the path
    is a fresh id), which also lets the SDK retry the upload."""
    from google.api_core.exceptions import PreconditionFailed
    path = storePhotoPath(storeId, photoId)
    token = secrets.token_urlsafe(24)
    try:
        blob = _bucket().blob(path)
        blob.metadata = {DOWNLOAD_TOKEN_KEY: token}
        blob.upload_from_string(data, content_type=STORE_PHOTO_CONTENT_TYPE,
                                if_generation_match=0, **_limits(deadline))
    except PreconditionFailed:
        # A retry found the object there: the path is a fresh id, so it is
        # the first attempt's upload, with this same token. Saved.
        pass
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    return downloadUrl(path, token)

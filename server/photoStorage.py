"""photoStorage.py — product photos in Firebase Storage (hand-written).

The bucket is the Firebase project's: FIREBASE_STORAGE_BUCKET, else
<FIREBASE_PROJECT_ID>.firebasestorage.app. In localdev with
STORAGE_EMULATOR_HOST or FIREBASE_STORAGE_EMULATOR_HOST set (create-star-app
dev sets the second), every call goes to the local Storage emulator
instead; every other environment ignores both.

- objectPathFromUrl(): the object path of a Firebase download URL of our
  bucket (https://firebasestorage.googleapis.com/v0/b/<bucket>/o/<path>,
  plus the emulator's http://<host>/v0/b/... in localdev only). Any other
  scheme, host or bucket is refused.
- readOwnImage(): a JPEG or PNG of at most MAX_IMAGE_BYTES under the
  caller's own users/<uid>/ folder, read by bucket and path through the
  Storage SDK. Nothing is ever fetched by URL, so no request can make the
  server fetch an address of its choosing. HEIC/HEIF is refused by name
  and by type.
- saveStorePhoto(): writes stores/<storeId>/photos/<photoId>.jpg, which
  storage.rules lets anyone read and only the server (admin credentials)
  write, and returns its download URL.

A refusal names the field (imageUrls[0]), never a URL or a path. The client
is one per process, built on first use under a lock (it is no Mongo client:
the connection budget is unchanged). A Storage failure is a 503
("Photo storage is unavailable right now"), logged by class only.
"""

import logging
import os
import re
import secrets
import threading
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from AppConfig import Environment
from services.__request import ServiceUnavailable

BUCKET_VARIABLE = 'FIREBASE_STORAGE_BUCKET'
EMULATOR_VARIABLES = ('STORAGE_EMULATOR_HOST', 'FIREBASE_STORAGE_EMULATOR_HOST')
DOWNLOAD_HOST = 'firebasestorage.googleapis.com'
STORAGE_API_ENDPOINT = 'https://storage.googleapis.com'
# /v0/b/<bucket>/o/<the object path, URL-encoded: its / are %2F>
DOWNLOAD_PATH_PATTERN = re.compile(r'/v0/b/([^/]+)/o/([^/]+)')
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_CONTENT_TYPES = ('image/jpeg', 'image/png')
HEIC_CONTENT_TYPES = ('image/heic', 'image/heif')
HEIC_EXTENSIONS = ('.heic', '.heif')
STORE_PHOTO_CONTENT_TYPE = 'image/jpeg'
DOWNLOAD_TOKEN_KEY = 'firebaseStorageDownloadTokens'
# What routeErrors' 503 names: "Photo storage is unavailable right now".
STORAGE_SERVICE = 'photo storage'
CONVERT_PHOTO = 'Convert the photo to JPG or PNG before uploading'
# Each Storage request waits at most STORAGE_TIMEOUT_SECONDS, and the SDK's
# retries of a read stop after STORAGE_RETRY_SECONDS, well inside the photo
# call's request.
STORAGE_TIMEOUT_SECONDS = 15
STORAGE_RETRY_SECONDS = 20

_client = None
_clientLock = threading.Lock()


def _isLocalDev() -> bool:
    return Environment().getisLocalDevEnvironment()


def emulatorHost() -> str | None:
    """The Storage emulator's host:port in localdev (STORAGE_EMULATOR_HOST,
    else FIREBASE_STORAGE_EMULATOR_HOST, without a scheme), else None."""
    if not _isLocalDev():
        return None
    for variable in EMULATOR_VARIABLES:
        host = (os.getenv(variable) or '').strip()
        if host:
            return re.sub(r'^https?://', '', host).rstrip('/')
    return None


def bucketName() -> str:
    """FIREBASE_STORAGE_BUCKET, else the Firebase project's default
    bucket."""
    from AuthHandler import _firebaseProjectId
    return ((os.getenv(BUCKET_VARIABLE) or '').strip()
            or f'{_firebaseProjectId()}.firebasestorage.app')


def unavailable() -> ServiceUnavailable:
    return ServiceUnavailable(STORAGE_SERVICE, 'photos')


def _storageErrors() -> tuple:
    """What a failed Storage call raises: an API error, a transport error
    or missing credentials."""
    import requests
    from google.api_core.exceptions import GoogleAPIError
    from google.auth.exceptions import GoogleAuthError
    return (GoogleAPIError, GoogleAuthError, requests.RequestException)


def _readRetry():
    from google.cloud.storage.retry import DEFAULT_RETRY
    return DEFAULT_RETRY.with_timeout(STORAGE_RETRY_SECONDS)


def _storageFailure(error: Exception) -> ServiceUnavailable:
    logging.warning({'event': 'photoStorageError',
                     'errorClass': type(error).__name__})
    return unavailable()


def _bucket():
    """Our bucket, through the one Storage client of the process: the
    emulator's (anonymous) in localdev when it is set, else Google's with
    the service's own credentials."""
    global _client
    with _clientLock:
        if _client is None:
            from google.cloud import storage
            host = emulatorHost()
            if host is not None:
                from google.auth.credentials import AnonymousCredentials
                from AuthHandler import _firebaseProjectId
                _client = storage.Client(
                    project=_firebaseProjectId(),
                    credentials=AnonymousCredentials(),
                    client_options={'api_endpoint': f'http://{host}'})
            else:
                # Named outright, so a stray STORAGE_EMULATOR_HOST never
                # redirects a cloud service.
                _client = storage.Client(
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
        port = parts.port
    except ValueError:
        raise notOurs from None
    if parts.username is not None or parts.password is not None:
        raise notOurs
    host = parts.hostname or ''
    hostPort = f'{host}:{port}' if port is not None else host
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
    """The host:port names of the emulator: as set, and with 127.0.0.1 and
    localhost swapped."""
    names = {emulator}
    host, _, port = emulator.rpartition(':')
    if host in ('127.0.0.1', 'localhost'):
        names |= {f'127.0.0.1:{port}', f'localhost:{port}'}
    return names


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


def readOwnImage(uid: str, path: str, label: str) -> tuple[bytes, str]:
    """The bytes and content type of the photo at `path` under the caller
    `uid`'s own folder (ownImagePath): it must exist, be a JPEG or PNG and
    be at most MAX_IMAGE_BYTES."""
    path = ownImagePath(uid, path, label)
    from google.api_core.exceptions import NotFound
    try:
        blob = _bucket().get_blob(path, timeout=STORAGE_TIMEOUT_SECONDS,
                                  retry=_readRetry())
        if blob is None:
            raise ValueError(f'{label} was not found: upload it again')
        contentType = (blob.content_type or '').split(';')[0].strip().lower()
        if contentType in HEIC_CONTENT_TYPES:
            raise ValueError(
                f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
        if contentType not in IMAGE_CONTENT_TYPES:
            raise ValueError(f'{label} must be a JPG or PNG photo')
        if blob.size is None or blob.size > MAX_IMAGE_BYTES:
            raise ValueError(f'{label} is over 10 MB')
        # At most one byte past the limit, should it have grown since.
        data = blob.download_as_bytes(end=MAX_IMAGE_BYTES,
                                      timeout=STORAGE_TIMEOUT_SECONDS,
                                      retry=_readRetry())
    except NotFound:
        raise ValueError(f'{label} was not found: upload it again') from None
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f'{label} is over 10 MB')
    return data, contentType


def storePhotoPath(storeId: str, photoId: str) -> str:
    return f'stores/{storeId}/photos/{photoId}.jpg'


def downloadUrl(path: str, token: str) -> str:
    """The Firebase download URL of `path` in our bucket with the download
    token `token` (the emulator's in localdev when it is set)."""
    host = emulatorHost()
    base = f'http://{host}' if host is not None else f'https://{DOWNLOAD_HOST}'
    return (f'{base}/v0/b/{quote(bucketName(), safe="")}/o/'
            f'{quote(path, safe="")}?alt=media&token={token}')


def saveStorePhoto(storeId: str, photoId: str, data: bytes) -> str:
    """Write the JPEG `data` to storePhotoPath(storeId, photoId) with a
    fresh random download token, and return its download URL."""
    path = storePhotoPath(storeId, photoId)
    token = secrets.token_urlsafe(24)
    try:
        blob = _bucket().blob(path)
        blob.metadata = {DOWNLOAD_TOKEN_KEY: token}
        blob.upload_from_string(data, content_type=STORE_PHOTO_CONTENT_TYPE,
                                timeout=STORAGE_TIMEOUT_SECONDS)
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    return downloadUrl(path, token)

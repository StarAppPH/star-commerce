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
- readOwnFiles(): files of the allowed types and sizes under the caller's
  own users/<uid>/ folder, read by bucket and path through the Storage
  SDK. Nothing is ever fetched by URL, so no request can make the server
  fetch an address of its choosing. HEIC/HEIF is refused by name and by
  type, and by contents: a file's first bytes must be its type's
  (SIGNATURES). ownFiles() and downloadFiles() are its two steps, for a
  caller that budgets between them.
  readOwnImages() is it for photos: JPEGs or PNGs of at most
  MAX_IMAGE_BYTES each (and PHOTO_REFERENCES_MAX_TOTAL_BYTES together);
  the chat's attachments add PDFs.
- publicPathUrl(): the canonical download URL of a path, without a token
  (objectPathFromUrl reads it back).
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
from typing import NamedTuple
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
# The first bytes of each type's files, and the ISO-BMFF brands of a
# HEIC/HEIF one (its ftyp box): a file whose contents are not its type's is
# refused.
SIGNATURES = {'image/jpeg': b'\xff\xd8\xff',
              'image/png': b'\x89PNG\r\n\x1a\n',
              'application/pdf': b'%PDF-'}
HEIC_BRANDS = (b'heic', b'heix', b'hevc', b'heim', b'heis', b'mif1', b'msf1')
NOT_A_REAL_PHOTO = "isn't a real JPG or PNG photo"
# What a skipped file of a skipUnreadable read is.
GONE = 'gone'
REFUSED = 'refused'
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


def objectPathFromUrl(url, label: str, kind: str = 'photo') -> str:
    """The object path in our bucket of the download URL `url` (`label`
    names it in a refusal, which never quotes it; `kind` says what it
    is)."""
    notOurs = ValueError(f"{label} is not a {kind} uploaded to this app")
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


def hasExtension(name: str, extensions: tuple[str, ...]) -> bool:
    """Whether the file name (or path) `name` ends with one of
    `extensions` (lower case, with their dot), whatever its case."""
    return name.lower().endswith(extensions)


def _isHeic(name: str) -> bool:
    return hasExtension(name, HEIC_EXTENSIONS)


def ownImagePath(uid: str, path: str, label: str, kind: str = 'photo') -> str:
    """`path`, refused unless it is a file under users/<uid>/: no empty,
    "." or ".." segment, no backslash or control character, no other
    user's folder. A HEIC/HEIF name is refused (CONVERT_PHOTO). `kind`
    says what it is in a refusal."""
    notYours = ValueError(f'{label} must be a {kind} you uploaded')
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


def ownPathsOfUrls(uid: str, urls: list, field: str, twice: str,
                   kind: str = 'photo') -> list[tuple[str, str]]:
    """The label (field[i]) and object path of each download URL of `urls`:
    of our bucket (objectPathFromUrl) and under the caller `uid`'s own
    folder (ownImagePath), no path twice (else `twice`)."""
    files = []
    for i, url in enumerate(urls):
        label = f'{field}[{i}]'
        path = objectPathFromUrl(url, label, kind)
        files.append((label, ownImagePath(uid, path, label, kind)))
    if len({path for _, path in files}) != len(files):
        raise ValueError(twice)
    return files


def megabytes(size: int) -> str:
    """`size` bytes as a refusal says it: whole MB."""
    return f'{size // (1024 * 1024)} MB'


def _overLimit(label: str, maxBytes: int) -> ValueError:
    return ValueError(f'{label} is over {megabytes(maxBytes)}')


def fileExtension(path: str) -> str:
    """The extension of the path's file name, lower case, with its dot
    ('' without one)."""
    name = path.rpartition('/')[2]
    return name[name.rfind('.'):].lower() if '.' in name else ''


def _isHeicData(data: bytes) -> bool:
    """Whether `data` starts as a HEIC/HEIF file does: an ISO-BMFF ftyp box
    of a HEIF brand."""
    return data[4:8] == b'ftyp' and data[8:12] in HEIC_BRANDS


def _checkContents(data: bytes, contentType: str, label: str,
                   notReal: str) -> None:
    """Refuse `data` unless its first bytes are those of `contentType`
    (SIGNATURES): a HEIC/HEIF file is CONVERT_PHOTO, any other `notReal`
    (after the label)."""
    if _isHeicData(data):
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    if not data.startswith(SIGNATURES[contentType]):
        raise ValueError(f'{label} {notReal}')


class _Gone(ValueError):
    """A file that is not in Storage (any more)."""


class OwnFile(NamedTuple):
    """A file under the caller's own folder, its metadata checked
    (ownFiles), not yet downloaded."""
    label: str
    path: str
    blob: object
    contentType: str

    @property
    def size(self) -> int:
        return self.blob.size


def _ownBlob(uid: str, path: str, label: str, allowedTypes: tuple[str, ...],
             wrongType: str, maxBytes: int,
             extensionTypes: dict[str, str] | None, kind: str,
             deadline: float) -> OwnFile:
    """The file at `path` under the caller `uid`'s own folder
    (ownImagePath), its metadata checked: it must exist (else _Gone), be of
    `allowedTypes` (else `wrongType`, after the label) and, with
    `extensionTypes` (extension: type), the type its name's extension
    names, and be at most `maxBytes`."""
    path = ownImagePath(uid, path, label, kind)
    blob = _bucket().get_blob(path, **_limits(deadline))
    if blob is None:
        raise _Gone(f'{label} was not found: upload it again')
    contentType = (blob.content_type or '').split(';')[0].strip().lower()
    if contentType in HEIC_CONTENT_TYPES:
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    if contentType not in allowedTypes or (
            extensionTypes is not None
            and extensionTypes.get(fileExtension(path)) != contentType):
        raise ValueError(f'{label} {wrongType}')
    if blob.size is None or blob.size > maxBytes:
        raise _overLimit(label, maxBytes)
    return OwnFile(label, path, blob, contentType)


def ownFiles(uid: str, files: list[tuple[str, str]],
             allowedTypes: tuple[str, ...], maxBytes: int, deadline: float,
             *, wrongType: str, extensionTypes: dict[str, str] | None = None,
             kind: str = 'photo', skipUnreadable: bool = False
             ) -> list[OwnFile | str]:
    """Each file (its label and path) under the caller `uid`'s own folder,
    its metadata checked (_ownBlob) before `deadline` (time.monotonic()),
    nothing downloaded yet. With `skipUnreadable`, a file that would be
    refused is GONE (not in Storage) or REFUSED instead. A Storage failure
    is a 503."""
    owned = []
    try:
        for label, path in files:
            try:
                owned.append(_ownBlob(uid, path, label, allowedTypes,
                                      wrongType, maxBytes, extensionTypes,
                                      kind, deadline))
            except ValueError as error:
                if not skipUnreadable:
                    raise
                owned.append(GONE if isinstance(error, _Gone) else REFUSED)
    except _Gone as error:
        raise ValueError(str(error)) from None
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    return owned


def downloadFiles(files: list[OwnFile], maxBytes: int, deadline: float, *,
                  notReal: str, skipUnreadable: bool = False
                  ) -> list[bytes | str]:
    """The bytes of each file (ownFiles), downloaded before `deadline`: at
    most `maxBytes`, and their first bytes those of their type
    (_checkContents, else `notReal`). With `skipUnreadable`, a file that
    would be refused is GONE (deleted since) or REFUSED instead. A Storage
    failure is a 503."""
    from google.api_core.exceptions import NotFound
    read = []
    try:
        for file in files:
            try:
                # At most one byte past the limit, should it have grown
                # since.
                data = file.blob.download_as_bytes(end=maxBytes,
                                                   **_limits(deadline))
                if len(data) > maxBytes:
                    raise _overLimit(file.label, maxBytes)
                _checkContents(data, file.contentType, file.label, notReal)
            except NotFound:
                if not skipUnreadable:
                    raise ValueError(f'{file.label} was not found: upload '
                                     'it again') from None
                read.append(GONE)
                continue
            except ValueError:
                if not skipUnreadable:
                    raise
                read.append(REFUSED)
                continue
            read.append(data)
    except _storageErrors() as error:
        raise _storageFailure(error) from None
    return read


def readOwnFiles(uid: str, files: list[tuple[str, str]],
                 allowedTypes: tuple[str, ...], maxBytes: int,
                 maxTotalBytes: int, deadline: float, *, wrongType: str,
                 notReal: str, totalTooLarge: str,
                 extensionTypes: dict[str, str] | None = None,
                 kind: str = 'photo') -> list[tuple[bytes, str]]:
    """The bytes and content type of each file (its label and path) under
    the caller `uid`'s own folder, read before `deadline`
    (time.monotonic()): every file's metadata is checked first (ownFiles),
    their sizes together included (`maxTotalBytes`, else `totalTooLarge`),
    then each is downloaded and its contents checked (downloadFiles)."""
    owned = ownFiles(uid, files, allowedTypes, maxBytes, deadline,
                     wrongType=wrongType, extensionTypes=extensionTypes,
                     kind=kind)
    if sum(file.size for file in owned) > maxTotalBytes:
        raise ValueError(totalTooLarge)
    read = downloadFiles(owned, maxBytes, deadline, notReal=notReal)
    return [(data, file.contentType) for data, file in zip(read, owned)]


def readOwnImages(uid: str, photos: list[tuple[str, str]],
                  deadline: float) -> list[tuple[bytes, str]]:
    """The bytes and content type of each photo (its label and path) under
    the caller `uid`'s own folder: JPEGs or PNGs of at most MAX_IMAGE_BYTES
    each and PHOTO_REFERENCES_MAX_TOTAL_BYTES together (readOwnFiles), read
    before `deadline` (time.monotonic())."""
    return readOwnFiles(uid, photos, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES,
                        PHOTO_REFERENCES_MAX_TOTAL_BYTES, deadline,
                        wrongType='must be a JPG or PNG photo',
                        notReal=NOT_A_REAL_PHOTO,
                        totalTooLarge=REFERENCES_TOO_LARGE)


def storePhotoPath(storeId: str, photoId: str) -> str:
    return f'stores/{storeId}/photos/{photoId}.jpg'


def publicPathUrl(path: str) -> str:
    """The canonical download URL of `path` in our bucket
    (https://firebasestorage.googleapis.com/v0/b/<bucket>/o/<path>),
    without a token: objectPathFromUrl reads it back, and Storage serves it
    only to a caller its rules let read the path."""
    return (f'https://{DOWNLOAD_HOST}/v0/b/{quote(bucketName(), safe="")}/o/'
            f'{quote(path, safe="")}')


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

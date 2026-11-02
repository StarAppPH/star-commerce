"""urlImport.py — a photo downloaded from a public https link (hand-written).

The one place the server fetches an address it was given: an admin's photo
link in the new products form (PhotoActions.importLinkedPhotos), which then
copies the photo, re-encoded, into the admin's own users/<uid>/imported/
folder. Everything else reads Storage by path (photoStorage).

fetchImage(url, label, deadline) returns (bytes, contentType) of a JPEG, PNG
or WebP, or raises ValueError (a message for the admin, after `label`,
never quoting the link). Guards against a link aimed at the server's own
network (SSRF):

- https only, port 443, no user:password, at most URL_MAX_LENGTH.
- The host's addresses are resolved, and every one must be public
  (ipaddress `is_global`, not multicast): no private, loopback,
  link-local (the metadata server's 169.254.169.254), shared or reserved
  address, IPv4-mapped ones read as their IPv4. The connection goes to the
  checked address itself (TLS still verifies the host's certificate and
  name), so a second DNS answer cannot move it.
- At most MAX_REDIRECTS redirects, each new link checked the same way.
- At most MAX_BYTES, read in chunks and cut off past it (a Content-Length
  over it is refused before reading), within PHOTO_SECONDS and `deadline`.
- The contents decide the type: a HEIC/HEIF file (or a .heic/.heif link or
  an image/heic|heif Content-Type) is CONVERT_PHOTO, anything that is not a
  JPEG, PNG or WebP is NOT_A_REAL_PHOTO.

No log carries a link, a host or a file's contents. `_resolve` and
`_request` are the test seams.
"""

import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

from photoStorage import (CONVERT_PHOTO, HEIC_CONTENT_TYPES, HEIC_EXTENSIONS,
                          IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES,
                          NOT_A_REAL_PHOTO, SIGNATURES, WEBP_CONTENT_TYPE,
                          WEBP_SIGNATURE, hasExtension)

URL_MAX_LENGTH = 4096
MAX_REDIRECTS = 3
MAX_BYTES = MAX_IMAGE_BYTES
PHOTO_SECONDS = 10
CONNECT_SECONDS = 5
CHUNK_BYTES = 64 * 1024
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
USER_AGENT = 'starCommerce-photo-import/1.0'

NOT_HTTPS = 'is not an https link to a photo'
NOT_PUBLIC = 'is not a public web address'
NOT_DOWNLOADED = "couldn't be downloaded: check the link, or add the photo"
TOO_MANY_REDIRECTS = 'redirects too many times'
TOO_LARGE = 'is over 10 MB: use a smaller photo'
TIMED_OUT = 'took too long to download'


def _resolve(host: str) -> list[str]:
    """The addresses `host` resolves to for https (the test seam)."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _isPublic(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _checkedLink(url, label: str) -> tuple[str, str]:
    """The (host, request target) of the https link `url`, refused unless
    it is https on port 443 with no user:password. The host is IDNA
    encoded, the target is the path and query (no fragment)."""
    if not isinstance(url, str) or len(url) > URL_MAX_LENGTH:
        raise ValueError(f'{label} {NOT_HTTPS}')
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise ValueError(f'{label} {NOT_HTTPS}') from None
    host = parts.hostname
    if (parts.scheme.lower() != 'https' or not host
            or parts.username is not None or parts.password is not None
            or port not in (None, 443)
            or any(c.isspace() or ord(c) < 32 for c in url.strip())):
        raise ValueError(f'{label} {NOT_HTTPS}')
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        raise ValueError(f'{label} {NOT_HTTPS}') from None
    if hasExtension(parts.path, HEIC_EXTENSIONS):
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    target = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    return host, target


def _publicAddress(host: str, label: str) -> str:
    """One public address of `host` (an IPv4 one first), refused when it
    has none or any of its addresses is not public."""
    try:
        addresses = _resolve(host)
    except (OSError, UnicodeError):
        raise ValueError(f'{label} {NOT_DOWNLOADED}') from None
    if not addresses or not all(_isPublic(a) for a in addresses):
        raise ValueError(f'{label} {NOT_PUBLIC}')
    ipv4 = [a for a in addresses if ':' not in a]
    return (ipv4 or addresses)[0]


def _remaining(deadline: float, label: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ValueError(f'{label} {TIMED_OUT}')
    return left


def _request(address: str, host: str, target: str, deadline: float):
    """A GET of `target` on `host`, connected to its checked `address`
    (TLS verifies `host`'s certificate), redirects not followed, the body
    not read yet (the test seam). Returns the urllib3 response."""
    left = max(deadline - time.monotonic(), 0.1)
    pool = urllib3.HTTPSConnectionPool(
        address, port=443, maxsize=1, retries=False,
        timeout=urllib3.Timeout(connect=min(CONNECT_SECONDS, left),
                                read=left),
        cert_reqs=ssl.CERT_REQUIRED, ca_certs=certifi.where(),
        server_hostname=host, assert_hostname=host)
    return pool.urlopen(
        'GET', target, redirect=False, retries=False, preload_content=False,
        assert_same_host=False,
        headers={'Host': host, 'User-Agent': USER_AGENT,
                 'Accept': ', '.join(IMAGE_CONTENT_TYPES)})


class _Refused(ValueError):
    """A refusal of ours, with a message for the admin (never one of
    urllib3's, which may quote the host)."""


def _readBody(response, deadline: float, label: str) -> bytes:
    length = response.headers.get('Content-Length')
    if length is not None and length.strip().isdigit() and (
            int(length) > MAX_BYTES):
        raise _Refused(f'{label} {TOO_LARGE}')
    chunks, total = [], 0
    for chunk in response.stream(CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_BYTES:
            raise _Refused(f'{label} {TOO_LARGE}')
        if time.monotonic() >= deadline:
            raise _Refused(f'{label} {TIMED_OUT}')
        chunks.append(chunk)
    return b''.join(chunks)


def _imageType(data: bytes, label: str) -> str:
    """The type the contents of `data` are: a JPEG, PNG or WebP."""
    if data[4:8] == b'ftyp':
        raise ValueError(f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
    for contentType in IMAGE_CONTENT_TYPES:
        if data.startswith(SIGNATURES[contentType]) and (
                contentType != WEBP_CONTENT_TYPE
                or data[8:12] == WEBP_SIGNATURE):
            return contentType
    raise ValueError(f'{label} {NOT_A_REAL_PHOTO}')


def fetchImage(url, label: str, deadline: float) -> tuple[bytes, str]:
    """The photo at the public https link `url`, before `deadline`
    (time.monotonic()) and within PHOTO_SECONDS, as (bytes, contentType),
    its type read from its contents (see the module)."""
    deadline = min(deadline, time.monotonic() + PHOTO_SECONDS)
    for _ in range(MAX_REDIRECTS + 1):
        host, target = _checkedLink(url, label)
        address = _publicAddress(host, label)
        _remaining(deadline, label)
        try:
            response = _request(address, host, target, deadline)
        except Exception:
            # urllib3, ssl and socket errors: never their text (it holds
            # the host).
            if time.monotonic() >= deadline:
                raise ValueError(f'{label} {TIMED_OUT}') from None
            raise ValueError(f'{label} {NOT_DOWNLOADED}') from None
        try:
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get('Location')
                if not location:
                    raise ValueError(f'{label} {NOT_DOWNLOADED}')
                url = urljoin(f'https://{host}{target}', location)
                continue
            if response.status != 200:
                raise ValueError(f'{label} {NOT_DOWNLOADED}')
            declared = (response.headers.get('Content-Type') or '').split(
                ';', 1)[0].strip().lower()
            if declared in HEIC_CONTENT_TYPES:
                raise ValueError(
                    f'{label} is a HEIC or HEIF photo. {CONVERT_PHOTO}')
            try:
                data = _readBody(response, deadline, label)
            except _Refused:
                raise
            except Exception:
                if time.monotonic() >= deadline:
                    raise ValueError(f'{label} {TIMED_OUT}') from None
                raise ValueError(f'{label} {NOT_DOWNLOADED}') from None
            return data, _imageType(data, label)
        finally:
            response.release_conn()
    raise ValueError(f'{label} {TOO_MANY_REDIRECTS}')

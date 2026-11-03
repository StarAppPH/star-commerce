"""photoLinks.py — a product photo read from a link in the admin's
spreadsheet (hand-written, no database).

A spreadsheet import may name a column of photo links
(showNewProductsFromSpreadsheet's photoColumn). The server reads each link
itself (PhotoLinkActions.importLinkedPhotos), so the link is someone else's
address the server is asked to reach: everything here keeps that request
from reaching anything but a public web server's photo.

Each hop (the link, then each redirect, at most REDIRECTS_MAX) is:

1. checked as text (checkedLink): https only, no user or password, port 443
   (or none), a host name with a dot that is no IP address in any spelling
   (dotted, decimal, hex, IPv6), no localhost or .local/.internal name;
2. resolved once (resolve), every address it resolves to checked
   (allowedAddress): a global address only, never a private, loopback,
   link-local (the metadata server's 169.254.169.254 among them), shared
   (100.64.0.0/10), multicast, reserved or unspecified one, an IPv6 address
   that carries an IPv4 one (mapped, compatible, NAT64, 6to4, Teredo)
   refused as well. One refused address refuses the link;
3. connected to that checked address itself (openConnection, a pinned
   HTTPS connection: TLS checks the certificate against the host name), so
   no second DNS answer can send it elsewhere (DNS rebinding);
4. one GET, no cookie, no credential, no proxy, no compression (identity
   only), redirects never followed by the library.

Time: a link has LINK_SECONDS (or less, by the caller's deadline), its
redirects included. Each lookup runs on a thread of its own, waited for at
most CONNECT_SECONDS and the time left (_lookup); each hop has a watchdog
(Hop) that kills its socket when the time is up, wherever it is blocked
(the TLS handshake, the status line, "100 Continue" lines, the headers, a
chunk's size line), so a server that trickles bytes cannot hold a link, its
thread or its caller's slot past its time; each socket read also has a
timeout, READ_SECONDS at most. Out of time is TOO_SLOW. The body is read a
chunk at a time (read1); it stops at MAX_BYTES whatever Content-Length
says, and at its first bytes when they are no JPEG, PNG or WebP (a web
page) or a HEIC/HEIF photo. What it returns is only checked by its first
bytes: the caller decodes it (photoStorage.storeOriginal).

Every failure is a LinkRefused with a code (REASONS has the admin's
words), and `connected`: whether any hop's TCP connection was made (even
when its TLS handshake then failed); the caller gives its quota back only
when none was. No message here carries the link, its host or an address,
and nothing here logs.
"""

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import photoStorage
from sharedConstants import PHOTO_UPLOAD_MAX_BYTES

PORT = 443
URL_MAX_LENGTH = 2048
REDIRECTS_MAX = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# A link's connection, each read, and the whole link (its redirects too).
CONNECT_SECONDS = 5
READ_SECONDS = 5
LINK_SECONDS = 15
CHUNK_BYTES = 64 * 1024
MAX_BYTES = PHOTO_UPLOAD_MAX_BYTES
# The bytes the type is sniffed from before the rest is read.
SNIFF_BYTES = 12
USER_AGENT = 'starCommerce-photo-import/1'
BLOCKED_HOSTS = frozenset({'localhost'})
BLOCKED_HOST_SUFFIXES = ('.localhost', '.local', '.internal', '.home.arpa',
                         '.lan', '.intranet', '.corp')
IMAGE_TYPES = ('image/jpeg', 'image/png', photoStorage.WEBP_CONTENT_TYPE)

# Why a link gave no photo (LinkRefused.code).
NOT_A_LINK = 'notALink'
BLOCKED = 'blocked'
UNREACHABLE = 'unreachable'
HTTP_ERROR = 'httpError'
TOO_MANY_REDIRECTS = 'tooManyRedirects'
TOO_LARGE = 'tooLarge'
NOT_AN_IMAGE = 'notAnImage'
HEIC = 'heic'
TOO_SLOW = 'tooSlow'
REASONS = {
    NOT_A_LINK: 'Not an https link',
    BLOCKED: "The link's address isn't allowed",
    UNREACHABLE: "The link couldn't be reached",
    HTTP_ERROR: 'The link answered with an error',
    TOO_MANY_REDIRECTS: 'The link redirects too many times',
    TOO_LARGE: f'The photo is over {photoStorage.megabytes(MAX_BYTES)}',
    NOT_AN_IMAGE: ("Not a JPG, PNG or WebP photo (use the image's own link, "
                   "not a web page's)"),
    HEIC: f'A HEIC or HEIF photo. {photoStorage.CONVERT_PHOTO}',
    TOO_SLOW: 'The link took too long',
}

# Networks whose addresses is_global lets through but no public photo is
# on: NAT64 (which carries an IPv4 address), the IPv4-compatible IPv6
# addresses, 6to4 and Teredo; and the IPv4 ones, again, a newer or older
# ipaddress might call global.
_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    '64:ff9b::/96', '64:ff9b:1::/48', '::/96', '2002::/16', '2001::/32',
    'fe80::/10', 'fc00::/7', '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10',
    '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24',
    '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4'))
# A host whose last label is a number (decimal or hex) is an address.
_NUMERIC_LABEL = re.compile(r'(0x[0-9a-f]*|[0-9]+)')
# What a link's path and query may hold as they are; the rest is
# percent-encoded.
_PATH_SAFE = "/%:@!$&'()*+,;=-._~?"


class LinkRefused(Exception):
    """A link that gave no photo: `code` (REASONS), and whether any of its
    hops opened a connection."""

    def __init__(self, code: str, connected: bool = False):
        super().__init__(code)
        self.code = code
        self.connected = connected


def _isAddress(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return _NUMERIC_LABEL.fullmatch(host.rsplit('.', 1)[-1]) is not None


def checkedLink(url) -> tuple[str, str]:
    """The ASCII host and the request target (path and query) of the link
    `url`, refused (LinkRefused) unless it is an https link to a host name
    on port 443 with no credentials: NOT_A_LINK, BLOCKED for an address or
    a local name, HEIC for a .heic/.heif file."""
    if (not isinstance(url, str) or not url or len(url) > URL_MAX_LENGTH
            or any(ord(c) < 33 or ord(c) == 127 for c in url)):
        raise LinkRefused(NOT_A_LINK)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise LinkRefused(NOT_A_LINK) from None
    if (parts.scheme.lower() != 'https' or '@' in parts.netloc
            or port not in (None, PORT) or not host):
        raise LinkRefused(NOT_A_LINK)
    host = host.rstrip('.')
    try:
        host = host.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise LinkRefused(NOT_A_LINK) from None
    if (not host or _isAddress(host) or '.' not in host
            or host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES)):
        raise LinkRefused(BLOCKED)
    if photoStorage.hasExtension(parts.path, photoStorage.HEIC_EXTENSIONS):
        raise LinkRefused(HEIC)
    target = quote(parts.path or '/', safe=_PATH_SAFE)
    if parts.query:
        target += '?' + quote(parts.query, safe=_PATH_SAFE)
    return host, target


def allowedAddress(address: str) -> bool:
    """Whether the server may connect to `address` (an IP address as
    getaddrinfo gives it): a global one only (see the module docstring)."""
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return allowedAddress(str(ip.ipv4_mapped))
    if any(ip.version == network.version and ip in network
           for network in _BLOCKED_NETWORKS):
        return False
    return (ip.is_global and not ip.is_private and not ip.is_loopback
            and not ip.is_link_local and not ip.is_multicast
            and not ip.is_reserved and not ip.is_unspecified)


def resolve(host: str) -> list[str]:
    """Every address `host` resolves to, each once, in the resolver's
    order (a test replaces it)."""
    found = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in found))


def _tlsContext() -> ssl.SSLContext:
    """Certificates checked against the host name, TLS 1.2 or later; the
    CA bundle requests uses (certifi) when there is one."""
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _shutDown(sock) -> None:
    """Shut `sock` down (never close it), so a call blocked on it in
    another thread returns at once (an end of file or an OSError). The
    watchdog's way: closing from its thread would free the file
    descriptor while the hop's thread (or OpenSSL) may still make one
    more call on it, by then maybe another socket's (Mongo's, another
    link's). The owning thread closes it."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _kill(sock) -> None:
    """Shut `sock` down and close it: only on the thread that owns it."""
    _shutDown(sock)
    try:
        sock.close()
    except OSError:
        pass


class Hop:
    """One hop's watchdog: at `end` (time.monotonic()) it shuts down the
    socket it watches (_shutDown, never a close: the hop's own thread
    closes it), whatever the hop is blocked on: the TLS handshake,
    the status line, any number of "100 Continue" lines, the headers, a
    chunk's size line. watch() switches it to the hop's newest socket (the
    TCP one, then its TLS wrapper) and marks the hop connected (the TCP
    connection was made). Cancel it when the hop ends."""

    def __init__(self, end: float):
        self.fired = False
        self.connected = False
        self._end = end
        self._sock = None
        self._cancelled = False
        self._lock = threading.Lock()
        self._arm()

    def _arm(self) -> None:
        self._timer = threading.Timer(max(0.0, self._end - time.monotonic()),
                                      self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock) -> None:
        """On the hop's own thread: after a fire, its socket is closed
        here at once."""
        with self._lock:
            self._sock = sock
            self.connected = True
            if self.fired:
                _kill(sock)

    def _fire(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            if time.monotonic() < self._end:
                # A timer may wake a little early (the clock's granularity,
                # about 16 ms on Windows): never before the link's end.
                self._arm()
                return
            self.fired = True
            if self._sock is not None:
                _shutDown(self._sock)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._timer.cancel()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to `host` made to the address `ip` checked
    for it: the TCP connection goes to `ip`, never to a new DNS answer,
    while TLS (its SNI and certificate check) and the Host header name
    `host`. Its sockets are watched by `hop` (the TCP one before the TLS
    handshake, then the TLS one), so the hop's end stops it anywhere."""

    def __init__(self, ip: str, host: str, timeout: float, hop: Hop,
                 port: int = PORT):
        super().__init__(host, PORT, timeout=timeout, context=_tlsContext())
        self.pinnedIp, self.pinnedPort, self.hop = ip, port, hop

    def connect(self) -> None:
        sock = socket.create_connection((self.pinnedIp, self.pinnedPort),
                                        self.timeout)
        self.hop.watch(sock)
        tls = None
        try:
            tls = self._context.wrap_socket(sock, server_hostname=self.host,
                                            do_handshake_on_connect=False)
            self.hop.watch(tls)
            tls.do_handshake()
        except BaseException:
            _kill(sock if tls is None else tls)
            raise
        self.sock = tls


def openConnection(ip: str, host: str, timeout: float,
                   hop: Hop) -> http.client.HTTPSConnection:
    """A not yet connected HTTPS connection to `host` pinned to `ip`,
    watched by `hop` (a test replaces it)."""
    return PinnedHTTPSConnection(ip, host, timeout, hop)


def _left(end: float) -> float:
    left = end - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _setTimeout(connection, seconds: float) -> None:
    if getattr(connection, 'sock', None) is not None:
        connection.sock.settimeout(seconds)


# The lookups run at once: each on a daemon thread of its own, so one the
# system resolver holds never holds a link past its time (the link stops
# waiting for it) nor the process's exit; past them a lookup waits for one,
# within its time.
LOOKUPS_AT_ONCE = 4
_lookupSlots = threading.BoundedSemaphore(LOOKUPS_AT_ONCE)


def _lookup(host: str, end: float) -> list[str]:
    """resolve(host), waited for at most CONNECT_SECONDS and the time the
    link has left: TimeoutError past it."""
    wait = min(CONNECT_SECONDS, _left(end))
    waitUntil = time.monotonic() + wait
    if not _lookupSlots.acquire(timeout=wait):
        raise TimeoutError
    done, box = threading.Event(), {}

    def run():
        try:
            box['found'] = resolve(host)
        except BaseException as error:
            box['error'] = error
        finally:
            _lookupSlots.release()
            done.set()

    threading.Thread(target=run, name='photoLinkLookup', daemon=True).start()
    if not done.wait(max(0.0, waitUntil - time.monotonic())):
        raise TimeoutError
    if 'error' in box:
        raise box['error']
    return box['found']


def _addresses(host: str, end: float) -> list[str]:
    try:
        found = _lookup(host, end)
    except TimeoutError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise LinkRefused(UNREACHABLE) from None
    if not found:
        raise LinkRefused(UNREACHABLE)
    if not all(allowedAddress(address) for address in found):
        raise LinkRefused(BLOCKED)
    return found


def _get(addresses: list[str], host: str, target: str, end: float,
         hop: Hop):
    """The response to a GET of `target` from the first of `addresses`
    that connects, and its connection (the caller closes it)."""
    refused = UNREACHABLE
    for ip in addresses:
        connection = openConnection(ip, host,
                                    min(CONNECT_SECONDS, _left(end)), hop)
        try:
            connection.connect()
        except TimeoutError:
            connection.close()
            refused = TOO_SLOW
            continue
        except (OSError, http.client.HTTPException, ValueError):
            connection.close()
            continue
        try:
            _setTimeout(connection, min(READ_SECONDS, _left(end)))
            connection.request('GET', target, headers={
                'Accept': ', '.join(IMAGE_TYPES),
                'Accept-Encoding': 'identity',
                'User-Agent': USER_AGENT,
            })
            return connection.getresponse(), connection
        except TimeoutError:
            connection.close()
            raise LinkRefused(TOO_SLOW) from None
        except (OSError, http.client.HTTPException, ValueError):
            connection.close()
            raise LinkRefused(UNREACHABLE) from None
    raise LinkRefused(refused)


def sniffedType(data: bytes) -> str:
    """The image type `data` starts as (JPEG, PNG, WebP); a HEIC/HEIF
    file is HEIC, anything else NOT_AN_IMAGE (LinkRefused)."""
    if photoStorage.isHeicData(data):
        raise LinkRefused(HEIC)
    for contentType in IMAGE_TYPES:
        if photoStorage.startsAs(data, contentType):
            return contentType
    raise LinkRefused(NOT_AN_IMAGE)


def _body(response, connection, end: float) -> tuple[bytes, str]:
    encoding = (response.getheader('Content-Encoding') or 'identity')
    if encoding.strip().lower() != 'identity':
        raise LinkRefused(NOT_AN_IMAGE)
    length = (response.getheader('Content-Length') or '').strip()
    if length.isdigit() and int(length) > MAX_BYTES:
        raise LinkRefused(TOO_LARGE)
    chunks, size, sniffed = [], 0, False
    while True:
        _setTimeout(connection, min(READ_SECONDS, _left(end)))
        try:
            chunk = response.read1(CHUNK_BYTES)
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise LinkRefused(UNREACHABLE) from None
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BYTES:
            raise LinkRefused(TOO_LARGE)
        chunks.append(chunk)
        if not sniffed and size >= SNIFF_BYTES:
            sniffedType(b''.join(chunks))
            sniffed = True
    data = b''.join(chunks)
    return data, sniffedType(data)


def _hop(url: str, end: float, hop: Hop) -> tuple[bytes, str] | str:
    """One hop of fetchPhoto: the photo, or the next hop's link (a
    redirect). Ended by `hop` at the link's end."""
    host, target = checkedLink(url)
    response, connection = _get(_addresses(host, end), host, target, end,
                                hop)
    try:
        if response.status in REDIRECT_STATUSES:
            location = response.getheader('Location')
            if not location:
                raise LinkRefused(HTTP_ERROR)
            return urljoin(url, location.strip())
        if response.status != 200:
            raise LinkRefused(HTTP_ERROR)
        return _body(response, connection, end)
    finally:
        connection.close()


def fetchPhoto(url: str, deadline: float) -> tuple[bytes, str]:
    """The bytes and image type (by its first bytes) of the photo at the
    link `url`, read by `deadline` (time.monotonic()) and within
    LINK_SECONDS (see the module docstring). LinkRefused otherwise."""
    end = min(deadline, time.monotonic() + LINK_SECONDS)
    connected = False
    current = url
    for _ in range(REDIRECTS_MAX + 1):
        hop = Hop(end)
        try:
            result = _hop(current, end, hop)
            if hop.fired:
                # Its socket was killed: what it read was cut short.
                raise LinkRefused(TOO_SLOW)
        except Exception as error:
            connected = connected or hop.connected
            # A socket the watchdog killed may fail in any way (an end of
            # file, an OSError, an SSL object gone mid-handshake).
            if hop.fired or isinstance(error, TimeoutError):
                code = TOO_SLOW
            elif isinstance(error, LinkRefused):
                code = error.code
            elif isinstance(error, (OSError, http.client.HTTPException,
                                    ValueError)):
                code = UNREACHABLE
            else:
                raise
            raise LinkRefused(code, connected) from None
        finally:
            hop.cancel()
        connected = connected or hop.connected
        if not isinstance(result, str):
            return result
        current = result
    raise LinkRefused(TOO_MANY_REDIRECTS, connected)

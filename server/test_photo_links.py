"""Photos read from a spreadsheet import's photo links (photoLinks.py,
actions/PhotoLinkActions.py, showNewProductsFromSpreadsheet's photoColumn).

No test reaches the network: conftest's FakePhotoWeb answers every lookup
and connection (photoLinks.resolve and openConnection), and the counting
FakeBucket and FakeModel of the chat suites play Storage and Meta. The
first part checks photoLinks alone (the link, the address, the pinned
connection, redirects, the body); the second the import through the chat
route."""

import datetime
import io
import json
import logging
import socket
import ssl
import threading
import time

import pytest
from PIL import Image

import chatForms
import conftest
import photoLinks
import photoStorage
import turnDeadline
from actions import ChatActions, PhotoActions, PhotoLinkActions, StoreActions
from actions.usageCounters import appDay, takeQuota, usageKey
from conftest import PUBLIC_IP, createTestStore
from mongoDb import db
# Fixtures (the autouse ones apply here too) and helpers of the chat,
# chat attachment and import suites.
from test_chat import (ADMIN, calls, clean, clock, freezeNow,  # noqa
                       frozenNow, model, offline, reply)
from test_chat_attachments import mine, sendAdmin, storage, urlOf  # noqa
from test_product_import import (TOOL, csvOf, mapping, openImport,
                                 refusal, submit, toolMessage)

PRIVATE_IP = '10.0.0.7'
LINK = 'https://shop.example.com/mug.jpg'
FAR_FUTURE = 10**12


def jpeg(size=(64, 48), color=(200, 30, 30), exif: bool = False) -> bytes:
    image = Image.new('RGB', size, color)
    out = io.BytesIO()
    options = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = 'PhoneMaker'  # Make
        data[0x8825] = {2: (10.0, 20.0, 30.0)}  # GPS
        options['exif'] = data.tobytes()
    image.save(out, 'JPEG', **options)
    return out.getvalue()


def png(size=(32, 32)) -> bytes:
    out = io.BytesIO()
    Image.new('RGBA', size, (0, 0, 255, 128)).save(out, 'PNG')
    return out.getvalue()


def webp(size=(32, 32)) -> bytes:
    out = io.BytesIO()
    Image.new('RGB', size, (0, 255, 0)).save(out, 'WEBP')
    return out.getvalue()


HEIC = b'\x00\x00\x00\x18ftypheic' + b'\x00' * 32
HTML = b'<!doctype html><html><body>Mug</body></html>'


def fetch(url: str = LINK) -> tuple[bytes, str]:
    return photoLinks.fetchPhoto(url, FAR_FUTURE)


def refused(url: str = LINK) -> photoLinks.LinkRefused:
    with pytest.raises(photoLinks.LinkRefused) as caught:
        fetch(url)
    return caught.value


# ============================ the link itself ============================ #


@pytest.mark.parametrize('url,code', [
    ('http://shop.example.com/mug.jpg', photoLinks.NOT_A_LINK),
    ('ftp://shop.example.com/mug.jpg', photoLinks.NOT_A_LINK),
    ('mug.jpg', photoLinks.NOT_A_LINK),
    ('just some words', photoLinks.NOT_A_LINK),
    ('https://user:pass@shop.example.com/mug.jpg', photoLinks.NOT_A_LINK),
    ('https://user@shop.example.com/mug.jpg', photoLinks.NOT_A_LINK),
    ('https://shop.example.com:8443/mug.jpg', photoLinks.NOT_A_LINK),
    ('https://shop.example.com:80/mug.jpg', photoLinks.NOT_A_LINK),
    ('https://shop.example.com:bad/mug.jpg', photoLinks.NOT_A_LINK),
    ('https:///mug.jpg', photoLinks.NOT_A_LINK),
    ('https://' + 'a' * 2050 + '.com/', photoLinks.NOT_A_LINK),
    ('https://127.0.0.1/mug.jpg', photoLinks.BLOCKED),
    ('https://8.8.8.8/mug.jpg', photoLinks.BLOCKED),
    ('https://[::1]/mug.jpg', photoLinks.BLOCKED),
    ('https://[2606:4700::1]/mug.jpg', photoLinks.BLOCKED),
    ('https://2130706433/mug.jpg', photoLinks.BLOCKED),
    ('https://0x7f.0x1/mug.jpg', photoLinks.BLOCKED),
    ('https://127.1/mug.jpg', photoLinks.BLOCKED),
    ('https://localhost/mug.jpg', photoLinks.BLOCKED),
    ('https://localhost./mug.jpg', photoLinks.BLOCKED),
    ('https://intranet/mug.jpg', photoLinks.BLOCKED),
    ('https://metadata.google.internal/computeMetadata/v1/',
     photoLinks.BLOCKED),
    ('https://printer.local/mug.jpg', photoLinks.BLOCKED),
    ('https://shop.example.com/mug.HEIC', photoLinks.HEIC),
    ('https://shop.example.com/mug.heif?x=1', photoLinks.HEIC),
])
def test_aLinkThatIsNoPublicHttpsName_isRefused_beforeAnyLookup(
        _fakePhotoLinks, url, code):
    assert refused(url).code == code
    assert _fakePhotoLinks.resolved == []
    assert _fakePhotoLinks.connects == []


def test_checkedLink_givesTheAsciiHostAndAnEncodedTarget():
    assert photoLinks.checkedLink(
        'https://Shop.Example.COM:443/a/%C3%BC.jpg?x=1&y=2#frag') == (
        'shop.example.com', '/a/%C3%BC.jpg?x=1&y=2')
    assert photoLinks.checkedLink('https://bücher.example/ü.jpg') == (
        'xn--bcher-kva.example', '/%C3%BC.jpg')


@pytest.mark.parametrize('address', [
    '10.1.2.3', '172.16.0.1', '172.31.255.255', '192.168.1.1', '127.0.0.1',
    '127.9.9.9', '169.254.169.254', '169.254.0.1', '100.64.0.1',
    '100.127.255.255', '0.0.0.0', '0.1.2.3', '192.0.0.9', '198.18.0.1',
    '224.0.0.1', '239.255.255.250', '240.0.0.1', '255.255.255.255',
    '::', '::1', 'fe80::1', 'fe80::1%eth0', 'fc00::1', 'fd12:3456::1',
    'ff02::1', '::ffff:10.0.0.1', '::ffff:127.0.0.1', '::ffff:169.254.169.254',
    '::a00:1', '64:ff9b::a00:1', '64:ff9b::808:808', '64:ff9b:1::1',
    '2002:a00:1::1', '2002:808:808::1', '2001::1', '2001:db8::1',
    'not an address',
])
def test_anAddressThatIsNotGlobal_isNotAllowed(address):
    assert not photoLinks.allowedAddress(address)


@pytest.mark.parametrize('address', [
    '93.184.216.34', '8.8.8.8', '1.1.1.1', '2606:4700::1111',
    '2a00:1450:4001:80b::200e', '::ffff:8.8.8.8',
])
def test_aGlobalAddress_isAllowed(address):
    assert photoLinks.allowedAddress(address)


# ====================== the request, through the fake web ====================== #


def test_aPhoto_isReadFromTheCheckedAddress_withOneGet(_fakePhotoLinks):
    web = _fakePhotoLinks
    photo = jpeg()
    web.serve(LINK, photo, headers={'Content-Type': 'image/jpeg'})

    assert fetch() == (photo, 'image/jpeg')

    assert web.resolved == ['shop.example.com']
    assert web.connects == [(PUBLIC_IP, 'shop.example.com')]
    [request] = web.requests
    assert request['method'] == 'GET'
    assert request['target'] == '/mug.jpg'
    assert request['headers']['Accept-Encoding'] == 'identity'
    assert 'Cookie' not in request['headers']
    assert 'Authorization' not in request['headers']


@pytest.mark.parametrize('make,contentType', [
    (png, 'image/png'), (webp, 'image/webp'), (jpeg, 'image/jpeg')])
def test_theTypeIsReadFromTheBytes_notTheHeader(_fakePhotoLinks, make,
                                                contentType):
    _fakePhotoLinks.serve(LINK, make(), headers={'Content-Type': 'text/html'})
    assert fetch()[1] == contentType


def test_aHostWithOnePrivateAddress_isRefused_neverConnected(
        _fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg(), ips=(PUBLIC_IP, PRIVATE_IP))

    error = refused()

    assert (error.code, error.connected) == (photoLinks.BLOCKED, False)
    assert _fakePhotoLinks.connects == []


@pytest.mark.parametrize('address', ['127.0.0.1', '169.254.169.254',
                                     '::1', '::ffff:10.0.0.1'])
def test_aHostResolvingToAPrivateAddress_isRefused(_fakePhotoLinks,
                                                   address):
    _fakePhotoLinks.serve(LINK, jpeg(), ips=(address, ))
    assert refused().code == photoLinks.BLOCKED
    assert _fakePhotoLinks.connects == []


def test_aHostThatDoesNotResolve_isUnreachable(_fakePhotoLinks):
    error = refused()
    assert (error.code, error.connected) == (photoLinks.UNREACHABLE, False)


def test_dnsRebinding_cannotMoveTheConnection(_fakePhotoLinks):
    """The host is looked up once per hop and the connection made to the
    address checked: a second answer (private) is never asked for."""
    web = _fakePhotoLinks
    web.serve(LINK, jpeg())
    web.hosts['shop.example.com'] = [[PUBLIC_IP], [PRIVATE_IP]]

    fetch()

    assert web.resolved == ['shop.example.com']
    assert web.connects == [(PUBLIC_IP, 'shop.example.com')]
    assert [request['ip'] for request in web.requests] == [PUBLIC_IP]


def test_aRebindingBetweenRedirectHops_isCheckedAgain(_fakePhotoLinks):
    """A redirect to the same host is a new hop: looked up and checked
    again, so a private second answer is refused there."""
    web = _fakePhotoLinks
    web.serve(LINK, status=302, headers={'Location': '/real.jpg'})
    web.serve('https://shop.example.com/real.jpg', jpeg())
    web.hosts['shop.example.com'] = [[PUBLIC_IP], [PRIVATE_IP]]

    error = refused()

    assert (error.code, error.connected) == (photoLinks.BLOCKED, True)
    assert web.resolved == ['shop.example.com', 'shop.example.com']
    assert web.connects == [(PUBLIC_IP, 'shop.example.com')]


def test_aRedirect_isFollowed_andCheckedAsALink(_fakePhotoLinks):
    web = _fakePhotoLinks
    photo = jpeg()
    web.serve(LINK, status=301, headers={
        'Location': 'https://cdn.example.net/files/mug.jpg'})
    web.serve('https://cdn.example.net/files/mug.jpg', photo,
              ips=('1.1.1.1', ))

    assert fetch() == (photo, 'image/jpeg')
    assert web.connects == [(PUBLIC_IP, 'shop.example.com'),
                            ('1.1.1.1', 'cdn.example.net')]


@pytest.mark.parametrize('location,code', [
    ('https://inside.example.com/x.jpg', photoLinks.BLOCKED),
    ('https://10.0.0.7/x.jpg', photoLinks.BLOCKED),
    ('https://169.254.169.254/latest/meta-data/', photoLinks.BLOCKED),
    ('http://cdn.example.net/x.jpg', photoLinks.NOT_A_LINK),
    ('https://cdn.example.net:8080/x.jpg', photoLinks.NOT_A_LINK),
    ('https://localhost/x.jpg', photoLinks.BLOCKED),
])
def test_aRedirectToWhereALinkMayNotGo_isRefused(_fakePhotoLinks, location,
                                                 code):
    web = _fakePhotoLinks
    web.serve(LINK, status=302, headers={'Location': location})
    web.hosts['inside.example.com'] = [PRIVATE_IP]

    error = refused()

    assert (error.code, error.connected) == (code, True)
    assert web.connects == [(PUBLIC_IP, 'shop.example.com')]


def test_moreThanThreeRedirects_areRefused(_fakePhotoLinks):
    web = _fakePhotoLinks
    for n in range(5):
        web.serve(f'https://shop.example.com/{n}.jpg', status=307,
                  headers={'Location': f'/{n + 1}.jpg'})
    web.serve('https://shop.example.com/5.jpg', jpeg())

    assert refused('https://shop.example.com/0.jpg').code == \
        photoLinks.TOO_MANY_REDIRECTS
    assert len(web.requests) == photoLinks.REDIRECTS_MAX + 1


@pytest.mark.parametrize('status', [404, 403, 500, 206, 204])
def test_anyOtherStatus_isAnError(_fakePhotoLinks, status):
    _fakePhotoLinks.serve(LINK, jpeg(), status=status)
    assert refused().code == photoLinks.HTTP_ERROR


def test_aRedirectWithNoLocation_isAnError(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, status=302)
    assert refused().code == photoLinks.HTTP_ERROR


def test_aConnectionThatFails_triesTheNextCheckedAddress(_fakePhotoLinks):
    web = _fakePhotoLinks
    web.serve(LINK, jpeg(), ips=('1.1.1.1', PUBLIC_IP))
    web.unreachable.add('1.1.1.1')

    fetch()

    assert web.connects == [('1.1.1.1', 'shop.example.com'),
                            (PUBLIC_IP, 'shop.example.com')]


def test_noAddressConnecting_isUnreachable(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg())
    _fakePhotoLinks.unreachable.add(PUBLIC_IP)
    error = refused()
    assert (error.code, error.connected) == (photoLinks.UNREACHABLE, False)


# ================================ the body ================================ #


def test_aContentLengthOverTheCap_stopsBeforeTheBodyIsRead(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg(), headers={
        'Content-Length': str(photoLinks.MAX_BYTES + 1)})

    assert refused().code == photoLinks.TOO_LARGE
    [response] = _fakePhotoLinks.responses
    assert response.reads == 0


@pytest.mark.parametrize('headers', [{}, {'Content-Length': '100'}])
def test_aBodyOverTheCap_isCut_whateverItsContentLengthSays(
        _fakePhotoLinks, headers):
    start = jpeg()
    chunk = b'\x00' * photoLinks.CHUNK_BYTES
    count = photoLinks.MAX_BYTES // len(chunk) + 5
    _fakePhotoLinks.serve(LINK, headers=headers,
                          chunks=[start] + [chunk] * count)

    assert refused().code == photoLinks.TOO_LARGE
    [response] = _fakePhotoLinks.responses
    assert len(response.chunks) >= 3  # stopped at the cap, not at the end


def test_aWebPage_isNotAnImage_andStopsAtItsFirstBytes(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, chunks=[HTML] + [b'x' * 1000] * 50)

    assert refused().code == photoLinks.NOT_AN_IMAGE
    [response] = _fakePhotoLinks.responses
    assert response.reads == 1


def test_aCompressedBody_isNotAnImage(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg(), headers={'Content-Encoding': 'gzip'})
    assert refused().code == photoLinks.NOT_AN_IMAGE


@pytest.mark.parametrize('body', [b'GIF89a' + b'\x00' * 40,
                                  b'%PDF-1.4' + b'\x00' * 40,
                                  b'RIFF\x00\x00\x00\x00WAVEfmt ',
                                  b'\xff\xd8'])
def test_otherBytes_areNotAnImage(_fakePhotoLinks, body):
    _fakePhotoLinks.serve(LINK, body)
    assert refused().code == photoLinks.NOT_AN_IMAGE


def test_aHeicPhoto_isRefused_byItsBytes(_fakePhotoLinks):
    _fakePhotoLinks.serve('https://shop.example.com/mug', HEIC)
    error = refused('https://shop.example.com/mug')
    assert error.code == photoLinks.HEIC
    assert 'Convert the photo to JPG, PNG or WebP' in \
        photoLinks.REASONS[error.code]


def test_aSlowBody_isCutAtTheLinksTime(_fakePhotoLinks, monkeypatch, clock):
    """Each read's timeout is the time the link has left, and the link
    ends at LINK_SECONDS however the bytes trickle."""
    monkeypatch.setattr(photoLinks, 'time', clock)
    trickle = [jpeg()[:20]] + [b'\x00'] * 100

    def slowly():
        clock.t += 4
    _fakePhotoLinks.serve(LINK, chunks=trickle, beforeRead=slowly)

    error = refused()

    assert (error.code, error.connected) == (photoLinks.TOO_SLOW, True)
    assert clock.t <= 1000 + photoLinks.LINK_SECONDS + 4
    [sock] = _fakePhotoLinks.sockets
    assert sock.timeouts and all(0 < t <= photoLinks.READ_SECONDS
                                 for t in sock.timeouts)
    assert sock.timeouts[-1] < photoLinks.READ_SECONDS  # the time left


def test_aReadTimeout_isTooSlow(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, chunks=[jpeg()[:20], TimeoutError()])
    assert refused().code == photoLinks.TOO_SLOW


def test_aBrokenConnection_isUnreachable(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, chunks=[jpeg()[:20], ConnectionResetError()])
    assert refused().code == photoLinks.UNREACHABLE


# ============================ the pinned connection ============================ #


def test_thePinnedConnection_connectsToTheCheckedAddress_withTlsOnTheName(
        monkeypatch):
    made = {}

    class FakeSocket:
        def close(self):
            made['closed'] = True

    def createConnection(address, timeout):
        made['address'], made['timeout'] = address, timeout
        return FakeSocket()

    class Tls(str):
        def do_handshake(self):
            pass

    class FakeContext:
        def wrap_socket(self, sock, server_hostname,
                        do_handshake_on_connect=True):
            made['serverHostname'] = server_hostname
            return Tls('tls')

    monkeypatch.setattr(photoLinks.socket, 'create_connection',
                        createConnection)
    monkeypatch.setattr(photoLinks, '_tlsContext', lambda: FakeContext())
    hop = photoLinks.Hop(time.monotonic() + 60)
    connection = photoLinks.PinnedHTTPSConnection(PUBLIC_IP,
                                                  'shop.example.com', 5, hop)

    try:
        connection.connect()
    finally:
        hop.cancel()

    assert made == {'address': (PUBLIC_IP, 443), 'timeout': 5,
                    'serverHostname': 'shop.example.com'}
    assert connection.sock == 'tls'
    assert connection.host == 'shop.example.com'


def test_theTlsContext_checksTheCertificateAndTheName():
    context = photoLinks._tlsContext()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_theRealOpenConnection_isThePinnedOne(monkeypatch):
    # conftest replaced openConnection; the module's own is pinned.
    import importlib
    fresh = importlib.reload(photoLinks)
    try:
        hop = fresh.Hop(time.monotonic() + 60)
        hop.cancel()
        connection = fresh.openConnection(PUBLIC_IP, 'shop.example.com', 3,
                                          hop)
        assert isinstance(connection, fresh.PinnedHTTPSConnection)
        assert connection.pinnedIp == PUBLIC_IP
    finally:
        importlib.reload(photoLinks)


def test_theRealResolve_listsEachAddressOnce(monkeypatch):
    import importlib
    fresh = importlib.reload(photoLinks)
    try:
        monkeypatch.setattr(fresh.socket, 'getaddrinfo', lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '',
             ('2606:4700::1111', 443, 0, 0)),
        ])
        assert fresh.resolve('one.example') == ['1.1.1.1',
                                                '2606:4700::1111']
    finally:
        importlib.reload(photoLinks)


# ===================== the import, through the chat route ===================== #


def linkKey(storeId: str) -> str:
    return usageKey(PhotoLinkActions.LINKS_KIND, storeId,
                    appDay(PhotoActions._now()))


def linkCount(storeId: str) -> int:
    counter = db.read({'_id': linkKey(storeId)}, PhotoActions.PHOTO_USAGE,
                      findOne=True)
    return 0 if counter is None else counter['count']


def photoSheet(links: list) -> bytes:
    return csvOf([['Name', 'Price', 'Photo']] + [
        [f'P{n}', n, link] for n, link in enumerate(links, start=1)])


@pytest.fixture
def photoUsage(clean):
    """No PhotoUsage counter before or after (the chat suite's clean
    leaves that collection)."""
    db.delete({}, PhotoActions.PHOTO_USAGE)
    yield
    db.delete({}, PhotoActions.PHOTO_USAGE)


@pytest.fixture
def links(_fakePhotoLinks, storage, photoUsage, monkeypatch, clock):
    """The fake web, with the links' clocks the chat's."""
    monkeypatch.setattr(photoLinks, 'time', clock)
    monkeypatch.setattr(PhotoLinkActions, 'time', clock)
    monkeypatch.setattr(turnDeadline, 'time', clock)
    monkeypatch.delenv(PhotoLinkActions.LINK_DAILY_CAP_ALL_VARIABLE,
                       raising=False)
    return _fakePhotoLinks


def test_theLinksPhotos_areOnTheRows_andTheModelGetsOnlyCounts(
        links, storage, model, caplog):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg(size=(3000, 1500), exif=True))
    links.serve('https://shop.example.com/cup.png', png())
    links.serve('https://shop.example.com/page', HTML)
    links.serve('https://inside.example.com/x.jpg', jpeg(),
                ips=(PRIVATE_IP, ))
    sheet = photoSheet([LINK, 'https://shop.example.com/cup.png', '',
                        LINK, 'https://shop.example.com/page',
                        'http://shop.example.com/x.jpg',
                        'https://inside.example.com/x.jpg', 'photo.jpg'])

    with caplog.at_level(logging.INFO):
        res, opened = openImport(storage, model, sheet,
                                 mapping(photoColumn='photo'))

    [form] = res['components']
    photos = form['rowPhotos']
    assert len(photos) == 8
    assert photos[0] is not None and photos[3] == photos[0]  # read once
    assert photos[1] is not None
    assert photos[2:3] + photos[4:] == [None] * 5
    for path in (photos[0], photos[1]):
        assert path.startswith(f'users/{ADMIN}/imported/')
        assert photoStorage.ownImagePath(ADMIN, path, 'x') == path
    assert photos[1].endswith('.png')
    saved = storage.objects[photos[0]]
    assert saved['contentType'] == 'image/jpeg'
    with Image.open(io.BytesIO(saved['data'])) as image:
        assert max(image.size) == PhotoLinkActions.MAX_SIDE
        assert not image.getexif()
    reasons = PhotoLinkActions.REASONS
    assert form['spreadsheetImport']['photoLinks'] == {
        'fetched': 3,
        'notFetched': [
            {'row': 6, 'name': 'P5', 'reason': reasons[photoLinks.NOT_AN_IMAGE]},
            {'row': 7, 'name': 'P6', 'reason': reasons[photoLinks.NOT_A_LINK]},
            {'row': 8, 'name': 'P7', 'reason': reasons[photoLinks.BLOCKED]},
            {'row': 9, 'name': 'P8', 'reason': reasons[photoLinks.NOT_A_LINK]},
        ]}
    told = toolMessage(opened)
    assert told['photosFetched'] == 3
    assert told['photosNotFetched'] == 4
    assert told['photosNotFetchedRows'][0] == {
        'row': 6, 'reason': reasons[photoLinks.NOT_AN_IMAGE]}
    # The model never reads a link, a host or a path.
    for text in (json.dumps(told), *(r.getMessage() for r in caplog.records)):
        assert 'example.com' not in text
        assert 'imported/' not in text
    # The 3 links that passed the text check were counted; the one refused
    # by its address before connecting was given back.
    assert linkCount(store['_id']) == 3
    [event] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
               and r.msg.get('event') == 'spreadsheetPhotoLinks']
    assert event['links'] == 6 and event['fetched'] == 2
    assert event['quotaTaken'] == 4 and event['quotaGivenBack'] == 1
    assert event['refused'] == {photoLinks.NOT_AN_IMAGE: 1,
                                photoLinks.NOT_A_LINK: 2,
                                photoLinks.BLOCKED: 1}


def test_theSubmit_putsTheLinksPhotosOnTheProducts(links, storage, model):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    res, _ = openImport(storage, model, photoSheet([LINK, '']),
                        mapping(photoColumn='Photo'))
    [form] = res['components']
    sent = [{**row, 'imageUrl': urlOf(path) if path else None}
            for row, path in zip(form['products'], form['rowPhotos'])]
    saved = model(reply('Added with photos.'))

    submit(res, form, sent)

    products = {doc['name']: doc for doc in db.read(
        {'storeId': store['_id']}, 'Products')}
    assert products['P1'].get('photoUrl')
    assert not products['P2'].get('photoUrl')
    assert "1 photo put on" in [m for m in saved.requests[0]['messages']
                                if m['role'] == 'user'][-1]['content']


def test_anImportWithoutAPhotoColumn_isUnchanged(links, storage, model):
    createTestStore(ADMIN)
    res, opened = openImport(storage, model, photoSheet([LINK]), mapping())
    [form] = res['components']
    assert form['rowPhotos'] == []
    assert 'photoLinks' not in form['spreadsheetImport']
    assert 'photosFetched' not in toolMessage(opened)
    assert links.resolved == []


def test_aPhotoColumnThatIsNotThere_isRefused_namingTheHeaders(
        links, storage, model):
    createTestStore(ADMIN)
    _, opened = openImport(storage, model, photoSheet([LINK]),
                           mapping(photoColumn='Image URL'))
    assert refusal(opened) == ('No column is headed "Image URL" (the photo '
                               'links). Its columns: "Name", "Price", '
                               '"Photo"')


def test_aBlankPhotoColumn_isNone(links, storage, model):
    createTestStore(ADMIN)
    res, _ = openImport(storage, model, photoSheet([LINK]),
                        mapping(photoColumn='  '))
    assert res['components'][0]['rowPhotos'] == []


def test_withAPhotoColumn_oneImportShowsAtMost50Rows(links, storage, model):
    createTestStore(ADMIN)
    res, opened = openImport(storage, model, photoSheet([''] * 60),
                             mapping(photoColumn='Photo'))
    [form] = res['components']
    assert len(form['products']) == 50
    assert form['spreadsheetImport']['nextFirstRow'] == 52
    assert toolMessage(opened)['nextFirstRow'] == 52
    assert form['spreadsheetImport']['photoLinks'] == {'fetched': 0,
                                                       'notFetched': []}


def test_theStoresDailyCap_opensTheFormWithoutPhotos(links, storage, model):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    assert takeQuota(PhotoActions.PHOTO_USAGE, linkKey(store['_id']), 10**6,
                     datetime.datetime(2099, 1, 1),
                     PhotoLinkActions.STORE_DAILY_LINK_CAP)

    res, opened = openImport(storage, model, photoSheet([LINK, '']),
                             mapping(photoColumn='Photo'))

    [form] = res['components']
    assert form['rowPhotos'] == [None, None]
    assert form['spreadsheetImport']['photoLinks']['notFetched'] == [
        {'row': 2, 'name': 'P1', 'reason': PhotoLinkActions.LINK_CAP_REACHED}]
    assert links.connects == []
    assert linkCount(store['_id']) == PhotoLinkActions.STORE_DAILY_LINK_CAP


def test_allStoresDailyCap_opensTheFormWithoutPhotos(links, storage, model,
                                                     monkeypatch):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    monkeypatch.setenv(PhotoLinkActions.LINK_DAILY_CAP_ALL_VARIABLE, '1')
    two = photoSheet([LINK, 'https://shop.example.com/two.jpg'])

    res, _ = openImport(storage, model, two, mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'notFetched'][0]['reason'] == PhotoLinkActions.LINK_ALL_CAP_REACHED
    assert linkCount(store['_id']) == 0


def test_aCapOf0_turnsPhotoLinksOff(links, storage, model, monkeypatch):
    createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    monkeypatch.setenv(PhotoLinkActions.LINK_DAILY_CAP_ALL_VARIABLE, '0')

    res, _ = openImport(storage, model, photoSheet([LINK]),
                        mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'] == {
        'fetched': 0, 'notFetched': [
            {'row': 2, 'name': 'P1', 'reason': PhotoLinkActions.LINKS_TURNED_OFF}]}
    assert links.connects == []


def test_anotherImportReadingLinks_refusesTheImport_beforeAnything(
        links, storage, model):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    with PhotoLinkActions.linkSlot():
        res, opened = openImport(storage, model, photoSheet([LINK]),
                                 mapping(photoColumn='Photo'))
    assert res['components'] == []
    assert refusal(opened) == chatForms.IMPORT_BUSY
    assert linkCount(store['_id']) == 0
    # The slot is free again once refused.
    with PhotoLinkActions.linkSlot():
        pass


def test_linksNotStartedInTime_areGivenBack(links, storage, model,
                                            monkeypatch):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    monkeypatch.setattr(PhotoLinkActions, 'LINKS_SECONDS', 0)

    res, _ = openImport(storage, model, photoSheet([LINK]),
                        mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'notFetched'] == [{'row': 2, 'name': 'P1', 'reason': PhotoLinkActions.REASONS[
            PhotoLinkActions.NOT_IN_TIME]}]
    assert links.connects == []
    assert linkCount(store['_id']) == 0


def test_aTurnWithLittleTimeLeft_leavesItsReplyItsTime(monkeypatch, clock):
    """The links' deadlines are the turn's (turnDeadline, set by the chat
    route's runningTool around the form tool)."""
    monkeypatch.setattr(turnDeadline, 'time', clock)
    given = []
    monkeypatch.setattr(
        PhotoLinkActions, 'importLinkedPhotos',
        lambda userId, storeId, rows, workBy, saveBy, reserve, release:
        given.append((workBy, saveBy)) or PhotoLinkActions.LinkedPhotos(
            [], 0, []))
    imported = SimpleImported([])
    context = chatForms.FormContext('c', (), 0, None)

    def deadlinesFor(secondsLeft):
        with turnDeadline.runningTool(secondsLeft):
            chatForms._linkedPhotos(ADMIN, 's', imported, context)
        return given.pop()

    workBy, saveBy = deadlinesFor(90)
    assert workBy == clock.t + PhotoLinkActions.LINKS_SECONDS
    assert saveBy == clock.t + PhotoLinkActions.LINKS_SECONDS + \
        PhotoLinkActions.LINK_SAVE_SECONDS
    workBy, saveBy = deadlinesFor(30)
    assert workBy == clock.t + 30 - 25 and saveBy == clock.t + 30 - 15


class SimpleImported:
    def __init__(self, photoLinks):
        self.photoLinks = photoLinks


def test_noBytesInFlightFree_leavesTheLinksBusy_andGivesTheQuotaBack(
        links, storage, model, monkeypatch):
    store = createTestStore(ADMIN)
    links.serve(LINK, jpeg())
    sheet = photoSheet([LINK])
    monkeypatch.setattr(ChatActions, 'CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX',
                        len(sheet) + photoLinks.MAX_BYTES - 1)

    res, _ = openImport(storage, model, sheet, mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'notFetched'] == [{'row': 2, 'name': 'P1', 'reason': PhotoLinkActions.REASONS[
            PhotoLinkActions.BUSY]}]
    assert linkCount(store['_id']) == 0


def test_fewerBytesFree_readsWithFewerWorkers(links, storage, model,
                                              monkeypatch, caplog):
    createTestStore(ADMIN)
    urls = [f'https://shop.example.com/{n}.jpg' for n in range(4)]
    for url in urls:
        links.serve(url, jpeg())
    sheet = photoSheet(urls)
    monkeypatch.setattr(ChatActions, 'CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX',
                        len(sheet) + 2 * photoLinks.MAX_BYTES)

    with caplog.at_level(logging.INFO):
        res, _ = openImport(storage, model, sheet,
                            mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'fetched'] == 4
    [event] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
               and r.msg.get('event') == 'spreadsheetPhotoLinks']
    assert event['workers'] == 2


def test_aLinkThatConnectedAndFailed_keepsItsQuota(links, storage, model):
    store = createTestStore(ADMIN)
    links.serve(LINK, HEIC)
    big = 'https://shop.example.com/big.png'
    links.serve(big, png(size=(5000, 5000)))
    slow = 'https://shop.example.com/slow.jpg'
    links.serve(slow, chunks=[jpeg()[:20], TimeoutError()])

    res, _ = openImport(storage, model, photoSheet([LINK, big, slow]),
                        mapping(photoColumn='Photo'))

    reasons = PhotoLinkActions.REASONS
    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'notFetched'] == [
        {'row': 2, 'name': 'P1', 'reason': reasons[photoLinks.HEIC]},
        {'row': 3, 'name': 'P2', 'reason': reasons[PhotoLinkActions.TOO_MANY_PIXELS]},
        {'row': 4, 'name': 'P3', 'reason': reasons[photoLinks.TOO_SLOW]}]
    assert linkCount(store['_id']) == 3


def test_aPhotoThatCannotBeSaved_leavesItsRowWithout(links, storage, model,
                                                     monkeypatch):
    createTestStore(ADMIN)
    links.serve(LINK, jpeg())

    def failing(*args):
        raise photoStorage.unavailable()
    monkeypatch.setattr(photoStorage, 'saveImportedPhoto', failing)

    res, _ = openImport(storage, model, photoSheet([LINK]),
                        mapping(photoColumn='Photo'))

    assert res['components'][0]['spreadsheetImport']['photoLinks'][
        'notFetched'] == [{'row': 2, 'name': 'P1', 'reason': PhotoLinkActions.REASONS[
            PhotoLinkActions.NOT_SAVED]}]


def test_anUnexpectedFailure_isARowWithoutAPhoto_neverAFailedImport(
        links, storage, model, monkeypatch, caplog):
    createTestStore(ADMIN)
    links.serve(LINK, jpeg())

    def broken(*args):
        raise RuntimeError('https://shop.example.com/secret')
    monkeypatch.setattr(photoLinks, 'fetchPhoto', broken)

    with caplog.at_level(logging.INFO):
        res, _ = openImport(storage, model, photoSheet([LINK]),
                            mapping(photoColumn='Photo'))

    assert res['components'][0]['rowPhotos'] == [None]
    assert 'example.com' not in caplog.text
    assert 'RuntimeError' in caplog.text


def test_aStoreReset_carriesTheLinkCounterOver(photoUsage):
    day = datetime.date(2026, 10, 30)
    old = usageKey(PhotoLinkActions.LINKS_KIND, 'old-store', day)
    assert takeQuota(PhotoActions.PHOTO_USAGE, old, 10**6,
                     datetime.datetime(2099, 1, 1), 41)

    StoreActions._carryOverUsage('old-store', 'new-store', day, None)

    new = db.read({'_id': usageKey(PhotoLinkActions.LINKS_KIND, 'new-store',
                                   day)}, PhotoActions.PHOTO_USAGE,
                  findOne=True)
    assert new['count'] == 41


def test_theImportTool_offersThePhotoColumn():
    tool = chatForms.FORM_TOOLS[TOOL]
    properties = tool.argsModel.model_json_schema()['properties']
    assert 'photoColumn' in properties
    assert 'photoColumn' in tool.description


# ===================== the link's time, whatever it blocks on ===================== #


@pytest.fixture
def quickLinks(_fakePhotoLinks, monkeypatch):
    """A link's whole time cut to QUICK seconds (real time), its connect
    and read timeouts far above it: only the hop's watchdog (or the lookup
    wait) can end it."""
    monkeypatch.setattr(photoLinks, 'LINK_SECONDS', QUICK)
    monkeypatch.setattr(photoLinks, 'CONNECT_SECONDS', 60)
    monkeypatch.setattr(photoLinks, 'READ_SECONDS', 60)
    return _fakePhotoLinks


QUICK = 0.4
# What a stopped link may take past its time (a loaded test machine).
SLACK = 3


def timedRefusal(url: str = LINK) -> tuple[photoLinks.LinkRefused, float]:
    started = time.monotonic()
    error = refused(url)
    return error, time.monotonic() - started


def test_aResponseThatNeverComes_isStoppedAtTheLinksTime(quickLinks):
    quickLinks.serve(LINK, status='hang')

    error, took = timedRefusal()

    assert (error.code, error.connected) == (photoLinks.TOO_SLOW, True)
    assert QUICK <= took < QUICK + SLACK
    [sock] = quickLinks.sockets
    assert sock.killed.is_set()


def test_endlessContinueLines_areStoppedAtTheLinksTime(quickLinks):
    quickLinks.serve(LINK, status='continue')

    error, took = timedRefusal()

    assert error.code == photoLinks.TOO_SLOW
    assert QUICK <= took < QUICK + SLACK
    assert quickLinks.continueLines > 0


class RecordingSocket:
    """A socket that records, by thread, what was done to it."""

    def __init__(self):
        self.shutdowns, self.closes = [], []
        self.shutDown = threading.Event()

    def shutdown(self, how):
        self.shutdowns.append((how, threading.current_thread()))
        self.shutDown.set()

    def close(self):
        self.closes.append(threading.current_thread())


def test_theWatchdog_onlyShutsItsSocketDown_neverClosesIt():
    """Closing from the timer's thread would free the descriptor while
    the hop's thread may still use it (then maybe another socket's)."""
    sock = RecordingSocket()
    hop = photoLinks.Hop(time.monotonic() + 0.05)
    hop.watch(sock)
    try:
        assert sock.shutDown.wait(10)
    finally:
        hop.cancel()

    assert hop.fired
    assert [how for how, _ in sock.shutdowns] == [socket.SHUT_RDWR]
    assert sock.shutdowns[0][1] is not threading.current_thread()
    assert sock.closes == []


def test_aSocketWatchedAfterTheFire_isClosedOnTheHopsThread():
    hop = photoLinks.Hop(time.monotonic())
    deadline = time.monotonic() + 10
    while not hop.fired and time.monotonic() < deadline:
        time.sleep(0.01)
    sock = RecordingSocket()
    try:
        hop.watch(sock)
    finally:
        hop.cancel()

    assert hop.fired
    assert sock.closes == [threading.current_thread()]


def test_aChunkSizeLineThatNeverEnds_isStopped_andWhatWasReadDropped(
        quickLinks):
    """A read blocked inside one read1() (a chunk's size line trickled)
    ends when the watchdog kills the socket, as an end of file: the bytes
    read so far are never taken for the photo."""
    quickLinks.serve(LINK, chunks=[jpeg()[:20], conftest.HANG])

    error, took = timedRefusal()

    assert error.code == photoLinks.TOO_SLOW
    assert QUICK <= took < QUICK + SLACK


def test_aHandshakeThatNeverEnds_isStopped_andKeepsTheConnection(
        quickLinks):
    quickLinks.serve(LINK, jpeg())
    quickLinks.hangingHandshakes.add(PUBLIC_IP)

    error, took = timedRefusal()

    # The TCP connection was made: its quota is kept (no free probing).
    assert (error.code, error.connected) == (photoLinks.TOO_SLOW, True)
    assert QUICK <= took < QUICK + SLACK


def test_aLookupThatNeverAnswers_isStoppedAtItsWait(quickLinks,
                                                   monkeypatch):
    monkeypatch.setattr(photoLinks, 'CONNECT_SECONDS', QUICK)
    quickLinks.serve(LINK, jpeg())
    quickLinks.lookupDelay = threading.Event()

    error, took = timedRefusal()

    assert (error.code, error.connected) == (photoLinks.TOO_SLOW, False)
    assert QUICK <= took < QUICK + SLACK
    assert quickLinks.connects == []


def test_noTimeLeft_isTooSlow_beforeAnyLookup(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg())
    with pytest.raises(photoLinks.LinkRefused) as caught:
        photoLinks.fetchPhoto(LINK, time.monotonic() - 1)
    assert caught.value.code == photoLinks.TOO_SLOW
    assert _fakePhotoLinks.resolved == []


def test_aRealTrickledTlsHandshake_isStoppedAtTheLinksTime(quickLinks,
                                                          monkeypatch):
    """A real socket on this machine (never the network): a server that
    accepts and then trickles a TLS record a byte at a time, so no read
    ever times out; the watchdog ends the handshake."""
    server = socket.create_server(('127.0.0.1', 0))
    port = server.getsockname()[1]
    stop = threading.Event()

    def trickle():
        client, _ = server.accept()
        try:
            client.sendall(b'\x16\x03\x03\x40\x00')  # a 16 KB handshake
            while not stop.is_set():
                client.sendall(b'\x00')
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            client.close()

    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    monkeypatch.setattr(
        photoLinks, 'openConnection',
        lambda ip, host, timeout, hop: photoLinks.PinnedHTTPSConnection(
            '127.0.0.1', host, timeout, hop, port=port))
    quickLinks.serve(LINK, jpeg())
    try:
        error, took = timedRefusal()
    finally:
        stop.set()
        server.close()
        thread.join(5)

    assert (error.code, error.connected) == (photoLinks.TOO_SLOW, True)
    assert QUICK <= took < QUICK + SLACK


def test_aFailedTcpConnection_isNotConnected(_fakePhotoLinks):
    _fakePhotoLinks.serve(LINK, jpeg())
    _fakePhotoLinks.unreachable.add(PUBLIC_IP)
    assert refused().connected is False


def test_anEscapeFromTheReads_givesBackAllButTheConnectedLinks(
        photoUsage, monkeypatch):
    """Whatever escapes the pool (here a BaseException from a read), the
    links that never connected get their quota back, then it is
    re-raised."""
    class Stop(BaseException):
        pass

    second = 'https://shop.example.com/two.jpg'

    def reading(userId, link, workBy, saveBy):
        if link == second:
            raise Stop()
        return PhotoLinkActions._Result(None, photoLinks.HTTP_ERROR, True)
    monkeypatch.setattr(PhotoLinkActions, '_readLink', reading)
    monkeypatch.setattr(PhotoLinkActions, 'LINK_WORKERS', 1)
    released = []

    with pytest.raises(Stop):
        PhotoLinkActions.importLinkedPhotos(
            ADMIN, 'st-escape', [(2, LINK, 'P1'), (3, second, 'P2')],
            FAR_FUTURE, FAR_FUTURE, lambda size: True, released.append)

    assert linkCount('st-escape') == 1
    assert released == [photoLinks.MAX_BYTES]

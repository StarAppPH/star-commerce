"""Photos from a link in the new products form: urlImport's guards (the
network is conftest's FakeLinkWeb, nothing is fetched), and the form
downloading each public link into the owner's own folder, telling the model
which rows got no photo."""

import io
import json
import time

import pytest

import urlImport
import photoStorage
from actions import PhotoActions
from conftest import createTestStore, wipeCollections
from mongoDb import db
from test_chat import ADMIN, calls, reply
from test_chat_components import (PHOTO_BUCKET, adminSays, photoBucket,
                                  uploadUrl, wiped)
from test_chat_components import __all__ as _chatFixtures
from test_photos import JPEG, FakeBucket

from test_chat import clean, clock, frozenNow, model, offline  # noqa: F401

__all__ = ['photoBucket', 'wiped', *_chatFixtures]

LINK = 'https://photos.example.com/cup.jpg?token=abc'


def png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGBA', (4, 3), (10, 20, 30, 128)).save(out, 'PNG')
    return out.getvalue()


def fetch(url=LINK, seconds: float = 30):
    return urlImport.fetchImage(url, 'The photo', time.monotonic() + seconds)


# ── urlImport ───────────────────────────────

def test_aPublicHttpsLink_isDownloaded_itsTypeReadFromItsContents(linkWeb):
    linkWeb.serve(LINK, png(), headers={'Content-Type': 'image/jpeg'})

    data, contentType = fetch()

    assert (data, contentType) == (png(), 'image/png')
    # Connected to the checked address, the host sent for TLS and Host.
    assert linkWeb.requests == [('93.184.215.14', 'photos.example.com',
                                 '/cup.jpg?token=abc')]


@pytest.mark.parametrize('url', [
    'http://photos.example.com/cup.jpg',
    'ftp://photos.example.com/cup.jpg',
    'https://user:pass@photos.example.com/cup.jpg',
    'https://photos.example.com:8443/cup.jpg',
    'https:///cup.jpg',
    'https://photos.example.com/c up.jpg',
    'not a link',
    None,
    'https://photos.example.com/' + 'a' * 5000,
])
def test_aLinkThatIsNotPlainHttps_isRefused_withoutAnyRequest(linkWeb, url):
    with pytest.raises(ValueError,
                       match='^The photo is not an https link to a photo$'):
        fetch(url)
    assert linkWeb.requests == []


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.5', '172.16.3.4', '192.168.1.1',
    '169.254.169.254', '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1',
    'fe80::1', 'fd00::1', '::ffff:127.0.0.1', '::ffff:169.254.169.254',
])
def test_aHostOnAPrivateAddress_isRefused_withoutAnyRequest(linkWeb,
                                                            address):
    linkWeb.addresses['photos.example.com'] = [address]
    linkWeb.serve(LINK, JPEG)

    with pytest.raises(ValueError,
                       match='^The photo is not a public web address$'):
        fetch()
    assert linkWeb.requests == []


def test_aHostWithAnyPrivateAddress_isRefused(linkWeb):
    linkWeb.addresses['photos.example.com'] = ['93.184.215.14', '10.0.0.1']
    linkWeb.serve(LINK, JPEG)

    with pytest.raises(ValueError, match='not a public web address'):
        fetch()
    assert linkWeb.requests == []


def test_aRedirect_isFollowed_andEachHopChecked(linkWeb):
    linkWeb.serve(LINK, status=302,
                  headers={'Location': 'https://cdn.example.net/x.jpg'})
    linkWeb.serve('https://cdn.example.net/x.jpg', JPEG)

    assert fetch() == (JPEG, 'image/jpeg')

    linkWeb.requests.clear()
    linkWeb.addresses['cdn.example.net'] = ['169.254.169.254']
    with pytest.raises(ValueError, match='not a public web address'):
        fetch()
    # The internal hop was never requested.
    assert [r[1] for r in linkWeb.requests] == ['photos.example.com']


def test_aRedirectToHttp_isRefused(linkWeb):
    linkWeb.serve(LINK, status=301,
                  headers={'Location': 'http://photos.example.com/cup.jpg'})

    with pytest.raises(ValueError, match='not an https link'):
        fetch()


def test_tooManyRedirects_areRefused(linkWeb):
    for i in range(urlImport.MAX_REDIRECTS + 1):
        linkWeb.serve(f'https://photos.example.com/{i}', status=307,
                      headers={'Location': f'/{i + 1}'})
    linkWeb.serve(f'https://photos.example.com/{urlImport.MAX_REDIRECTS + 1}',
                  JPEG)

    with pytest.raises(ValueError,
                       match='^The photo redirects too many times$'):
        fetch('https://photos.example.com/0')
    assert len(linkWeb.requests) == urlImport.MAX_REDIRECTS + 1


def test_aPhotoOver10MB_isRefused_byItsLengthOrWhileRead(linkWeb,
                                                        monkeypatch):
    monkeypatch.setattr(urlImport, 'MAX_BYTES', 1000)
    linkWeb.serve(LINK, JPEG + b'x' * 2000)
    with pytest.raises(ValueError, match='^The photo is over 10 MB'):
        fetch()

    linkWeb.serve(LINK, JPEG, headers={'Content-Length': '5000'})
    with pytest.raises(ValueError, match='^The photo is over 10 MB'):
        fetch()


@pytest.mark.parametrize('body,headers,url', [
    (b'\x00\x00\x00\x18ftypheic' + b'\x00' * 20, {}, LINK),
    (JPEG, {'Content-Type': 'image/heif'}, LINK),
    (JPEG, {}, 'https://photos.example.com/cup.HEIC'),
])
def test_aHeicPhoto_isRefused_byContentsTypeOrName(linkWeb, body, headers,
                                                   url):
    linkWeb.serve(url, body, headers=headers)

    with pytest.raises(ValueError, match='Convert the photo to JPG, PNG or '
                       'WebP before uploading'):
        fetch(url)


def test_aPageThatIsNotAPhoto_isRefused(linkWeb):
    linkWeb.serve(LINK, b'<html>hello</html>',
                  headers={'Content-Type': 'image/jpeg'})

    with pytest.raises(ValueError,
                       match="^The photo isn't a real JPG, PNG or WebP"):
        fetch()


def test_anErrorStatusOrAnUnreachableHost_isNotDownloaded(linkWeb):
    linkWeb.serve(LINK, b'gone', status=404)
    with pytest.raises(ValueError, match="^The photo couldn't be downloaded"):
        fetch()
    with pytest.raises(ValueError, match="^The photo couldn't be downloaded"):
        fetch('https://nowhere.example.org/a.jpg')


def test_aDownloadPastItsDeadline_timesOut(linkWeb, monkeypatch):
    linkWeb.serve(LINK, JPEG)
    with pytest.raises(ValueError, match='^The photo took too long'):
        fetch(seconds=-1)
    assert linkWeb.requests == []


def test_aRefusal_neverQuotesTheLink(linkWeb):
    secret = 'https://secret-host.example.com/private.jpg?token=s3cr3t'
    linkWeb.addresses['secret-host.example.com'] = ['10.1.2.3']
    with pytest.raises(ValueError) as error:
        fetch(secret)
    assert 'secret' not in str(error.value)


# ── The new products form ──────────────────

@pytest.fixture
def bucket(photoBucket, monkeypatch):
    wipeCollections([PhotoActions.PHOTO_USAGE])
    fake = FakeBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: fake)
    yield fake
    wipeCollections([PhotoActions.PHOTO_USAGE])


def toolResult(fake, callId='c1') -> dict:
    [message] = [m for m in fake.requests[1]['messages']
                 if m['role'] == 'tool' and m.get('tool_call_id') == callId]
    return json.loads(message['content'])


def importCounters() -> list[dict]:
    return db.read({'_id': {'$regex': f'^{PhotoActions.IMPORTS_KIND}:'}},
                   PhotoActions.PHOTO_USAGE)


def test_aNewProductsForm_downloadsEachLinkedPhoto_intoTheOwnersFolder(
        wiped, bucket, linkWeb, model):
    createTestStore(ADMIN)
    linkWeb.serve(LINK, JPEG)
    linkWeb.serve('https://photos.example.com/plate.png', png())
    own = uploadUrl(ADMIN, 'bowl.jpg')
    fake = model(calls(('c1', 'showNewProductsForm', json.dumps(
        {'products': [{'name': 'Cup', 'imageUrl': LINK},
                      {'name': 'Plate', 'imageUrl':
                       'https://photos.example.com/plate.png'},
                      {'name': 'Bowl', 'imageUrl': own},
                      {'name': 'Lid'}]}))),
        reply('Check the photos.'))

    [form] = adminSays('Add these')['components']

    cup, plate, bowl, lid = form['rowPhotos']
    assert cup.startswith(f'users/{ADMIN}/imported/') and cup.endswith(
        '.jpg')
    assert plate.startswith(f'users/{ADMIN}/imported/') and plate.endswith(
        '.png')
    assert (bowl, lid) == (f'users/{ADMIN}/chat-photos/bowl.jpg', None)
    # Re-encoded: saved as what Pillow wrote, with its own download token.
    saved = bucket.objects[cup]
    assert saved['contentType'] == 'image/jpeg'
    assert saved['data'] == photoStorage.storeOriginal(
        JPEG, 'image/jpeg', 'x')[0]
    assert photoStorage.DOWNLOAD_TOKEN_KEY in saved['metadata']
    # Every photo on: the model is told nothing more.
    assert 'photosNotAdded' not in toolResult(fake)
    # 2 photos, counted on the store's counter and all stores'.
    assert sum(c['count'] for c in importCounters()) == 4


def test_aNewProductsForm_tellsTheModel_whichRowsGotNoPhoto(
        wiped, bucket, linkWeb, model):
    createTestStore(ADMIN)
    linkWeb.serve(LINK, JPEG)
    linkWeb.addresses['internal.example.com'] = ['10.0.0.8']
    sheet = uploadUrl(ADMIN, 'list.png')
    fake = model(calls(('c1', 'showNewProductsForm', json.dumps(
        {'products': [
            {'name': 'Cup', 'imageUrl': LINK},
            {'name': 'Plate', 'imageUrl': 'https://photos.example.com/cut'},
            {'name': 'Bowl', 'imageUrl': 'https://internal.example.com/a.jpg'},
            {'name': 'Tray', 'imageUrl': sheet},
            {'name': 'Box', 'imageUrl': sheet},
            {'name': 'Mug', 'imageUrl': uploadUrl('someone-else')},
            {'name': 'Lid', 'imageUrl': 'http://photos.example.com/lid.jpg'},
        ]}))),
        reply('Some photos could not be added.'))

    [form] = adminSays('Add these')['components']

    assert form['rowPhotos'][0].startswith(f'users/{ADMIN}/imported/')
    assert form['rowPhotos'][1:] == [None] * 6
    assert toolResult(fake)['photosNotAdded'] == [
        {'row': 2, 'reason': "The photo of row 2 couldn't be downloaded: "
                             'check the link, or add the photo'},
        {'row': 3, 'reason': 'The photo of row 3 is not a public web '
                             'address'},
        {'row': 4, 'reason': 'The photo of row 4 is on several rows, so no '
                             'row got it'},
        {'row': 5, 'reason': 'The photo of row 5 is on several rows, so no '
                             'row got it'},
        {'row': 6, 'reason': 'The photo of row 6 must be a photo you '
                             'uploaded'},
        {'row': 7, 'reason': 'The photo of row 7 is not an https link to a '
                             'photo'},
    ]
    # Only the photo saved stays counted (the store's and all stores').
    assert sum(c['count'] for c in importCounters()) == 2
    tool = __import__('chatForms').FORM_TOOLS['showNewProductsForm']
    assert 'photosNotAdded' in tool.description
    assert 'never that their photo is on' in tool.description


def test_linkImports_reachingTheStoresDailyCap_leaveTheRowsEmpty(
        wiped, bucket, linkWeb, model, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(PhotoActions, 'STORE_DAILY_IMPORT_CAP', 1)
    linkWeb.serve(LINK, JPEG)
    linkWeb.serve('https://photos.example.com/b.jpg', JPEG)
    fake = model(calls(('c1', 'showNewProductsForm', json.dumps(
        {'products': [{'name': 'Cup', 'imageUrl': LINK},
                      {'name': 'Plate', 'imageUrl':
                       'https://photos.example.com/b.jpg'}]}))),
        reply('Add them on the card.'))

    [form] = adminSays('Add these')['components']

    assert form['rowPhotos'] == [None, None]
    assert [n['reason'] for n in toolResult(fake)['photosNotAdded']] == [
        f'The photo of row {i}: {PhotoActions.IMPORT_CAP_REACHED}'
        for i in (1, 2)]
    assert linkWeb.requests == []
    assert bucket.objects == {}


def test_linkImports_turnedOff_leaveTheRowsEmpty(wiped, bucket, linkWeb,
                                                 model, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setenv(PhotoActions.IMPORT_DAILY_CAP_ALL_VARIABLE, '0')
    linkWeb.serve(LINK, JPEG)
    fake = model(calls(('c1', 'showNewProductsForm', json.dumps(
        {'products': [{'name': 'Cup', 'imageUrl': LINK}]}))),
        reply('Add it on the card.'))

    [form] = adminSays('Add this')['components']

    assert form['rowPhotos'] == [None]
    assert toolResult(fake)['photosNotAdded'] == [
        {'row': 1, 'reason':
         f'The photo of row 1: {PhotoActions.IMPORTS_TURNED_OFF}'}]


def test_aLinkedPhoto_isPutOnTheProduct_whenTheFormIsSent(
        wiped, bucket, linkWeb, model):
    """The imported photo is the owner's own upload: the answer takes it,
    and setUploadedProductPhotos copies it into the store's photos."""
    createTestStore(ADMIN)
    linkWeb.serve(LINK, JPEG)
    model(calls(('c1', 'showNewProductsForm', json.dumps(
        {'products': [{'name': 'Cup', 'imageUrl': LINK}]}))),
        reply('Check the photo.'))
    res = adminSays('Add this')
    [form] = res['components']
    [path] = form['rowPhotos']
    url = (f'https://{photoStorage.DOWNLOAD_HOST}/v0/b/{PHOTO_BUCKET}/o/'
           f'{path.replace("/", "%2F")}?alt=media&token=t')
    fake = model(reply('Adding it.'))

    adminSays(None, res['conversationId'], {
        'kind': 'newProducts', 'formId': form['formId'], 'products': [
            {'name': 'Cup', 'price': 50, 'inventory': 3, 'imageUrl': url}]})

    # The answer passed the own-upload check: the model got it as a photo.
    sent = json.dumps(fake.requests[0]['messages'])
    assert 'Cup' in sent and '(with a photo)' in sent

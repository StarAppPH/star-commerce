"""Product photos (PhotoActions, imageModel, photoStorage) and the store's
photo theme (updateMyStoreTheme).

No real Meta or Storage call: a FakeBucket replaces photoStorage._bucket()
(an in-memory object store), and a FakeImages client replaces
chatModel.client(), recording every images/edits request (modelClient)."""

import asyncio
import base64
import datetime
import importlib
import io
import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import quote

import httpx2 as httpx
import openai
import pytest
from flask_jwt_extended import create_access_token

import chatModel
import imageModel
import mcpServer
import photoStorage
import pubSub as pubSubModule
from actions import PhotoActions, usageCounters
from actions.StoreActions import NO_STORE_YET, NO_THEME_SENT
from ApiRequests import ApiRequests
from conftest import (assertExactShape, createTestStore, createTestUser,
                      modelKeys, wipeCollections, withoutBrandColors)
from mongoDb import db
from objects import (PHOTO_NOTES_MAX_LENGTH, PHOTOS_PER_CALL, PHOTOS_THEMED,
                     PRODUCT_NAME_MAX_LENGTH,
                     AdminStoreResponse,
                     GeneratedPhotosResponse, MyStoreResponse, StoreResponse,
                     StoreThemeResponse)
from objects.store import PHOTO_STYLE_MAX_LENGTH, STORE_THEME_FIELDS
from routeErrors import routeFailure
from services.__request import ServiceUnavailable

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'GeneratedPhotos',
               'PhotoUsage']
ADMIN = 'ph-admin'
BUCKET = 'ph-test-bucket.firebasestorage.app'
JPEG = b'\xff\xd8\xff' + b'jpeg-bytes'
# Far ahead, so no TTL monitor deletes a counter mid-test.
FAR_EXPIRY = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)

api = ApiRequests()


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


class FakeBlob:

    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.content_type = self.size = None
        self.metadata = None

    def download_as_bytes(self, end, **kwargs):
        return self.bucket.objects[self.name]['data'][:end + 1]

    def upload_from_string(self, data, content_type, **kwargs):
        self.bucket.uploads.append(kwargs)
        if len(self.bucket.uploads) > self.bucket.uploadsThatWork:
            raise photoStorage._storageErrors()[0]('upload failed')
        self.bucket.objects[self.name] = {'data': data,
                                          'contentType': content_type,
                                          'metadata': self.metadata}


class FakeBucket:

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.uploadsThatWork = 100

    def get_blob(self, name, **kwargs):
        stored = self.objects.get(name)
        if stored is None:
            return None
        blob = FakeBlob(self, name)
        blob.content_type = stored['contentType']
        blob.size = stored.get('size', len(stored['data']))
        return blob

    def blob(self, name):
        return FakeBlob(self, name)


class FakeImages:
    """images.edit returns `returned` images per call, or the n asked for
    when it is None. Calls may come at once (one per prompt)."""

    def __init__(self, returned: int | None = None):
        self.returned = returned
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        encoded = base64.b64encode(JPEG).decode()
        returned = kwargs['n'] if self.returned is None else self.returned
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)
                                     for _ in range(returned)])

    def byN(self) -> dict:
        """The calls by their n: 1 is the global prompt's, PHOTOS_THEMED
        the themed one's."""
        byN = {call['n']: call for call in self.calls}
        assert len(byN) == len(self.calls)
        return byN


def dataUrlImage(dataUrl: str) -> tuple[bytes, str]:
    """A body `images` entry's data URL as the photo it carries: its bytes
    and content type."""
    header, encoded = dataUrl.split(',', 1)
    assert header.startswith('data:') and header.endswith(';base64')
    return (base64.b64decode(encoded, validate=True),
            header[len('data:'):-len(';base64')])


def modelClient(images):
    """chatModel.client() for imageModel: its post('/images/edits', ...)
    goes to images.edit as one call's fields (the JSON body's, `image` the
    photos of its `images` in order, each its bytes and content type, and
    `timeout`). Nothing goes as multipart."""

    def post(path, *, cast_to, body, options):
        assert path == '/images/edits'
        return images.edit(
            **body, image=[dataUrlImage(entry['image_url'])
                           for entry in body['images']],
            timeout=options['timeout'])

    return SimpleNamespace(post=post)


@pytest.fixture
def fakes(clean, monkeypatch):
    bucket = FakeBucket()
    images = FakeImages()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.setattr(chatModel, 'client', lambda: modelClient(images))
    return SimpleNamespace(bucket=bucket, images=images)


def downloadUrl(path: str) -> str:
    return (f'https://{photoStorage.DOWNLOAD_HOST}/v0/b/{BUCKET}/o/'
            f'{quote(path, safe="")}?alt=media&token=t')


def test_objectPathFromUrl_readsOurBucketsDownloadUrl(clean):
    path = f'users/{ADMIN}/item 1.jpg'

    assert photoStorage.objectPathFromUrl(downloadUrl(path), 'u') == path
    for url in (downloadUrl(path).replace(BUCKET, 'other-bucket'),
                downloadUrl(path).replace('https://', 'http://'),
                'https://example.com/v0/b/x/o/y'):
        with pytest.raises(ValueError, match='^u is not a photo') as error:
            photoStorage.objectPathFromUrl(url, 'u')
        assert url not in str(error.value)


def test_updateMyStoreTheme_thenGetUserStore_carriesTheTheme(clean):
    withoutBrandColors(createTestStore(ADMIN))

    theme = api.updateMyStoreTheme(ADMIN, photoStyle='  soft light  ',
                                   primaryColor='#1a73e8')

    assertExactShape(StoreThemeResponse, theme)
    assert theme == {'photoStyle': 'soft light', 'primaryColor': '#1A73E8',
                     'secondaryColor': None}
    store = api.getUserStore(ADMIN)
    assertExactShape(MyStoreResponse, store)
    assert store['store']['photoStyle'] == 'soft light'
    assert 'secondaryColor' not in store['store']


def test_generateProductPhotos_savesEachPhotoReturned(fakes, monkeypatch):
    store = createTestStore(ADMIN)
    api.updateMyStoreTheme(ADMIN, photoStyle='white marble, morning light',
                           primaryColor='#112233')
    source = f'users/{ADMIN}/mug.jpg'
    fakes.bucket.objects[source] = {'data': JPEG, 'contentType': 'image/jpeg',
                                    'metadata': None}

    res = api.generateProductPhotos(ADMIN, [downloadUrl(source)],
                                    notes='from above')

    assertExactShape(GeneratedPhotosResponse, res)
    assert len(res['photos']) == PHOTOS_PER_CALL
    assert res['notGenerated'] is None
    calls = fakes.images.byN()
    assert sorted(calls) == [1, PHOTOS_THEMED]
    for call in calls.values():
        assert call['model'] == imageModel.IMAGE_MODEL
        assert call['image'] == [(JPEG, 'image/jpeg')]
        assert 'Special request: from above' in call['prompt']
    assert 'primary brand color #112233' in calls[1]['prompt']
    assert 'primary #112233' in calls[PHOTOS_THEMED]['prompt']
    assert 'Style:' not in calls[1]['prompt']
    assert calls[PHOTOS_THEMED]['prompt'].endswith(
        '\nStyle: white marble, morning light')
    # Photo 1 in the global prompt, photos 2 and 3 in the themed one.
    assert [p['prompt'] for p in res['photos']] == [
        calls[1]['prompt'], *[calls[PHOTOS_THEMED]['prompt']] * PHOTOS_THEMED]
    saved = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert {doc['_id'] for doc in saved} == {p['_id'] for p in res['photos']}
    prompts = {p['_id']: p['prompt'] for p in res['photos']}
    for doc in saved:
        assert doc['path'] == f'stores/{store["_id"]}/photos/{doc["_id"]}.jpg'
        assert fakes.bucket.objects[doc['path']]['data'] == JPEG
        assert doc['prompt'] == prompts[doc['_id']]
    assert all(u['if_generation_match'] == 0 for u in fakes.bucket.uploads)
    counts = {doc['_id'].split(':')[1]: doc['count']
              for doc in db.read({}, PhotoActions.PHOTO_USAGE)}
    assert counts == {store['_id']: PHOTOS_PER_CALL,
                      PhotoActions.ALL_STORES: PHOTOS_PER_CALL}


def test_generate_aStoreWithNoPhotoStyle_getsTheGlobalPhotoOnly(fakes):
    createTestStore(ADMIN)
    theme(primaryColor='#112233')
    url = putObject(fakes, MINE)

    res = api.generateProductPhotos(ADMIN, [url], notes='on a desk')

    assertExactShape(GeneratedPhotosResponse, res)
    [call] = fakes.images.calls
    assert call['n'] == 1 and 'Style:' not in call['prompt']
    assert 'Special request: on a desk' in call['prompt']
    assert 'primary #112233' in call['prompt']
    [photo] = res['photos']
    assert photo['prompt'] == call['prompt']
    assert res['notGenerated'] == PhotoActions.NO_STYLE_GLOBAL_ONLY
    assert usageCounts() == [1, 1]


def test_generate_aFailedThemedCall_keepsTheGlobalPhoto_andGivesTwoBack(
        fakes, frozen, monkeypatch, noSleep, caplog):
    store, url = themedStoreWithPhoto(fakes)

    class ThemedFails(FakeImages):
        def edit(self, **kwargs):
            if kwargs['n'] == PHOTOS_THEMED:
                self.calls.append(kwargs)
                raise sdkError(openai.BadRequestError, 400)
            return super().edit(**kwargs)

    images = useImages(monkeypatch, ThemedFails())

    with caplog.at_level(logging.WARNING):
        res = api.generateProductPhotos(ADMIN, [url])

    assert len(images.calls) == 2
    [photo] = res['photos']
    assert 'Style:' not in photo['prompt']
    assert res['notGenerated'] == PhotoActions.SOME_NOT_MADE
    assert counterValue(store['_id']) == 1
    assert counterValue(PhotoActions.ALL_STORES) == 1
    assert any(isinstance(r.msg, dict)
               and r.msg.get('event') == 'productPhotoCallFailed'
               for r in caplog.records)


def themedStoreWithPhoto(fakes, size=None) -> tuple[dict, str]:
    store = createTestStore(ADMIN)
    api.updateMyStoreTheme(ADMIN, photoStyle='plain')
    source = f'users/{ADMIN}/mug.jpg'
    fakes.bucket.objects[source] = {'data': JPEG, 'contentType': 'image/jpeg',
                                    'metadata': None}
    if size is not None:
        fakes.bucket.objects[source]['size'] = size
    return store, downloadUrl(source)


def usageCounts() -> list[int]:
    return [doc['count'] for doc in db.read({}, PhotoActions.PHOTO_USAGE)]


def test_allStoresCap_refusesAndGivesTheStoreCountBack(fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    monkeypatch.setenv(PhotoActions.PHOTO_DAILY_CAP_ALL_VARIABLE, '1')

    with pytest.raises(ValueError, match='^Photo generation is busy today'):
        api.generateProductPhotos(ADMIN, [url])

    assert fakes.images.calls == []
    assert usageCounts() == [0]


@pytest.mark.parametrize('raw', ['lots', '-3', '1.5'])
def test_dailyCapAll_fallsBackToTheDefaultOnABadValue(monkeypatch, caplog,
                                                       raw):
    monkeypatch.setenv(PhotoActions.PHOTO_DAILY_CAP_ALL_VARIABLE, raw)
    with caplog.at_level(logging.WARNING):
        assert (PhotoActions.dailyCapAll() ==
                PhotoActions.DEFAULT_PHOTO_DAILY_CAP_ALL)
    assert raw not in caplog.text


def test_aFailedLaterUpload_returnsTheSavedPhotos_andKeepsTheCount(fakes):
    store, url = themedStoreWithPhoto(fakes)
    fakes.bucket.uploadsThatWork = 1

    res = api.generateProductPhotos(ADMIN, [url])

    assertExactShape(GeneratedPhotosResponse, res)
    assert len(res['photos']) == 1
    [saved] = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert saved['_id'] == res['photos'][0]['_id']
    # All 3 photos came back from Meta (billed): none is given back.
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]


def test_noUploadWorks_raises_andRecordsNothing(fakes):
    _, url = themedStoreWithPhoto(fakes)
    fakes.bucket.uploadsThatWork = 0

    with pytest.raises(Exception, match='photo storage'):
        api.generateProductPhotos(ADMIN, [url])
    assert db.read({}, 'GeneratedPhotos') == []


def test_referencesOver25MbTogether_areRefusedBeforeAnyDownload(
        fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes, size=9 * 1024 * 1024)
    others = [f'users/{ADMIN}/b.jpg', f'users/{ADMIN}/c.jpg']
    for path in others:
        fakes.bucket.objects[path] = {'data': JPEG,
                                      'contentType': 'image/jpeg',
                                      'metadata': None,
                                      'size': 9 * 1024 * 1024}
    monkeypatch.setattr(FakeBlob, 'download_as_bytes',
                        lambda *a, **k: pytest.fail('downloaded'))

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN,
                                  [url, *map(downloadUrl, others)])
    assert str(error.value) == photoStorage.REFERENCES_TOO_LARGE
    assert usageCounts() == [0, 0]


def test_tooLittleTimeLeft_skipsTheModel_andGivesEverythingBack(
        fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    # The deadline is taken at the real now; every later read of the
    # route's clock is past the point the model may still start.
    lateBy = (PhotoActions.PHOTO_DEADLINE_SECONDS -
              PhotoActions.MODEL_MIN_SECONDS + 1)
    reads = []

    def monotonic():
        reads.append(1)
        return time.monotonic() + (lateBy if len(reads) > 1 else 0)

    monkeypatch.setattr(PhotoActions, 'time',
                        SimpleNamespace(monotonic=monotonic))

    with pytest.raises(Exception, match='photo generation'):
        api.generateProductPhotos(ADMIN, [url])
    assert fakes.images.calls == []
    assert usageCounts() == [0, 0]


@pytest.mark.parametrize('host, accepted', [
    ('127.0.0.1:9199', True), ('localhost:9199', True), ('[::1]:9199', True),
    ('http://127.0.0.1:9199', True), ('10.0.0.5:9199', False),
    ('storage.example.com:9199', False)])
def test_emulatorHost_isLoopbackOnly(monkeypatch, host, accepted):
    monkeypatch.delenv('STORAGE_EMULATOR_HOST', raising=False)
    monkeypatch.setenv('FIREBASE_STORAGE_EMULATOR_HOST', host)
    assert (photoStorage.emulatorHost() is not None) is accepted


def test_editImages_skipsABadImage_andLogsAnEmptyAnswer(monkeypatch, caplog):
    good = base64.b64encode(JPEG).decode()
    data = [SimpleNamespace(b64_json='not base64!!'),
            SimpleNamespace(b64_json=good)]
    fake = SimpleNamespace(edit=lambda **k: SimpleNamespace(data=data))
    monkeypatch.setattr(chatModel, 'client', lambda: modelClient(fake))
    with caplog.at_level(logging.WARNING):
        assert imageModel.editImages([], 'p', 2,
                                     time.monotonic() + 60) == [JPEG]
        data[1] = SimpleNamespace(b64_json=None)
        assert imageModel.editImages([], 'p', 2,
                                     time.monotonic() + 60) == []
    events = [r.msg.get('event') for r in caplog.records
              if isinstance(r.msg, dict)]
    assert 'imageModelBadImage' in events and 'imageModelEmpty' in events


@pytest.mark.parametrize('emulator', [None, '10.0.0.5:9199'])
def test_localdev_withoutALoopbackEmulator_isA503_neverRealCredentials(
        clean, monkeypatch, caplog, emulator):
    import google.auth
    from routeErrors import routeFailure

    def realCredentials(*args, **kwargs):
        raise AssertionError('google.auth.default must not be called')

    monkeypatch.setattr(google.auth, 'default', realCredentials)
    monkeypatch.setattr(photoStorage, '_client', None)
    if emulator is not None:
        monkeypatch.setenv('STORAGE_EMULATOR_HOST', emulator)
    createTestStore(ADMIN)
    api.updateMyStoreTheme(ADMIN, photoStyle='plain')

    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception) as error:
            api.generateProductPhotos(
                ADMIN, [downloadUrl(f'users/{ADMIN}/mug.jpg')])

    assert routeFailure(error.value, status=400).status == 503
    assert photoStorage._client is None
    [warning] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
                 and r.msg.get('event') == 'photoStorageNoLocalEmulator']
    assert warning['variable'] == (None if emulator is None
                                   else 'STORAGE_EMULATOR_HOST')
    assert '10.0.0.5' not in caplog.text
    assert usageCounts() == [0, 0]


def test_aRetriedUploadFinding412_isSaved(fakes, monkeypatch):
    from google.api_core.exceptions import PreconditionFailed
    store, url = themedStoreWithPhoto(fakes)
    def landedThenRetried(self, data, content_type, **kwargs):
        raise PreconditionFailed('the first attempt landed')

    monkeypatch.setattr(FakeBlob, 'upload_from_string', landedThenRetried)

    res = api.generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == PHOTOS_PER_CALL
    assert len(db.read({'storeId': store['_id']},
                       'GeneratedPhotos')) == PHOTOS_PER_CALL


def test_capAllZero_isPhotosTurnedOff(fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    monkeypatch.setenv(PhotoActions.PHOTO_DAILY_CAP_ALL_VARIABLE, '0')

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN, [url])
    assert str(error.value) == PhotoActions.PHOTOS_TURNED_OFF
    assert usageCounts() == []


def test_takeQuotas_aDatabaseErrorGivesBackWhatWasTaken(clean, monkeypatch):
    from pymongo.errors import PyMongoError
    from actions import usageCounters
    expireAt = FAR_EXPIRY
    real = usageCounters.takeQuota
    calls = []

    def failingSecond(collection, key, cap, expireAt, amount=1):
        calls.append(key)
        if len(calls) == 2:
            raise PyMongoError('down')
        return real(collection, key, cap, expireAt, amount)

    monkeypatch.setattr(usageCounters, 'takeQuota', failingSecond)
    with pytest.raises(PyMongoError):
        usageCounters.takeQuotas('PhotoUsage', [('a', 5, 'A'), ('b', 5, 'B')],
                                 expireAt, amount=3)
    assert usageCounts() == [0]


def test_giveBackQuotas_logsAndSwallowsAFailure(clean, monkeypatch, caplog):
    from pymongo.errors import PyMongoError
    from actions import usageCounters
    given = []

    def flaky(collection, key, amount=1):
        given.append(key)
        if key == 'a':
            raise PyMongoError('down')

    monkeypatch.setattr(usageCounters, 'giveBackQuota', flaky)
    with caplog.at_level(logging.WARNING):
        usageCounters.giveBackQuotas('PhotoUsage', ['a', 'b'])
    assert given == ['a', 'b']
    assert 'quotaRefundFailed' in caplog.text and 'down' not in caplog.text


# ======================= security-review coverage ======================= #

OTHER = 'ph-other-admin'
CUSTOMER = 'ph-customer'
NO_STORE_ADMIN = 'ph-no-store'
MINE = f'users/{ADMIN}/mug.jpg'
MAX = photoStorage.MAX_IMAGE_BYTES
FAR_NOW = datetime.datetime(2099, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)
FAR_DAY = datetime.date(2099, 6, 15)  # FAR_NOW in UTC+8 (20:00)
MODEL_URL = 'https://api.meta.ai/v1/images/edits'
SENTINEL_NOTES = 'SENTINEL-NOTES-TEXT'
SENTINEL_STYLE = 'SENTINEL-STYLE-TEXT'
LEAKY_ERROR = f'https://leak.example/{MINE} {SENTINEL_NOTES}'
JWT_SECRET = 'ph-http-test-jwt-secret-0123456789abcdefghij'


@pytest.fixture
def frozen(monkeypatch):
    """The photo route's clock stopped on FAR_DAY (far ahead, so no TTL
    monitor deletes a counter mid-test)."""
    monkeypatch.setattr(PhotoActions, '_now', lambda: FAR_NOW)


def counterKey(owner: str, day: datetime.date = FAR_DAY) -> str:
    return usageCounters.usageKey('photos', owner, day)


def counterValue(owner: str, day: datetime.date = FAR_DAY) -> int | None:
    doc = db.read({'_id': counterKey(owner, day)}, PhotoActions.PHOTO_USAGE,
                  findOne=True)
    return None if doc is None else doc['count']


def slotsFree() -> int:
    """How many photo slots are free (taken and given back again)."""
    got = 0
    while PhotoActions._photoSlots.acquire(blocking=False):
        got += 1
    for _ in range(got):
        PhotoActions._photoSlots.release()
    return got


def sdkError(kind, status=None, body=None):
    request = httpx.Request('POST', MODEL_URL)
    if kind is openai.APIConnectionError:
        return openai.APIConnectionError(request=request)
    if kind is openai.APITimeoutError:
        return openai.APITimeoutError(request=request)
    return kind(LEAKY_ERROR,
                response=httpx.Response(status, request=request),
                body=body)


def policyError(key='code'):
    return sdkError(openai.BadRequestError, 400,
                    {key: imageModel.CONTENT_POLICY_VIOLATION})


class ScriptedImages:
    """images.edit plays its outcomes in order: an exception is raised, a
    number is that many images returned."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        encoded = base64.b64encode(JPEG).decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)
                                     for _ in range(outcome)])


def useImages(monkeypatch, images):
    monkeypatch.setattr(chatModel, 'client', lambda: modelClient(images))
    return images


@pytest.fixture
def noSleep(monkeypatch):
    """chatModel's retry pause costs no time; the pauses are recorded."""
    slept = []
    monkeypatch.setattr(chatModel, 'time', SimpleNamespace(
        monotonic=time.monotonic, sleep=slept.append))
    return slept


def putObject(fakes, path, data=JPEG, **stored):
    fakes.bucket.objects[path] = {'data': data, 'contentType': 'image/jpeg',
                                  'metadata': None, **stored}
    return downloadUrl(path)


def emulatorUrl(path, host='127.0.0.1:9199', scheme='http', bucket=BUCKET):
    return (f'{scheme}://{host}/v0/b/{bucket}/o/{quote(path, safe="")}'
            '?alt=media&token=t')


def readOne(fakes, path=MINE, **stored):
    putObject(fakes, path, **stored)
    return photoStorage.readOwnImages(ADMIN, [('imageUrls[0]', path)],
                                      time.monotonic() + 60)


def httpPost(monkeypatch, route, identity, body):
    """POST to the generated Flask app with a JWT for `identity`:
    (status code, body)."""
    monkeypatch.setenv('JWT_SECRET_KEY', JWT_SECRET)

    def noGcp(*args, **kwargs):
        raise AssertionError('a test tried to create the Pub/Sub clients')

    monkeypatch.setattr(pubSubModule.pubSub, '_ensureClients', noGcp)
    flaskApp = importlib.import_module('app').app
    with flaskApp.app_context():
        token = create_access_token(identity=identity)
    res = flaskApp.test_client().post(
        f'/{route}', json=body, headers={'Authorization': f'Bearer {token}'})
    return res.status_code, res.get_json()


def theme(**fields) -> dict:
    return api.updateMyStoreTheme(ADMIN, **fields)


def storedTheme(store: dict) -> dict:
    doc = db.read({'_id': store['_id']}, 'Stores', findOne=True)
    return {field: doc.get(field) for field in STORE_THEME_FIELDS}


# ------------------------------ URL checks ------------------------------ #

HOSTILE_URLS = {
    'userinfo': downloadUrl(MINE).replace('https://', 'https://user:pw@'),
    'userinfoLookalike': downloadUrl(MINE).replace(
        'https://firebasestorage.googleapis.com',
        'https://firebasestorage.googleapis.com@evil.example'),
    'explicitPort': downloadUrl(MINE).replace(
        photoStorage.DOWNLOAD_HOST, f'{photoStorage.DOWNLOAD_HOST}:443'),
    'malformedPort': downloadUrl(MINE).replace(
        photoStorage.DOWNLOAD_HOST, f'{photoStorage.DOWNLOAD_HOST}:abc'),
    'rawSlashesInObject': (f'https://{photoStorage.DOWNLOAD_HOST}/v0/b/'
                           f'{BUCKET}/o/{MINE}?alt=media'),
    'notTheObjectEndpoint': downloadUrl(MINE).replace('/o/', '/x/'),
    'encodedTraversal': downloadUrl(f'users/{ADMIN}/../{OTHER}/a.jpg'),
    'encodedTraversalUp': downloadUrl(f'users/{ADMIN}/../../x/a.jpg'),
    'doubleEncodedTraversal': downloadUrl(
        f'users/{ADMIN}/../{OTHER}/a.jpg').replace('%2F', '%252F'),
    'otherUid': downloadUrl(f'users/{OTHER}/mug.jpg'),
    'prefixLookalike': downloadUrl(f'users/{ADMIN}X/mug.jpg'),
    'encodedBucketSuffix': downloadUrl(MINE).replace(BUCKET,
                                                     f'{BUCKET}%2Fx'),
    'upperCaseBucket': downloadUrl(MINE).replace(BUCKET, BUCKET.upper()),
    'emptySegment': downloadUrl(f'users/{ADMIN}//mug.jpg'),
    'dotSegment': downloadUrl(f'users/{ADMIN}/./mug.jpg'),
    'backslash': downloadUrl(f'users/{ADMIN}/a\\b.jpg'),
    'controlCharacter': downloadUrl(f'users/{ADMIN}/a\x01b.jpg'),
    'heicUpperCase': downloadUrl(f'users/{ADMIN}/mug.HEIC'),
    'heif': downloadUrl(f'users/{ADMIN}/mug.heif'),
    'storesFolder': downloadUrl('stores/s1/photos/p.jpg'),
}


@pytest.mark.parametrize('url', HOSTILE_URLS.values(),
                         ids=HOSTILE_URLS.keys())
def test_imageUrls_hostileUrl_refused_withoutQuotingIt(clean, url):
    with pytest.raises(ValueError) as error:
        PhotoActions._validImageUrls(ADMIN, [url])

    assert str(error.value).startswith('imageUrls[0]')
    assert url not in str(error.value)


@pytest.mark.parametrize('value', [5, None, ['u'], {'u': 1}, b'bytes'])
def test_imageUrls_aNonStringUrl_isRefused(clean, value):
    with pytest.raises(ValueError, match='^imageUrls.0. must be text'):
        PhotoActions._validImageUrls(ADMIN, [value])


def test_imageUrls_tokenQueryAndWhitespaceAreIgnored_nestedFolderIsMine(
        clean):
    nested = f'users/{ADMIN}/shop/mug.png'
    urls = [downloadUrl(MINE) + '&token=another&x=1#frag',
            f'  {downloadUrl(nested)}\n']

    assert PhotoActions._validImageUrls(ADMIN, urls) == [MINE, nested]


@pytest.mark.parametrize('envHost, url', [
    # the wrong port
    ('127.0.0.1:9199', emulatorUrl(MINE, host='127.0.0.1:9200')),
    # a host that is no loopback
    ('127.0.0.1:9199', emulatorUrl(MINE, host='10.0.0.5:9199')),
    # the emulator's own address, but a non-loopback emulator host is none
    ('10.0.0.5:9199', emulatorUrl(MINE, host='10.0.0.5:9199')),
    # https against the emulator
    ('127.0.0.1:9199', emulatorUrl(MINE, scheme='https')),
    ('127.0.0.1:9199', emulatorUrl(MINE).replace('http://',
                                                 'http://user@')),
    ('127.0.0.1:9199', emulatorUrl(MINE, bucket='other-bucket')),
    # no emulator set at all
    (None, emulatorUrl(MINE)),
])
def test_objectPathFromUrl_localdevEmulatorUrl_onlyOnTheLoopbackEmulator(
        clean, monkeypatch, envHost, url):
    if envHost is not None:
        monkeypatch.setenv('STORAGE_EMULATOR_HOST', envHost)

    with pytest.raises(ValueError, match='^u is not a photo') as error:
        photoStorage.objectPathFromUrl(url, 'u')
    assert url not in str(error.value)


def test_objectPathFromUrl_theEmulatorUrl_worksOnlyInLocalDev(clean,
                                                               monkeypatch):
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', '127.0.0.1:9199')
    for host in ('127.0.0.1:9199', 'localhost:9199'):
        assert photoStorage.objectPathFromUrl(emulatorUrl(MINE, host=host),
                                              'u') == MINE

    monkeypatch.setattr(photoStorage, '_isLocalDev', lambda: False)
    url = emulatorUrl(MINE)
    with pytest.raises(ValueError, match='^u is not a photo') as error:
        photoStorage.objectPathFromUrl(url, 'u')
    assert url not in str(error.value)
    # And the download URL is the only form left.
    assert photoStorage.objectPathFromUrl(downloadUrl(MINE), 'u') == MINE


@pytest.mark.parametrize('uid, path', [
    (ADMIN, f'users/{OTHER}/a.jpg'),
    (ADMIN, f'users/{ADMIN}X/a.jpg'),
    (ADMIN, f'users/{ADMIN}'),
    (ADMIN, f'users/{ADMIN}/'),
    (ADMIN, f'users/{ADMIN}/../a.jpg'),
    (ADMIN, f'users/{ADMIN}/.'),
    (ADMIN, f'users//{ADMIN}/a.jpg'),
    (ADMIN, f'users/{ADMIN}//a.jpg'),
    (ADMIN, f'/users/{ADMIN}/a.jpg'),
    (ADMIN, f'stores/{ADMIN}/a.jpg'),
    (ADMIN, f'users/{ADMIN}/a\\b.jpg'),
    (ADMIN, f'users/{ADMIN}/a\x00b.jpg'),
    (ADMIN, f'users/{ADMIN}/a\nb.jpg'),
    (ADMIN, 5),
    (ADMIN, None),
    ('', 'users//a.jpg'),
    ('a/b', 'users/a/b/c.jpg'),
    (None, 'users/None/a.jpg'),
    (5, 'users/5/a.jpg'),
])
def test_ownImagePath_refusesWhatIsNotUnderTheCallersFolder(uid, path):
    with pytest.raises(ValueError,
                       match='^imageUrls.0. must be a photo you uploaded'):
        photoStorage.ownImagePath(uid, path, 'imageUrls[0]')


@pytest.mark.parametrize('name', ['a.HEIC', 'a.heic', 'a.HeIf', 'a.heif'])
def test_ownImagePath_heicByName_asksToConvert(name):
    path = f'users/{ADMIN}/{name}'

    with pytest.raises(ValueError) as error:
        photoStorage.ownImagePath(ADMIN, path, 'imageUrls[0]')
    assert photoStorage.CONVERT_PHOTO in str(error.value)
    assert path not in str(error.value)


# ----------------------------- photo checks ----------------------------- #


@pytest.mark.parametrize('stored', [
    {'size': MAX},
    {'contentType': 'image/JPEG; charset=x'},
    {'contentType': 'image/png', 'data': b'\x89PNG\r\n\x1a\n' + b'png'},
    {'data': b'\xff\xd8\xff' + b'x' * (MAX - 3), 'size': MAX},
    {'contentType': 'image/webp', 'data': b'RIFF\x00\x00\x00\x00WEBPVP8 '},
])
def test_readOwnImages_acceptsTheBoundaries(fakes, stored):
    [(data, contentType)] = readOne(fakes, **stored)

    assert len(data) == len(stored.get('data', JPEG))
    assert contentType in photoStorage.IMAGE_CONTENT_TYPES


@pytest.mark.parametrize('stored, message', [
    ({'size': MAX + 1}, 'is over 10 MB'),
    ({'size': None}, 'is over 10 MB'),
    # The metadata understates it: the read bound (end + 1 byte) catches it.
    ({'data': b'x' * (MAX + 5), 'size': 10}, 'is over 10 MB'),
    ({'contentType': None}, 'must be a JPG, PNG or WebP photo'),
    ({'contentType': 'image/gif'}, 'must be a JPG, PNG or WebP photo'),
    ({'contentType': 'application/pdf'}, 'must be a JPG, PNG or WebP photo'),
    # A WebP's RIFF header without its WEBP mark (a WAV, say) is no WebP.
    ({'contentType': 'image/webp', 'data': b'RIFF\x00\x00\x00\x00WAVE'},
     photoStorage.NOT_A_REAL_PHOTO),
    ({'contentType': 'image/heic'}, photoStorage.CONVERT_PHOTO),
    ({'contentType': 'image/HEIF; charset=x'}, photoStorage.CONVERT_PHOTO),
])
def test_readOwnImages_refusesABadObject_withoutQuotingItsPath(fakes, stored,
                                                               message):
    with pytest.raises(ValueError) as error:
        readOne(fakes, **stored)

    assert message in str(error.value)
    assert str(error.value).startswith('imageUrls[0]')
    assert MINE not in str(error.value)


def webp(mode: str) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    color = (10, 200, 30, 128) if mode == 'RGBA' else (10, 200, 30)
    Image.new(mode, (4, 3), color).save(out, 'WEBP')
    return out.getvalue()


def test_modelImage_aWebP_becomesAJpeg_orAPngWithTransparency():
    """Meta does not say it takes WebP: a WebP goes to it as a JPEG, or a
    PNG when it has transparency; a JPEG or PNG goes as it is."""
    from PIL import Image
    opaque, clear = webp('RGB'), webp('RGBA')

    data, contentType = photoStorage.modelImage(opaque, 'image/webp', 'x')
    assert contentType == 'image/jpeg' and data.startswith(b'\xff\xd8\xff')
    data, contentType = photoStorage.modelImage(clear, 'image/webp', 'x')
    assert contentType == 'image/png'
    with Image.open(io.BytesIO(data)) as image:
        assert image.mode == 'RGBA' and image.size == (4, 3)
    assert photoStorage.modelImage(JPEG, 'image/jpeg', 'x') == (
        JPEG, 'image/jpeg')


@pytest.mark.parametrize('data', [
    b'RIFF\x00\x00\x00\x00WEBPVP8 broken', JPEG])
def test_modelImage_aWebPThatCannotBeRead_isRefused_namingItsPlace(data):
    with pytest.raises(ValueError) as error:
        photoStorage.modelImage(data, 'image/webp', 'imageUrls[1]')

    assert str(error.value) == (
        f'imageUrls[1] {photoStorage.NOT_A_REAL_PHOTO}')


def test_references_aWebP_goesToTheModelAsAJpeg():
    [(name, data, contentType)] = PhotoActions._references(
        [(webp('RGB'), 'image/webp')])

    assert (name, contentType) == ('reference0.jpg', 'image/jpeg')
    assert data.startswith(b'\xff\xd8\xff')


def test_storePhotoPath_aWebPOriginal_isSavedAsWebp():
    assert photoStorage.storePhotoPath('s1', 'p1', 'image/webp') == (
        'stores/s1/photos/p1.webp')


def test_readOwnImages_downloadsAtMostOneByteOverTheLimit(fakes, monkeypatch):
    seen = []
    real = FakeBlob.download_as_bytes

    def spy(self, end, **kwargs):
        seen.append(end)
        return real(self, end, **kwargs)

    monkeypatch.setattr(FakeBlob, 'download_as_bytes', spy)

    readOne(fakes)

    # The SDK's end is inclusive: end=MAX is at most MAX + 1 bytes.
    assert seen == [MAX]


def test_readOwnImages_aMissingObject_isAValueError(fakes):
    with pytest.raises(ValueError, match='^imageUrls.0. was not found') as e:
        photoStorage.readOwnImages(ADMIN, [('imageUrls[0]', MINE)],
                                   time.monotonic() + 60)
    assert MINE not in str(e.value)


def test_readOwnImages_anObjectGoneBeforeTheDownload_isAValueError(
        fakes, monkeypatch):
    from google.api_core.exceptions import NotFound

    def gone(self, end, **kwargs):
        raise NotFound('gone')

    monkeypatch.setattr(FakeBlob, 'download_as_bytes', gone)

    with pytest.raises(ValueError, match='^imageUrls.0. was not found'):
        readOne(fakes)


@pytest.mark.parametrize('where', ['metadata', 'download'])
def test_readOwnImages_aStorageError_isA503(fakes, monkeypatch, caplog, where):
    from google.api_core.exceptions import ServiceUnavailable as GoogleDown
    error = GoogleDown('SENTINEL-STORAGE-DETAIL')

    def fail(*args, **kwargs):
        raise error

    putObject(fakes, MINE)
    if where == 'metadata':
        monkeypatch.setattr(FakeBucket, 'get_blob', fail)
    else:
        monkeypatch.setattr(FakeBlob, 'download_as_bytes', fail)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ServiceUnavailable) as raised:
            photoStorage.readOwnImages(ADMIN, [('imageUrls[0]', MINE)],
                                       time.monotonic() + 60)

    assert routeFailure(raised.value, status=400).status == 503
    assert 'SENTINEL-STORAGE-DETAIL' not in caplog.text
    assert MINE not in caplog.text


def test_readOwnImages_aPassedDeadline_isA503(fakes):
    putObject(fakes, MINE)

    with pytest.raises(ServiceUnavailable):
        photoStorage.readOwnImages(ADMIN, [('imageUrls[0]', MINE)],
                                   time.monotonic() - 1)


# --------------------------- the imageUrls list --------------------------- #


@pytest.mark.parametrize('imageUrls', [[], 'x', None, {}, 5])
def test_generate_imageUrlsShape_refused(fakes, imageUrls):
    themedStoreWithPhoto(fakes)

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN, imageUrls)

    assert str(error.value) == PhotoActions.IMAGE_URLS_COUNT
    assert usageCounts() == [] and fakes.images.calls == []


def test_generate_tenUrlsWork_elevenAreRefused(fakes):
    themedStoreWithPhoto(fakes)
    urls = [putObject(fakes, f'users/{ADMIN}/p{i}.jpg') for i in range(11)]

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN, urls)
    assert str(error.value) == PhotoActions.IMAGE_URLS_COUNT
    assert usageCounts() == [] and fakes.images.calls == []

    res = api.generateProductPhotos(ADMIN, urls[:10])
    assert len(res['photos']) == PHOTOS_PER_CALL
    assert len(fakes.images.calls) == 2
    for call in fakes.images.calls:
        assert len(call['image']) == 10


@pytest.mark.parametrize('second', [
    downloadUrl(MINE),
    # Two tokens of one object.
    downloadUrl(MINE).replace('token=t', 'token=another'),
    # The same path spelled with lowercase hex.
    downloadUrl(MINE).replace('%2F', '%2f'),
], ids=['same', 'otherToken', 'lowerHex'])
def test_generate_theSamePhotoTwice_isRefused_beforeAnyQuota(fakes, second):
    themedStoreWithPhoto(fakes)

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN, [downloadUrl(MINE), second])

    assert str(error.value) == PhotoActions.IMAGE_URLS_TWICE
    assert usageCounts() == [] and fakes.images.calls == []


def test_generate_overHttp_alwaysMakes3_aCountSentIsIgnored(fakes,
                                                            monkeypatch):
    _, url = themedStoreWithPhoto(fakes)

    status, body = httpPost(monkeypatch, 'generateProductPhotos', ADMIN,
                            {'userId': ADMIN, 'imageUrls': [url],
                             'count': 1})

    assert status == 200
    assert sorted(fakes.images.byN()) == [1, PHOTOS_THEMED]
    assert len(body['data']['photos']) == PHOTOS_PER_CALL


def test_generate_notes_whitespaceIsNone_overLengthAndNonTextRefused(fakes):
    store, url = themedStoreWithPhoto(fakes)

    api.generateProductPhotos(ADMIN, [url], notes='  \n ')
    for call in fakes.images.calls:
        assert 'Special request:' not in call['prompt']
    [saved, *_] = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert saved['notes'] is None

    before = usageCounts()
    for notes in ('n' * (PHOTO_NOTES_MAX_LENGTH + 1), 5, ['x']):
        with pytest.raises(ValueError):
            api.generateProductPhotos(ADMIN, [url], notes=notes)
    assert usageCounts() == before and len(fakes.images.calls) == 2


@pytest.mark.parametrize('clear', [None, '', '   '])
def test_generate_withoutAPhotoStyle_makesTheGlobalPhotoOnly(fakes, clear):
    if clear is None:
        createTestStore(ADMIN)
        url = putObject(fakes, MINE)
    else:
        _, url = themedStoreWithPhoto(fakes)
        theme(photoStyle=clear)

    res = api.generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == 1
    assert res['notGenerated'] == PhotoActions.NO_STYLE_GLOBAL_ONLY
    [call] = fakes.images.calls
    assert call['n'] == 1 and 'Style:' not in call['prompt']
    assert usageCounts() == [1, 1]


# -------------------------------- access -------------------------------- #


@pytest.mark.parametrize('route, caller, status, message', [
    ('generateProductPhotos', 'customer', 403, 'Only a store admin'),
    ('updateMyStoreTheme', 'customer', 403, 'Only a store admin'),
    ('generateProductPhotos', 'noStore', 400, NO_STORE_YET),
    ('updateMyStoreTheme', 'noStore', 400, NO_STORE_YET),
])
def test_access_aCustomerOrAnAdminWithoutAStore_isRefused(
        fakes, monkeypatch, route, caller, status, message):
    store, url = themedStoreWithPhoto(fakes)
    createTestUser(NO_STORE_ADMIN)
    identity = CUSTOMER if caller == 'customer' else NO_STORE_ADMIN
    body = ({'imageUrls': [url]} if route == 'generateProductPhotos'
            else {'photoStyle': 'x'})

    code, res = httpPost(monkeypatch, route, identity,
                         {'userId': identity, **body})

    assert code == status and res['status'] == status
    assert message in res['message'] and res['data'] is None
    assert usageCounts() == [] and fakes.images.calls == []
    assert storedTheme(store)['photoStyle'] == 'plain'


def test_access_aBodyUserIdThatIsNotTheJwtIdentity_is400(fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    createTestStore(OTHER, name='Bakery B')

    code, res = httpPost(monkeypatch, 'generateProductPhotos', OTHER,
                         {'userId': ADMIN, 'imageUrls': [url]})

    assert code == 400 and 'Unauthorized access' in res['message']
    assert usageCounts() == [] and fakes.images.calls == []


# ---------------------------- logs and errors ---------------------------- #


def assertNothingSensitiveLogged(caplog, *extra):
    for text in (MINE, 'mug.jpg', SENTINEL_NOTES, SENTINEL_STYLE,
                 'leak.example', 'token=', *extra):
        assert text not in caplog.text, text


def test_generate_logsNeverAUrlPathNotesOrStyle(fakes, caplog):
    store = createTestStore(ADMIN)
    theme(photoStyle=SENTINEL_STYLE)
    url = putObject(fakes, MINE)

    with caplog.at_level(logging.INFO):
        res = api.generateProductPhotos(ADMIN, [url], notes=SENTINEL_NOTES)

    assertNothingSensitiveLogged(caplog, url,
                                 *(p['url'] for p in res['photos']))
    [line] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
              and r.msg.get('event') == 'productPhotos']
    assert line['storeId'] == store['_id']
    assert line['count'] == PHOTOS_PER_CALL
    assert line['returned'] == PHOTOS_PER_CALL and 'durationMs' in line


@pytest.mark.parametrize('key', ['code', 'type'])
def test_generate_contentPolicyViolation_isTheFixed400_notRetried(
        fakes, monkeypatch, noSleep, caplog, frozen, key):
    store = createTestStore(ADMIN)
    theme(photoStyle=SENTINEL_STYLE)
    url = putObject(fakes, MINE)
    # Both calls (the global and the themed prompt) are refused.
    images = useImages(monkeypatch, ScriptedImages(policyError(key),
                                                   policyError(key)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError) as error:
            api.generateProductPhotos(ADMIN, [url], notes=SENTINEL_NOTES)

    assert str(error.value) == imageModel.PHOTO_REQUEST_DECLINED
    assert len(images.calls) == 2 and noSleep == []
    assert routeFailure(error.value, status=400).status == 400
    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE
    assertNothingSensitiveLogged(caplog, url)


def edit(deadline=None):
    return imageModel.editImages([('r.jpg', JPEG, 'image/jpeg')], 'prompt', 1,
                                 deadline or time.monotonic() + 60)


def test_editImages_sendsThePhotosAsDataUrls_inAJsonBody(monkeypatch):
    """The real SDK's request: Meta ignores multipart image files (under any
    field name), so the photos go in a JSON body's `images`, each a data
    URL of its bytes and content type, in order."""
    sent = []

    def answer(request):
        sent.append(request)
        encoded = base64.b64encode(JPEG).decode()
        return httpx.Response(200, json={'created': 0,
                                         'data': [{'b64_json': encoded}]})

    client = openai.OpenAI(
        base_url=chatModel.CHAT_BASE_URL, api_key='test-key', max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(answer)))
    monkeypatch.setattr(chatModel, 'client', lambda: client)
    PNG = b'\x89PNG\r\n\x1a\nsecond'

    assert imageModel.editImages([('reference0.jpg', JPEG, 'image/jpeg'),
                                  ('reference1.png', PNG, 'image/png')],
                                 'prompt', 2,
                                 time.monotonic() + 60) == [JPEG]

    [request] = sent
    assert str(request.url) == MODEL_URL
    assert request.headers['content-type'] == 'application/json'
    raw = request.read()
    assert b'multipart' not in raw and b'image[' not in raw
    assert json.loads(raw) == {
        'model': imageModel.IMAGE_MODEL, 'prompt': 'prompt', 'n': 2,
        'size': imageModel.IMAGE_SIZE,
        'output_format': imageModel.IMAGE_OUTPUT_FORMAT,
        'response_format': 'b64_json',
        'images': [
            {'image_url': 'data:image/jpeg;base64,'
                          + base64.b64encode(JPEG).decode()},
            {'image_url': 'data:image/png;base64,'
                          + base64.b64encode(PNG).decode()}]}


@pytest.mark.parametrize('first', [
    (openai.InternalServerError, 500), (openai.InternalServerError, 504),
    (openai.RateLimitError, 429), (openai.APIConnectionError, None),
    (openai.APITimeoutError, None)],
    ids=['500', '504', '429', 'connection', 'timeout'])
def test_editImages_aRetryableError_isRetriedOnce(monkeypatch, noSleep,
                                                  caplog, first):
    images = useImages(monkeypatch, ScriptedImages(sdkError(*first), 1))

    with caplog.at_level(logging.INFO):
        assert edit() == [JPEG]

    assert len(images.calls) == 2
    assert noSleep == [chatModel.RETRY_BACKOFF_SECONDS]
    assertNothingSensitiveLogged(caplog)


def test_editImages_twoServerErrorsInARow_is503_afterTwoCalls(
        monkeypatch, noSleep, caplog):
    images = useImages(monkeypatch, ScriptedImages(
        sdkError(openai.InternalServerError, 500),
        sdkError(openai.InternalServerError, 502)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(ServiceUnavailable) as error:
            edit()

    assert len(images.calls) == 2 and len(noSleep) == 1
    assert error.value.service == imageModel.PHOTO_SERVICE
    assertNothingSensitiveLogged(caplog)


@pytest.mark.parametrize('kind, status', [
    (openai.BadRequestError, 400), (openai.AuthenticationError, 401),
    (openai.PermissionDeniedError, 403), (openai.NotFoundError, 404),
    (openai.UnprocessableEntityError, 422)])
def test_editImages_otherClientErrors_areNotRetried_andAre503(
        monkeypatch, noSleep, caplog, kind, status):
    images = useImages(monkeypatch, ScriptedImages(sdkError(kind, status),
                                                   1))

    with caplog.at_level(logging.INFO):
        with pytest.raises(ServiceUnavailable):
            edit()

    assert len(images.calls) == 1 and noSleep == []
    assertNothingSensitiveLogged(caplog)


@pytest.mark.parametrize('body', [None, {}, {'code': 'invalid_size'},
                                  {'type': 'invalid_request'}])
def test_editImages_aOther400_isNotAPolicyRefusal(monkeypatch, noSleep, body):
    useImages(monkeypatch,
              ScriptedImages(sdkError(openai.BadRequestError, 400, body)))

    with pytest.raises(ServiceUnavailable):
        edit()


def test_editImages_eachAttemptWaitsAtMostTheTimeLeft(monkeypatch, noSleep):
    images = useImages(monkeypatch, ScriptedImages(1, 1))

    edit(time.monotonic() + 600)
    edit(time.monotonic() + 10)

    first, second = (call['timeout'] for call in images.calls)
    assert first == imageModel.IMAGE_TIMEOUT_SECONDS
    assert 0 < second <= 10


def test_editImages_aPassedDeadline_makesNoCall(monkeypatch, noSleep):
    images = useImages(monkeypatch, ScriptedImages(1))

    with pytest.raises(ServiceUnavailable):
        edit(time.monotonic() - 1)
    assert images.calls == []


def test_generate_aMissingModelApiKey_is503_andRefunded(
        clean, frozen, monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.delenv(chatModel.API_KEY_VARIABLE, raising=False)
    monkeypatch.setattr(chatModel, '_client', None)
    store, url = themedStoreWithPhoto(SimpleNamespace(bucket=bucket))

    with pytest.raises(ServiceUnavailable) as error:
        api.generateProductPhotos(ADMIN, [url])

    assert error.value.service == imageModel.PHOTO_SERVICE
    assert routeFailure(error.value, status=400).status == 503
    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE
    assert db.read({}, 'GeneratedPhotos') == []


# ------------------------------- the theme ------------------------------- #

FULL_THEME = {'photoStyle': 'old style', 'primaryColor': '#111111',
              'secondaryColor': '#222222'}


@pytest.mark.parametrize('field, sent, stored', [
    ('photoStyle', 'new style', 'new style'),
    ('photoStyle', '', None),
    ('photoStyle', '   ', None),
    ('primaryColor', '#abcdef', '#ABCDEF'),
    ('primaryColor', '', None),
    ('secondaryColor', ' #AbCdEf ', '#ABCDEF'),
    ('secondaryColor', '', None),
    ('secondaryColor', '  ', None),
])
def test_theme_aFieldIsSetOrClearedAlone_theOthersAreUntouched(
        clean, field, sent, stored):
    store = createTestStore(ADMIN)
    theme(**FULL_THEME)

    res = theme(**{field: sent})

    assertExactShape(StoreThemeResponse, res)
    expected = {**FULL_THEME, field: stored}
    assert res == expected
    assert storedTheme(store) == expected


def test_theme_nothingSent_isRefused_andChangesNothing(clean):
    store = createTestStore(ADMIN)
    theme(**FULL_THEME)
    before = db.read({'_id': store['_id']}, 'Stores', findOne=True)

    with pytest.raises(ValueError) as error:
        theme()

    assert str(error.value) == NO_THEME_SENT
    assert db.read({'_id': store['_id']}, 'Stores', findOne=True) == before


def test_theme_aStyleOf300CharactersWorks_301IsRefused(clean):
    store = createTestStore(ADMIN)

    assert theme(photoStyle='x' * PHOTO_STYLE_MAX_LENGTH)['photoStyle']
    for style in ('y' * (PHOTO_STYLE_MAX_LENGTH + 1), 'a\x00b', 5, ['x']):
        with pytest.raises(ValueError) as error:
            theme(photoStyle=style)
        assert 'yyy' not in str(error.value)
    assert storedTheme(store)['photoStyle'] == 'x' * PHOTO_STYLE_MAX_LENGTH


@pytest.mark.parametrize('color', [
    '1A73E8', '#12345', '#1234567', '#GGGGGG', '##123456', 'red',
    'rgb(1,2,3)', '#SENTINEL', '＃112233', 5, 1.5, True, ['#112233'],
    {'c': '#112233'}])
@pytest.mark.parametrize('field', ['primaryColor', 'secondaryColor'])
def test_theme_aBadColor_isRefused_andNothingIsSaved(clean, field, color):
    store = withoutBrandColors(createTestStore(ADMIN))

    with pytest.raises(ValueError) as error:
        # The good style rides along: a refusal saves none of it.
        theme(photoStyle='fine', **{field: color})

    assert str(error.value) == f'{field} must be a hex color like #1A73E8'
    assert storedTheme(store) == {f: None for f in STORE_THEME_FIELDS}


def test_theme_colorIsStoredUppercase(clean):
    store = createTestStore(ADMIN)

    res = theme(primaryColor='#1a73e8', secondaryColor='#00ff7f')

    assert res['primaryColor'] == '#1A73E8'
    assert res['secondaryColor'] == '#00FF7F'
    assert storedTheme(store)['primaryColor'] == '#1A73E8'


def test_theme_onlyTheCallersOwnStoreChanges(clean):
    mine = createTestStore(ADMIN)
    theme(**FULL_THEME)
    theirs = createTestStore(OTHER, name='Bakery B')

    api.updateMyStoreTheme(OTHER, photoStyle='their style')

    assert storedTheme(mine) == FULL_THEME
    assert storedTheme(theirs)['photoStyle'] == 'their style'


def test_theme_aCustomerOrAnAdminWithoutAStore_changesNothing(clean):
    store = withoutBrandColors(createTestStore(ADMIN))
    createTestUser(NO_STORE_ADMIN)

    with pytest.raises(PermissionError):
        api.updateMyStoreTheme(CUSTOMER, photoStyle='x')
    with pytest.raises(ValueError, match=NO_STORE_YET):
        api.updateMyStoreTheme(NO_STORE_ADMIN, photoStyle='x')
    assert storedTheme(store) == {f: None for f in STORE_THEME_FIELDS}


def test_theResponsesACustomerCanSee_carryNoThemeField(clean):
    store = createTestStore(ADMIN)
    theme(**FULL_THEME)

    located = api.setUserStoreLocation(ADMIN, 14.6, 121.0)

    assertExactShape(StoreResponse, located)
    assert not set(STORE_THEME_FIELDS) & set(located)
    assert not set(STORE_THEME_FIELDS) & modelKeys(StoreResponse)
    assert set(STORE_THEME_FIELDS) <= modelKeys(AdminStoreResponse)
    # Only the owner's own view has them.
    mine = api.getUserStore(ADMIN)['store']
    assert {f: mine[f] for f in STORE_THEME_FIELDS} == FULL_THEME
    assert store['_id'] == mine['_id']


def test_theOwnersView_ofAStoreWithNoTheme_leavesTheFieldsOut(clean):
    withoutBrandColors(createTestStore(ADMIN))

    mine = api.getUserStore(ADMIN)['store']

    assert not set(STORE_THEME_FIELDS) & set(mine)


@pytest.mark.parametrize('colors, expected, absent', [
    ({}, [], ['Brand colors', 'primary', 'secondary']),
    ({'primaryColor': '#112233'}, ['Brand colors: primary #112233'],
     ['secondary']),
    ({'secondaryColor': '#445566'}, ['Brand colors: secondary #445566'],
     ['primary']),
    ({'primaryColor': '#112233', 'secondaryColor': '#445566'},
     ['Brand colors: primary #112233, secondary #445566'], []),
])
def test_photoPrompt_hasTheColorsLineOnlyWhenAColorIsSet(colors, expected,
                                                         absent):
    prompt = PhotoActions.photoPrompt({'photoStyle': 'calm', **colors}, None)

    assert prompt.startswith(PhotoActions.PHOTO_INSTRUCTION)
    assert 'Style: calm' in prompt and 'Special request:' not in prompt
    for text in expected:
        assert text in prompt
    for text in absent:
        assert text not in prompt


def test_photoPrompt_namesTheProduct_onlyWhenItsNameIsKnown():
    store = {'photoStyle': 'calm'}

    named = PhotoActions.photoPrompt(store, None, 'Blue Mug').split('\n')
    unnamed = PhotoActions.photoPrompt(store, None)

    assert named == [PhotoActions.PHOTO_INSTRUCTION, 'Product: Blue Mug',
                     'Style: calm']
    assert 'Product:' not in unnamed


def test_photoPrompts_theGlobalPromptsLines_thenTheStoreThemeLast():
    store = {'photoStyle': 'calm', 'primaryColor': '#112233',
             'secondaryColor': '#445566'}

    [(globalPrompt, one), (themed, two)] = PhotoActions.photoPrompts(
        store, 'on a desk', 'Blue Mug')

    assert (one, two) == (1, PHOTOS_THEMED)
    assert globalPrompt.split('\n') == [
        PhotoActions.PHOTO_INSTRUCTION,
        'Special request: on a desk',
        'Product: Blue Mug',
        PhotoActions.backgroundLine('#112233')]
    assert themed.split('\n') == [
        PhotoActions.PHOTO_INSTRUCTION,
        'Special request: on a desk',
        'Product: Blue Mug',
        PhotoActions.brandColorsLine('#112233', '#445566'),
        'Style: calm']


def test_globalPhotoPrompt_isTheProductOnAPlainPrimaryColorBackground():
    prompt = PhotoActions.globalPhotoPrompt(
        {'primaryColor': '#112233', 'secondaryColor': '#445566'}, None)

    assert prompt.split('\n') == [PhotoActions.PHOTO_INSTRUCTION,
                                  PhotoActions.backgroundLine('#112233')]
    assert 'plain, seamless background' in prompt
    assert 'primary brand color #112233' in prompt
    assert 'Show only the product' in prompt
    assert '#445566' not in prompt and 'Brand colors:' not in prompt


def test_globalPhotoPrompt_withoutAPrimaryColor_keepsTheBrandColorsLine():
    prompt = PhotoActions.globalPhotoPrompt({'secondaryColor': '#445566'},
                                            None)

    assert prompt.split('\n') == [
        PhotoActions.PHOTO_INSTRUCTION,
        PhotoActions.brandColorsLine(None, '#445566')]
    assert 'Background:' not in prompt


def test_themedPrompts_neverGetThePlainBackground():
    prompt = PhotoActions.photoPrompt(
        {'photoStyle': 'calm', 'primaryColor': '#112233'}, None)

    assert 'Background:' not in prompt
    assert prompt.endswith('\nStyle: calm')


def test_photoPrompts_aStoreWithNoPhotoStyle_getsTheGlobalPromptOnly():
    for style in (None, ''):
        [(prompt, n)] = PhotoActions.photoPrompts({'photoStyle': style},
                                                  None)
        assert n == 1 and prompt == PhotoActions.PHOTO_INSTRUCTION


def test_photoInstruction_asksForTheProductOnly_withoutWhatSitsBesideIt():
    instruction = PhotoActions.PHOTO_INSTRUCTION

    assert 'Show only the product' in instruction
    assert 'items placed beside it for scale' in instruction


def test_generate_productName_isInThePrompt_strippedAndBlankIsNone(fakes):
    _, url = themedStoreWithPhoto(fakes)

    api.generateProductPhotos(ADMIN, [url], productName='  Blue Mug ')
    for blank in ('', '   '):
        api.generateProductPhotos(ADMIN, [url], productName=blank)
    api.generateProductPhotos(ADMIN, [url])

    named, *unnamed = [call['prompt'] for call in fakes.images.calls]
    assert 'Product: Blue Mug\n' in named
    # Each call's two prompts name it (or not) alike.
    assert 'Product: Blue Mug\n' in unnamed.pop(0)
    assert len(unnamed) == 6
    assert all('Product:' not in prompt for prompt in unnamed)


def test_generate_productName_overLengthOrNotText_isRefused_beforeAnyQuota(
        fakes):
    _, url = themedStoreWithPhoto(fakes)

    for productName in ('n' * (PRODUCT_NAME_MAX_LENGTH + 1), 5, ['x'],
                        'Mug\x00'):
        with pytest.raises(ValueError) as error:
            api.generateProductPhotos(ADMIN, [url], productName=productName)
        assert str(error.value).startswith(PhotoActions.PRODUCT_NAME_LABEL)
    assert usageCounts() == [] and fakes.images.calls == []
    api.generateProductPhotos(ADMIN, [url],
                              productName='n' * PRODUCT_NAME_MAX_LENGTH)
    assert len(fakes.images.calls) == 2


def test_generate_withNoColors_thePromptHasNoColorsLine(fakes):
    store, url = themedStoreWithPhoto(fakes)
    withoutBrandColors(store)

    api.generateProductPhotos(ADMIN, [url])

    assert len(fakes.images.calls) == 2
    for call in fakes.images.calls:
        assert 'Brand colors' not in call['prompt']


def test_mcp_generateProductPhotos_hasNoUserIdInItsSchema():
    async def schemas():
        tools = await mcpServer.servers['admin'].list_tools()
        return {t.name: t.input_schema for t in tools}

    byName = asyncio.run(schemas())

    assert set(byName['generateProductPhotos']['properties']) == {
        'imageUrls', 'notes', 'productName'}
    assert 'userId' not in byName['updateMyStoreTheme']['properties']


# -------------------------------- quota --------------------------------- #


def test_quota_98Used_aRequestFor3IsRefused_andTheSlotIsReleased(fakes,
                                                                  frozen):
    store, url = themedStoreWithPhoto(fakes)
    usageCounters.takeQuota(PhotoActions.PHOTO_USAGE,
                            counterKey(store['_id']),
                            PhotoActions.STORE_DAILY_PHOTO_CAP, FAR_EXPIRY,
                            amount=98)

    with pytest.raises(ValueError) as error:
        api.generateProductPhotos(ADMIN, [url])

    assert str(error.value) == PhotoActions.PHOTO_CAP_REACHED
    assert counterValue(store['_id']) == 98
    assert counterValue(PhotoActions.ALL_STORES) is None
    assert fakes.images.calls == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE

    # The slot is free again, and the 3 left (97 used) are still there to
    # take.
    usageCounters.giveBackQuota(PhotoActions.PHOTO_USAGE,
                                counterKey(store['_id']), 1)
    assert len(api.generateProductPhotos(ADMIN, [url])['photos']) == 3
    assert counterValue(store['_id']) == 100
    with pytest.raises(ValueError) as again:
        api.generateProductPhotos(ADMIN, [url])
    assert str(again.value) == PhotoActions.PHOTO_CAP_REACHED
    assert counterValue(store['_id']) == 100
    assert counterValue(PhotoActions.ALL_STORES) == 3


def test_slots_bothHeld_theNextCallIs503_beforeAnyQuotaOrRead(fakes, frozen,
                                                              monkeypatch):
    store, url = themedStoreWithPhoto(fakes)
    monkeypatch.setattr(photoStorage, '_bucket',
                        lambda: pytest.fail('storage read'))
    slots = PhotoActions._photoSlots
    held = 0
    while slots.acquire(blocking=False):
        held += 1
    try:
        assert held == PhotoActions.PHOTO_CALLS_AT_ONCE
        with pytest.raises(ServiceUnavailable) as error:
            api.generateProductPhotos(ADMIN, [url])
        assert error.value.service == imageModel.PHOTO_SERVICE
        assert counterValue(store['_id']) is None
        assert counterValue(PhotoActions.ALL_STORES) is None
        assert usageCounts() == [] and fakes.images.calls == []
    finally:
        for _ in range(held):
            slots.release()
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_slots_aCallSucceedsAfterMoreFailuresThanThereAreSlots(
        fakes, monkeypatch, noSleep):
    # No photo style: one model call each, so the script plays in order.
    createTestStore(ADMIN)
    url = putObject(fakes, MINE)
    failures = PhotoActions.PHOTO_CALLS_AT_ONCE + 1
    images = useImages(monkeypatch, ScriptedImages(
        *[sdkError(openai.BadRequestError, 400) for _ in range(failures)],
        1))

    for _ in range(failures):
        with pytest.raises(ServiceUnavailable):
            api.generateProductPhotos(ADMIN, [url])
        assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE

    assert len(api.generateProductPhotos(ADMIN, [url])['photos']) == 1
    assert len(images.calls) == failures + 1


@pytest.mark.parametrize('instant, day', [
    (datetime.datetime(2099, 1, 1, 15, 59, 59, tzinfo=datetime.timezone.utc),
     datetime.date(2099, 1, 1)),
    (datetime.datetime(2099, 1, 1, 16, 0, 0, tzinfo=datetime.timezone.utc),
     datetime.date(2099, 1, 2)),
])
def test_quota_theDayKeyRollsAt16UtcWhichIsMidnightUtcPlus8(
        fakes, monkeypatch, instant, day):
    store, url = themedStoreWithPhoto(fakes)
    monkeypatch.setattr(PhotoActions, '_now', lambda: instant)

    api.generateProductPhotos(ADMIN, [url])

    ids = {doc['_id'] for doc in db.read({}, PhotoActions.PHOTO_USAGE)}
    assert ids == {counterKey(store['_id'], day),
                   counterKey(PhotoActions.ALL_STORES, day)}


@pytest.mark.parametrize('failure, expected', [
    ('policy', ValueError), ('model503', ServiceUnavailable),
    ('noImages', ServiceUnavailable)])
def test_quota_aFailedModelCall_givesEverythingBack_onBothCounters(
        fakes, frozen, monkeypatch, noSleep, failure, expected):
    store, url = themedStoreWithPhoto(fakes)
    outcome = {'policy': policyError,
               'model503': lambda: sdkError(openai.BadRequestError, 400),
               'noImages': lambda: 0}[failure]
    # Both calls (the global and the themed prompt) fail alike.
    useImages(monkeypatch, ScriptedImages(outcome(), outcome()))

    with pytest.raises(expected):
        api.generateProductPhotos(ADMIN, [url])

    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert db.read({}, 'GeneratedPhotos') == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_quota_2Of3Returned_gives1Back_onBothCounters(fakes, frozen):
    store, url = themedStoreWithPhoto(fakes)
    # The themed call returns 1 of its 2.
    fakes.images.returned = 1

    res = api.generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == 2
    assert res['notGenerated'] == PhotoActions.SOME_NOT_MADE
    assert counterValue(store['_id']) == 2
    assert counterValue(PhotoActions.ALL_STORES) == 2


@pytest.mark.parametrize('where', ['missing', 'storage503'])
def test_quota_aReferenceReadFailure_refundsAll_andReleasesTheSlot(
        fakes, frozen, monkeypatch, where):
    from google.api_core.exceptions import ServiceUnavailable as GoogleDown
    store, url = themedStoreWithPhoto(fakes)
    if where == 'missing':
        url = downloadUrl(f'users/{ADMIN}/gone.jpg')
        expected = ValueError
    else:
        expected = ServiceUnavailable

        def down(self, *args, **kwargs):
            raise GoogleDown('down')

        monkeypatch.setattr(FakeBucket, 'get_blob', down)

    with pytest.raises(expected) as error:
        api.generateProductPhotos(ADMIN, [url])

    if where == 'missing':
        assert 'was not found' in str(error.value)
    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert fakes.images.calls == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


# ------------------------- content signatures ------------------------- #

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


@pytest.mark.parametrize('stored', [
    # The stored type says PNG, the bytes are a JPEG.
    {'contentType': 'image/png'},
    # The stored type says JPEG, the bytes are a PNG.
    {'data': PNG_SIGNATURE + b'png'},
    {'data': b'%PDF-1.4 not a photo'},
    {'data': b''},
    {'data': b'\xff\xd8'},
    {'contentType': 'image/png', 'data': PNG_SIGNATURE[:-1]},
])
def test_readOwnImages_contentsOfAnotherType_areRefused_withoutQuotingThePath(
        fakes, stored):
    with pytest.raises(ValueError) as error:
        readOne(fakes, **stored)

    assert str(error.value) == f'imageUrls[0] {photoStorage.NOT_A_REAL_PHOTO}'
    assert MINE not in str(error.value)


@pytest.mark.parametrize('brand', list(photoStorage.HEIC_BRANDS))
def test_readOwnImages_aHeicByContent_asksToConvert(fakes, brand):
    # Named .jpg and stored as image/jpeg, but an ISO-BMFF HEIF file.
    data = b'\x00\x00\x00\x18ftyp' + brand + b'\x00' * 8

    with pytest.raises(ValueError) as error:
        readOne(fakes, data=data)

    assert str(error.value).startswith('imageUrls[0]')
    assert photoStorage.CONVERT_PHOTO in str(error.value)
    assert MINE not in str(error.value)

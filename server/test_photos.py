"""Product photos (PhotoActions, imageModel, photoStorage) and the store's
photo theme (updateMyStoreTheme).

No real Meta or Storage call: a FakeBucket replaces photoStorage._bucket()
(an in-memory object store), and a FakeImages client replaces
chatModel.client(), recording every images/edits request (modelClient)."""

import asyncio
import base64
import datetime
import io
import json
import logging
import re
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

import httpx2 as httpx
import openai
import pytest

import chatModel
import chatTools
import imageModel
import mcpServer
import photoStorage
from actions import PhotoActions, usageCounters
from actions.StoreActions import NO_STORE_YET, NO_THEME_SENT
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import (assertExactShape, createTestStore, createTestUser,
                      modelKeys, wipeCollections, withoutBrandColors)
from mongoDb import db
from objects import (PHOTO_PROMPT_MAX_LENGTH, PHOTOS_PER_CALL,
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
def tinyJpeg() -> bytes:
    """A real 4 x 3 JPEG: references are read by Pillow before the model
    (photoStorage.modelReference)."""
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (4, 3), (120, 80, 40)).save(out, 'JPEG')
    return out.getvalue()


JPEG = tinyJpeg()
# Far ahead, so no TTL monitor deletes a counter mid-test.
FAR_EXPIRY = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)

api = ApiRequests()
tools = ToolRequests()


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
    when it is None."""

    def __init__(self, returned: int | None = None):
        self.returned = returned
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        encoded = base64.b64encode(JPEG).decode()
        returned = kwargs['n'] if self.returned is None else self.returned
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)
                                     for _ in range(returned)])

    def byShot(self) -> list[dict]:
        """The calls one photo call made, one per shot (n=1 each), in
        PHOTO_SHOTS' order (they run at once, so in any order)."""
        return callsByShot(self.calls)


# A phrase of each shot's line (PhotoActions.PHOTO_SHOTS), whatever the
# store's colors.
SHOT_MARKERS = ('three-quarter front view', 'where it is actually used',
                'low or slightly top-down')


def shotOf(prompt: str) -> int:
    """Which shot of PHOTO_SHOTS `prompt` is."""
    [shot] = [i for i, marker in enumerate(SHOT_MARKERS) if marker in prompt]
    return shot


def callsByShot(calls: list[dict]) -> list[dict]:
    ordered = sorted(calls, key=lambda call: shotOf(call['prompt']))
    assert [shotOf(call['prompt']) for call in ordered] == [0, 1, 2]
    assert [call['n'] for call in ordered] == [1] * PHOTOS_PER_CALL
    return ordered


def expectedPrompts(store: dict, base: str | None = None,
                    notes: str | None = None,
                    productName: str | None = None) -> list[str]:
    """The prompts of a photo call of `store` (its stored theme), in shot
    order: `base` (else the default one), its style and shot lines, and
    `notes` last."""
    theme = storedTheme(store)
    if base is None:
        base = PhotoActions.defaultPhotoPrompt(theme, productName)
    return PhotoActions.shotPrompts(theme, base, notes)


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

    theme = tools.updateMyStoreTheme(ADMIN, photoStyle='  soft light  ',
                                   primaryColor='#1a73e8')

    assertExactShape(StoreThemeResponse, theme)
    assert theme == {'photoStyle': 'soft light', 'primaryColor': '#1A73E8',
                     'secondaryColor': None}
    store = tools.getUserStore(ADMIN)
    assertExactShape(MyStoreResponse, store)
    assert store['store']['photoStyle'] == 'soft light'
    assert 'secondaryColor' not in store['store']


def test_generateProductPhotos_makes3_oneCallPerShot_fromTheDefaultBase(
        fakes, monkeypatch):
    """Replaces "one prompt makes all 3 in one call": each photo is its own
    call (n=1), all at once, from the default base and its own shot, the
    store's style on shots 2 and 3 only."""
    store = createTestStore(ADMIN)
    tools.updateMyStoreTheme(ADMIN, photoStyle='white marble, morning light',
                           primaryColor='#112233')
    source = f'users/{ADMIN}/mug.jpg'
    fakes.bucket.objects[source] = {'data': JPEG, 'contentType': 'image/jpeg',
                                    'metadata': None}

    res = tools.generateProductPhotos(ADMIN, [downloadUrl(source)])

    assertExactShape(GeneratedPhotosResponse, res)
    assert len(res['photos']) == PHOTOS_PER_CALL
    assert res['notGenerated'] is None
    calls = fakes.images.byShot()
    prompts = expectedPrompts(store)
    assert [call['prompt'] for call in calls] == prompts
    base = PhotoActions.defaultPhotoPrompt(storedTheme(store))
    shots = PhotoActions.storeShots(storedTheme(store))
    style = 'Style: white marble, morning light'
    assert prompts == [f'{base}\n{shots[0]["line"]}',
                       f'{base}\n{style}\n{shots[1]["line"]}',
                       f'{base}\n{style}\n{shots[2]["line"]}']
    for call in calls:
        assert call['model'] == imageModel.IMAGE_MODEL
        assert call['image'] == [(JPEG, 'image/jpeg')]
        assert 'primary #112233' in call['prompt']
    # Each photo keeps its own prompt, in shot order.
    assert [p['prompt'] for p in res['photos']] == prompts
    saved = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert {doc['_id'] for doc in saved} == {p['_id'] for p in res['photos']}
    byId = {p['_id']: p['prompt'] for p in res['photos']}
    for doc in saved:
        assert doc['path'] == f'stores/{store["_id"]}/photos/{doc["_id"]}.jpg'
        assert fakes.bucket.objects[doc['path']]['data'] == JPEG
        assert doc['prompt'] == byId[doc['_id']]
        assert doc['notes'] is None and doc['productId'] is None
    assert all(u['if_generation_match'] == 0 for u in fakes.bucket.uploads)
    counts = {doc['_id'].split(':')[1]: doc['count']
              for doc in db.read({}, PhotoActions.PHOTO_USAGE)}
    assert counts == {store['_id']: PHOTOS_PER_CALL,
                      PhotoActions.ALL_STORES: PHOTOS_PER_CALL}


def test_generate_aStoreWithNoPhotoStyle_gets3Shots_withNoStyleLine(fakes):
    """Replaces "a store with no photo style gets 3 from the global
    prompt": its 3 shots carry no style line."""
    store = createTestStore(ADMIN)
    theme(primaryColor='#112233')
    url = putObject(fakes, MINE)

    res = tools.generateProductPhotos(ADMIN, [url])

    assertExactShape(GeneratedPhotosResponse, res)
    prompts = [call['prompt'] for call in fakes.images.byShot()]
    assert prompts == expectedPrompts(store)
    assert all('Style:' not in prompt for prompt in prompts)
    assert 'in the primary brand color' in prompts[0]
    assert [p['prompt'] for p in res['photos']] == prompts
    assert res['notGenerated'] is None
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]


def test_generate_theAdminsPrompt_isTheBase_ofEachShot(fakes):
    """Replaces "the admin's prompt is the whole prompt": it is the base,
    stripped, each shot adding its own lines."""
    store, url = themedStoreWithPhoto(fakes)

    res = tools.generateProductPhotos(ADMIN, [url],
                                      prompt='  A mug on a red desk.\n',
                                      productName='Mug')

    prompts = [call['prompt'] for call in fakes.images.byShot()]
    assert prompts == expectedPrompts(store, 'A mug on a red desk.')
    assert all(p.startswith('A mug on a red desk.\n') for p in prompts)
    assert [p['prompt'] for p in res['photos']] == prompts
    for doc in db.read({'storeId': store['_id']}, 'GeneratedPhotos'):
        assert doc['prompt'] in prompts


def test_generate_oneFailedCall_keepsTheOtherTwo_andSaysSomeWereNotMade(
        fakes, frozen, monkeypatch, noSleep, caplog):
    store, url = themedStoreWithPhoto(fakes)
    useImages(monkeypatch, ScriptedImages(perShot=[
        [1], [sdkError(openai.BadRequestError, 400)], [1]]))

    with caplog.at_level(logging.INFO):
        res = tools.generateProductPhotos(ADMIN, [url])

    prompts = expectedPrompts(store)
    assert [p['prompt'] for p in res['photos']] == [prompts[0], prompts[2]]
    assert res['notGenerated'] == PhotoActions.SOME_NOT_MADE
    assert counterValue(store['_id']) == 2
    assert counterValue(PhotoActions.ALL_STORES) == 2
    assert len(db.read({'storeId': store['_id']}, 'GeneratedPhotos')) == 2
    [failed] = [r.msg for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get('event') == 'productPhotoCallFailed']
    assert failed['errorClass'] == 'ServiceUnavailable'
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def themedStoreWithPhoto(fakes, size=None) -> tuple[dict, str]:
    store = createTestStore(ADMIN)
    tools.updateMyStoreTheme(ADMIN, photoStyle='plain')
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
        tools.generateProductPhotos(ADMIN, [url])

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

    res = tools.generateProductPhotos(ADMIN, [url])

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
        tools.generateProductPhotos(ADMIN, [url])
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
        tools.generateProductPhotos(ADMIN,
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
        tools.generateProductPhotos(ADMIN, [url])
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
    tools.updateMyStoreTheme(ADMIN, photoStyle='plain')

    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception) as error:
            tools.generateProductPhotos(
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

    res = tools.generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == PHOTOS_PER_CALL
    assert len(db.read({'storeId': store['_id']},
                       'GeneratedPhotos')) == PHOTOS_PER_CALL


def test_capAllZero_isPhotosTurnedOff(fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    monkeypatch.setenv(PhotoActions.PHOTO_DAILY_CAP_ALL_VARIABLE, '0')

    with pytest.raises(ValueError) as error:
        tools.generateProductPhotos(ADMIN, [url])
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
    number is that many images returned. With `perShot` (a list of outcomes
    per shot of PHOTO_SHOTS), each call plays its own shot's, so the calls
    of one photo call, which run at once, play the same in any order."""

    def __init__(self, *outcomes, perShot=None):
        self.outcomes = list(outcomes)
        self.perShot = perShot
        self.calls = []
        self.lock = threading.Lock()

    def edit(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
            outcomes = (self.outcomes if self.perShot is None
                        else self.perShot[shotOf(kwargs['prompt'])])
            outcome = outcomes.pop(0)
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


def theme(**fields) -> dict:
    return tools.updateMyStoreTheme(ADMIN, **fields)


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
        tools.generateProductPhotos(ADMIN, imageUrls)

    assert str(error.value) == PhotoActions.IMAGE_URLS_COUNT
    assert usageCounts() == [] and fakes.images.calls == []


def test_generate_tenUrlsWork_elevenAreRefused(fakes):
    themedStoreWithPhoto(fakes)
    urls = [putObject(fakes, f'users/{ADMIN}/p{i}.jpg') for i in range(11)]

    with pytest.raises(ValueError) as error:
        tools.generateProductPhotos(ADMIN, urls)
    assert str(error.value) == PhotoActions.IMAGE_URLS_COUNT
    assert usageCounts() == [] and fakes.images.calls == []

    res = tools.generateProductPhotos(ADMIN, urls[:10])
    assert len(res['photos']) == PHOTOS_PER_CALL
    # One call per shot, each with every reference.
    assert [len(call['image']) for call in fakes.images.byShot()] == [10] * 3


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
        tools.generateProductPhotos(ADMIN, [downloadUrl(MINE), second])

    assert str(error.value) == PhotoActions.IMAGE_URLS_TWICE
    assert usageCounts() == [] and fakes.images.calls == []


def test_generate_aCountSentToTheTool_isIgnored_alwaysMakes3(fakes):
    _, url = themedStoreWithPhoto(fakes)

    args = chatTools.GenerateProductPhotosArgs.model_validate(
        {'imageUrls': [url], 'count': 1})
    res = chatTools._generateProductPhotos(ADMIN, '', args)

    # One photo per call, one call per shot.
    assert [call['n'] for call in fakes.images.byShot()] == [1] * 3
    assert len(res['photos']) == PHOTOS_PER_CALL


def test_generate_prompt_blankIsTheDefault_overLengthAndNonTextRefused(fakes):
    """Replaces the notes test: a blank prompt is the default one, and one
    over PHOTO_PROMPT_MAX_LENGTH or not text is refused before any quota."""
    store, url = themedStoreWithPhoto(fakes)

    tools.generateProductPhotos(ADMIN, [url], prompt='  \n ')
    assert [call['prompt'] for call in fakes.images.byShot()] == (
        expectedPrompts(store))

    before = usageCounts()
    for prompt in ('n' * (PHOTO_PROMPT_MAX_LENGTH + 1), 5, ['x'], 'a\x00b'):
        with pytest.raises(ValueError) as error:
            tools.generateProductPhotos(ADMIN, [url], prompt=prompt)
        assert str(error.value).startswith(PhotoActions.PROMPT_LABEL)
    assert usageCounts() == before and len(fakes.images.calls) == 3
    tools.generateProductPhotos(ADMIN, [url],
                                prompt='n' * PHOTO_PROMPT_MAX_LENGTH)
    assert [call['prompt'] for call in callsByShot(fakes.images.calls[3:])] \
        == expectedPrompts(store, 'n' * PHOTO_PROMPT_MAX_LENGTH)


@pytest.mark.parametrize('clear', [None, '', '   '])
def test_generate_withoutAPhotoStyle_makes3Shots_withNoStyleLine(fakes, clear):
    """Replaces "without a photo style makes the global photo only"."""
    if clear is None:
        createTestStore(ADMIN)
        url = putObject(fakes, MINE)
    else:
        _, url = themedStoreWithPhoto(fakes)
        theme(photoStyle=clear)

    res = tools.generateProductPhotos(ADMIN, [url])

    assert len(res['photos']) == PHOTOS_PER_CALL
    assert res['notGenerated'] is None
    assert all('Style:' not in call['prompt']
               for call in fakes.images.byShot())
    assert usageCounts() == [PHOTOS_PER_CALL, PHOTOS_PER_CALL]


# -------------------------------- access -------------------------------- #


@pytest.mark.parametrize('route, caller, error, message', [
    ('generateProductPhotos', 'customer', PermissionError,
     'Only a store admin'),
    ('updateMyStoreTheme', 'customer', PermissionError, 'Only a store admin'),
    ('generateProductPhotos', 'noStore', ValueError, NO_STORE_YET),
    ('updateMyStoreTheme', 'noStore', ValueError, NO_STORE_YET),
])
def test_access_aCustomerOrAnAdminWithoutAStore_isRefused(
        fakes, route, caller, error, message):
    store, url = themedStoreWithPhoto(fakes)
    createTestUser(NO_STORE_ADMIN)
    identity = CUSTOMER if caller == 'customer' else NO_STORE_ADMIN
    body = ({'imageUrls': [url]} if route == 'generateProductPhotos'
            else {'photoStyle': 'x'})

    with pytest.raises(error, match=message):
        getattr(tools, route)(identity, **body)

    assert usageCounts() == [] and fakes.images.calls == []
    assert storedTheme(store)['photoStyle'] == 'plain'


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
        res = tools.generateProductPhotos(ADMIN, [url],
                                          prompt=SENTINEL_NOTES)

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
    # Every call is refused.
    images = useImages(monkeypatch, ScriptedImages(
        perShot=[[policyError(key)] for _ in range(PHOTOS_PER_CALL)]))

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError) as error:
            tools.generateProductPhotos(ADMIN, [url], prompt=SENTINEL_NOTES)

    assert str(error.value) == imageModel.PHOTO_REQUEST_DECLINED
    assert len(images.calls) == PHOTOS_PER_CALL and noSleep == []
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
        tools.generateProductPhotos(ADMIN, [url])

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

    tools.updateMyStoreTheme(OTHER, photoStyle='their style')

    assert storedTheme(mine) == FULL_THEME
    assert storedTheme(theirs)['photoStyle'] == 'their style'


def test_theme_aCustomerOrAnAdminWithoutAStore_changesNothing(clean):
    store = withoutBrandColors(createTestStore(ADMIN))
    createTestUser(NO_STORE_ADMIN)

    with pytest.raises(PermissionError):
        tools.updateMyStoreTheme(CUSTOMER, photoStyle='x')
    with pytest.raises(ValueError, match=NO_STORE_YET):
        tools.updateMyStoreTheme(NO_STORE_ADMIN, photoStyle='x')
    assert storedTheme(store) == {f: None for f in STORE_THEME_FIELDS}


def test_theResponsesACustomerCanSee_carryNoThemeField(clean):
    store = createTestStore(ADMIN)
    theme(**FULL_THEME)

    located = tools.setUserStoreLocation(ADMIN, 14.6, 121.0)

    assertExactShape(StoreResponse, located)
    assert not set(STORE_THEME_FIELDS) & set(located)
    assert not set(STORE_THEME_FIELDS) & modelKeys(StoreResponse)
    assert set(STORE_THEME_FIELDS) <= modelKeys(AdminStoreResponse)
    # Only the owner's own view has them.
    mine = tools.getUserStore(ADMIN)['store']
    assert {f: mine[f] for f in STORE_THEME_FIELDS} == FULL_THEME
    assert store['_id'] == mine['_id']


def test_theOwnersView_ofAStoreWithNoTheme_leavesTheFieldsOut(clean):
    withoutBrandColors(createTestStore(ADMIN))

    mine = tools.getUserStore(ADMIN)['store']

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
def test_defaultPhotoPrompt_hasTheColorsLineOnlyWhenAColorIsSet(
        colors, expected, absent):
    """Replaces photoPrompt's colors test: the default base carries the
    colors line, and never a style, a shot or notes."""
    prompt = PhotoActions.defaultPhotoPrompt({'photoStyle': 'calm', **colors})

    assert prompt.split('\n')[:5] == list(PhotoActions.PHOTO_BASE_LINES)
    for text in ('Style:', 'Shot:', 'Special request:'):
        assert text not in prompt
    for text in expected:
        assert text in prompt
    for text in absent:
        assert text not in prompt


def test_defaultPhotoPrompt_isTheBaseLines_theProduct_andTheColors():
    """Replaces the themed and global default prompt tests: one default
    base, the same with or without a photo style."""
    store = {'primaryColor': '#112233', 'secondaryColor': '#445566'}
    for style in (None, '', 'calm'):
        prompt = PhotoActions.defaultPhotoPrompt({**store,
                                                  'photoStyle': style},
                                                 'Blue Mug')
        assert prompt.split('\n') == [
            *PhotoActions.PHOTO_BASE_LINES,
            'Product: Blue Mug',
            'Brand colors: primary #112233, secondary #445566 — for '
            'backgrounds and props only, never on the product.']
    assert (PhotoActions.defaultPhotoPrompt({'photoStyle': None})
            == '\n'.join(PhotoActions.PHOTO_BASE_LINES))
    assert 'Product:' not in PhotoActions.defaultPhotoPrompt(store)


def test_photoBaseLines_askForTheProductOnly_withoutWhatSitsBesideIt():
    """Replaces the PHOTO_INSTRUCTION test."""
    base = '\n'.join(PhotoActions.PHOTO_BASE_LINES)

    assert 'Show only the product' in base
    assert 'such as items for scale' in base
    assert 'Keep the product exactly as it is' in base
    assert ('No added text, logos, watermarks or people unless the photo '
            'style asks for them.') in base


def test_shotLines_nameTheBrandColors_theStoreHas():
    shots = PhotoActions.shotLines('#112233', '#445566')

    assert [shot['label'] for shot in shots] == ['Catalog', 'In use', 'Hero']
    assert len(shots) == PHOTOS_PER_CALL
    assert 'a seamless background in the primary brand color' in (
        shots[0]['line'])
    assert 'props in the secondary brand color' in shots[1]['line']
    assert ('a smooth gradient from the primary to the secondary brand '
            'color') in shots[2]['line']
    assert all(shot['line'].startswith('Shot: ') for shot in shots)
    # The hex values are the base's colors line's, never a shot's.
    assert all('#' not in shot['line'] for shot in shots)
    # Never the product's name, which a base may lack.
    assert 'inferred from the product,' in shots[1]['line']
    assert 'name' not in shots[1]['line']


def test_shotLines_withoutColors_fallBackToNeutral():
    shots = PhotoActions.shotLines(None, None)

    assert 'a seamless neutral light-grey background' in shots[0]['line']
    assert 'a few simple neutral props' in shots[1]['line']
    assert 'a soft neutral gradient' in shots[2]['line']
    assert all('brand color' not in shot['line'] for shot in shots)
    onlyPrimary = PhotoActions.shotLines('#112233', None)
    assert 'in the primary brand color' in onlyPrimary[0]['line']
    assert 'simple neutral props' in onlyPrimary[1]['line']
    assert ('from the primary brand color to a soft neutral light grey'
            in onlyPrimary[2]['line'])
    onlySecondary = PhotoActions.shotLines(None, '#445566')
    assert 'neutral light-grey background' in onlySecondary[0]['line']
    assert 'props in the secondary brand color' in onlySecondary[1]['line']
    assert ('from the secondary brand color to a soft neutral light grey'
            in onlySecondary[2]['line'])


def test_cardShots_sayTheStyleIsAdded_onShots2And3_displayOnly():
    styled = {'photoStyle': 'calm', 'primaryColor': '#112233'}
    shots = PhotoActions.storeShots(styled)

    card = PhotoActions.cardShots(styled)

    assert [c['line'] for c in card] == [
        shots[0]['line'],
        shots[1]['line'] + PhotoActions.STYLE_SHOT_SUFFIX,
        shots[2]['line'] + PhotoActions.STYLE_SHOT_SUFFIX]
    assert PhotoActions.STYLE_SHOT_SUFFIX == ' Plus your photo style.'
    # The prompts never carry the suffix: they carry the style itself.
    assert all(PhotoActions.STYLE_SHOT_SUFFIX.strip() not in prompt
               for prompt in PhotoActions.shotPrompts(styled, 'BASE', None))
    for unstyled in ({'photoStyle': None, 'primaryColor': '#112233'},
                     {'photoStyle': '', 'primaryColor': '#112233'}):
        assert PhotoActions.cardShots(unstyled) == shots


def test_generate_encodesTheReferencesOnce_sharedByThe3Calls(
        fakes, monkeypatch):
    _, url = themedStoreWithPhoto(fakes)
    real = imageModel.imageInputs
    encoded = []

    def counting(images):
        encoded.append(len(images))
        return real(images)

    monkeypatch.setattr(imageModel, 'imageInputs', counting)

    tools.generateProductPhotos(ADMIN, [url])

    # Once for the photo call, not once per request.
    assert encoded == [1]
    assert [call['image'] for call in fakes.images.byShot()] == [
        [(JPEG, 'image/jpeg')]] * PHOTOS_PER_CALL


def encoded(size, fmt, mode='RGB', orientation=None) -> bytes:
    """A solid image of `size` saved as `fmt`, with an EXIF orientation
    flag when given."""
    from PIL import Image
    out = io.BytesIO()
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[photoStorage.EXIF_ORIENTATION] = orientation
        options['exif'] = exif
    color = (10, 20, 30, 200) if mode == 'RGBA' else (10, 20, 30)
    Image.new(mode, size, color).save(out, fmt, **options)
    return out.getvalue()


def pixelSize(data: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        return image.size


@pytest.fixture
def smallDecodeCap(monkeypatch):
    """DECODE_MAX_PIXELS at 1000 (and the model's side at 20), so small
    test photos stand for photos over the real 16 megapixels."""
    monkeypatch.setattr(photoStorage, 'DECODE_MAX_PIXELS', 1000)
    monkeypatch.setattr(photoStorage, 'MODEL_REFERENCE_MAX_SIDE', 20)


def test_decodeCap_isSixteenMegapixels_andSaysSo():
    assert photoStorage.DECODE_MAX_PIXELS == 16_000_000
    assert photoStorage.DECODE_TOO_LARGE == (
        'is too large (over 16 megapixels). Use a smaller photo.')


def test_decodeCap_aPngOrWebPOverIt_isRefused_inEveryDecode(smallDecodeCap):
    png = encoded((40, 40), 'PNG', 'RGBA')
    webpPhoto = encoded((40, 40), 'WEBP')
    message = '^photo ' + re.escape(photoStorage.DECODE_TOO_LARGE) + '$'

    for decode in (
            lambda: photoStorage.storeOriginal(png, 'image/png', 'photo'),
            lambda: photoStorage.storeOriginal(webpPhoto, 'image/webp',
                                               'photo'),
            lambda: photoStorage.modelReference(png, 'image/png', 'photo'),
            lambda: photoStorage.modelReference(webpPhoto, 'image/webp',
                                                'photo'),
            lambda: photoStorage.receiptImage(png, 'photo'),
            lambda: photoStorage.receiptImage(webpPhoto, 'photo')):
        with pytest.raises(ValueError, match=message):
            decode()
    # Under it, a PNG is decoded as before.
    small = encoded((30, 30), 'PNG', 'RGBA')
    assert pixelSize(photoStorage.storeOriginal(small, 'image/png',
                                                'photo')[0]) == (30, 30)


def test_decodeCap_aPngOverIt_isNeverLoaded_beforeItIsRefused(
        smallDecodeCap, monkeypatch):
    """Reading a PNG's EXIF loads all its pixels (an eXIf chunk may follow
    them), so the cap comes first: a PNG over it is refused unloaded."""
    from PIL import PngImagePlugin
    loads = []
    load = PngImagePlugin.PngImageFile.load

    def spiedLoad(self):
        loads.append(self.size)
        return load(self)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, 'load', spiedLoad)
    png = encoded((40, 40), 'PNG', 'RGBA')
    message = '^photo ' + re.escape(photoStorage.DECODE_TOO_LARGE) + '$'

    for decode in (
            lambda: photoStorage.modelReference(png, 'image/png', 'photo'),
            lambda: photoStorage.storeOriginal(png, 'image/png', 'photo'),
            lambda: photoStorage.receiptImage(png, 'photo')):
        with pytest.raises(ValueError, match=message):
            decode()
    assert loads == []
    # The spy sees a load within the cap, and a small upright PNG is still
    # passed through as is.
    small = encoded((20, 20), 'PNG', 'RGBA')
    assert photoStorage.modelReference(small, 'image/png', 'photo') == (
        small, 'image/png')
    assert loads != []


def test_modelReference_aJpegDraftedToTheSide_isStillReEncoded(
        smallDecodeCap):
    """The side checked for the pass-through is the stored one: a JPEG the
    draft shrinks to the side is re-encoded, never sent at its full size."""
    jpeg = encoded((40, 40), 'JPEG')

    data, contentType = photoStorage.modelReference(jpeg, 'image/jpeg',
                                                    'photo')
    assert contentType == 'image/jpeg' and data != jpeg
    assert max(pixelSize(data)) <= 20


def test_decodeCap_aJpegOverIt_isDecodedAtAReducedScale(smallDecodeCap):
    """A phone JPEG over the cap (a 48 megapixel one, say) still works:
    the draft decodes it at the least reduction under the cap."""
    jpeg = encoded((40, 40), 'JPEG')

    data, contentType = photoStorage.storeOriginal(jpeg, 'image/jpeg',
                                                   'photo')
    width, height = pixelSize(data)
    assert contentType == 'image/jpeg' and width * height <= 1000
    assert (width, height) == (20, 20)
    width, height = pixelSize(photoStorage.receiptImage(jpeg, 'photo'))
    assert width * height <= 1000
    data, _ = photoStorage.modelReference(jpeg, 'image/jpeg', 'photo')
    assert pixelSize(data) == (20, 20)


def test_modelReference_turnsAPhotoUpright_beforeScalingItDown():
    """A portrait phone photo is stored sideways with a rotation flag: it
    reaches the model upright."""
    side = photoStorage.MODEL_REFERENCE_MAX_SIDE
    sideways = encoded((3000, 2000), 'JPEG', orientation=6)

    data, contentType = photoStorage.modelReference(sideways, 'image/jpeg',
                                                    'r')

    width, height = pixelSize(data)
    assert contentType == 'image/jpeg'
    assert height > width and height == side


def test_modelReference_aSmallFlaggedPhoto_isTurned_anUnflaggedOneKept():
    flagged = encoded((6, 4), 'JPEG', orientation=6)
    plain = encoded((6, 4), 'JPEG')

    data, _ = photoStorage.modelReference(flagged, 'image/jpeg', 'r')

    assert pixelSize(data) == (4, 6)
    assert photoStorage.modelReference(plain, 'image/jpeg', 'r') == (
        plain, 'image/jpeg')
    upright = encoded((6, 4), 'JPEG', orientation=1)
    assert photoStorage.modelReference(upright, 'image/jpeg', 'r') == (
        upright, 'image/jpeg')


def test_modelReference_scalesALargePhotoDown_andKeepsASmallOneAsIs():
    from PIL import Image
    side = photoStorage.MODEL_REFERENCE_MAX_SIDE
    out = io.BytesIO()
    Image.new('RGB', (side + 952, 1000), (10, 20, 30)).save(out, 'JPEG')
    data, contentType = photoStorage.modelReference(out.getvalue(),
                                                    'image/jpeg', 'r')
    with Image.open(io.BytesIO(data)) as image:
        assert contentType == 'image/jpeg' and max(image.size) == side
    out = io.BytesIO()
    Image.new('RGBA', (1000, side + 1), (10, 20, 30, 100)).save(out, 'PNG')
    data, contentType = photoStorage.modelReference(out.getvalue(),
                                                    'image/png', 'r')
    with Image.open(io.BytesIO(data)) as image:
        assert contentType == 'image/png' and image.mode == 'RGBA'
        assert max(image.size) == side
    assert photoStorage.modelReference(JPEG, 'image/jpeg', 'r') == (
        JPEG, 'image/jpeg')
    with pytest.raises(ValueError, match="^r isn't a real"):
        photoStorage.modelReference(JPEG[:3] + b'x', 'image/jpeg', 'r')


def test_shotPrompts_theStyleOnShots2And3Only_andTheNotesLastOnEach():
    store = {'photoStyle': 'calm', 'primaryColor': '#112233'}
    shots = PhotoActions.storeShots(store)

    colors = PhotoActions.brandColorsLine('#112233', None)

    prompts = PhotoActions.shotPrompts(store, 'BASE', 'brighter')

    # A base without the colors line gets it back on each shot.
    assert [p.split('\n') for p in prompts] == [
        ['BASE', colors, shots[0]['line'], 'Special request: brighter'],
        ['BASE', colors, 'Style: calm', shots[1]['line'],
         'Special request: brighter'],
        ['BASE', colors, 'Style: calm', shots[2]['line'],
         'Special request: brighter']]
    # A base holding it (the default one) is not given it twice.
    default = PhotoActions.defaultPhotoPrompt(store)
    assert all(p.count(colors) == 1
               for p in PhotoActions.shotPrompts(store, default, None))
    plain = PhotoActions.shotPrompts({'photoStyle': None}, 'BASE', None)
    assert [p.split('\n') for p in plain] == [
        ['BASE', shot['line']] for shot in PhotoActions.shotLines(None, None)]


def test_generate_productName_isInThePrompt_strippedAndBlankIsNone(fakes):
    _, url = themedStoreWithPhoto(fakes)

    tools.generateProductPhotos(ADMIN, [url], productName='  Blue Mug ')
    for blank in ('', '   '):
        tools.generateProductPhotos(ADMIN, [url], productName=blank)
    tools.generateProductPhotos(ADMIN, [url])

    prompts = [call['prompt'] for call in fakes.images.calls]
    # One call per shot, each photo call's 3 after the one before.
    named, unnamed = prompts[:PHOTOS_PER_CALL], prompts[PHOTOS_PER_CALL:]
    assert all('Product: Blue Mug\n' in prompt for prompt in named)
    assert len(unnamed) == 3 * PHOTOS_PER_CALL
    assert all('Product:' not in prompt for prompt in unnamed)


def test_generate_productName_overLengthOrNotText_isRefused_beforeAnyQuota(
        fakes):
    _, url = themedStoreWithPhoto(fakes)

    for productName in ('n' * (PRODUCT_NAME_MAX_LENGTH + 1), 5, ['x'],
                        'Mug\x00'):
        with pytest.raises(ValueError) as error:
            tools.generateProductPhotos(ADMIN, [url], productName=productName)
        assert str(error.value).startswith(PhotoActions.PRODUCT_NAME_LABEL)
    assert usageCounts() == [] and fakes.images.calls == []
    tools.generateProductPhotos(ADMIN, [url],
                              productName='n' * PRODUCT_NAME_MAX_LENGTH)
    assert len(fakes.images.calls) == PHOTOS_PER_CALL


def test_generate_withNoColors_thePromptHasNoColorsLine(fakes):
    store, url = themedStoreWithPhoto(fakes)
    withoutBrandColors(store)

    tools.generateProductPhotos(ADMIN, [url])

    assert all('Brand colors' not in call['prompt']
               for call in fakes.images.byShot())


def test_mcp_generateProductPhotos_hasNoUserIdInItsSchema():
    async def schemas():
        tools = await mcpServer.servers['admin'].list_tools()
        return {t.name: t.input_schema for t in tools}

    byName = asyncio.run(schemas())

    assert set(byName['generateProductPhotos']['properties']) == {
        'imageUrls', 'prompt', 'productName', 'notes'}
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
        tools.generateProductPhotos(ADMIN, [url])

    assert str(error.value) == PhotoActions.PHOTO_CAP_REACHED
    assert counterValue(store['_id']) == 98
    assert counterValue(PhotoActions.ALL_STORES) is None
    assert fakes.images.calls == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE

    # The slot is free again, and the 3 left (97 used) are still there to
    # take.
    usageCounters.giveBackQuota(PhotoActions.PHOTO_USAGE,
                                counterKey(store['_id']), 1)
    assert len(tools.generateProductPhotos(ADMIN, [url])['photos']) == 3
    assert counterValue(store['_id']) == 100
    with pytest.raises(ValueError) as again:
        tools.generateProductPhotos(ADMIN, [url])
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
            tools.generateProductPhotos(ADMIN, [url])
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
    # Each photo call's 3 model calls end before the next one's, so the
    # script plays a photo call's 3 outcomes at a time.
    createTestStore(ADMIN)
    url = putObject(fakes, MINE)
    failures = PhotoActions.PHOTO_CALLS_AT_ONCE + 1
    images = useImages(monkeypatch, ScriptedImages(
        *[sdkError(openai.BadRequestError, 400)
          for _ in range(failures * PHOTOS_PER_CALL)],
        1, 0, 0))

    for _ in range(failures):
        with pytest.raises(ServiceUnavailable):
            tools.generateProductPhotos(ADMIN, [url])
        assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE

    assert len(tools.generateProductPhotos(ADMIN, [url])['photos']) == 1
    assert len(images.calls) == (failures + 1) * PHOTOS_PER_CALL


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

    tools.generateProductPhotos(ADMIN, [url])

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
    # Every call fails.
    useImages(monkeypatch, ScriptedImages(
        perShot=[[outcome()] for _ in range(PHOTOS_PER_CALL)]))

    with pytest.raises(expected):
        tools.generateProductPhotos(ADMIN, [url])

    assert counterValue(store['_id']) == 0
    assert counterValue(PhotoActions.ALL_STORES) == 0
    assert db.read({}, 'GeneratedPhotos') == []
    assert slotsFree() == PhotoActions.PHOTO_CALLS_AT_ONCE


def test_quota_2Of3Returned_gives1Back_onBothCounters(fakes, frozen,
                                                       monkeypatch):
    store, url = themedStoreWithPhoto(fakes)
    # One of the 3 calls returns no photo.
    useImages(monkeypatch, ScriptedImages(perShot=[[1], [0], [1]]))

    res = tools.generateProductPhotos(ADMIN, [url])

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
        tools.generateProductPhotos(ADMIN, [url])

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

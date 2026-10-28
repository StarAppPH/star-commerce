"""Product photos (PhotoActions, imageModel, photoStorage) and the store's
photo theme (updateMyStoreTheme).

No real Meta or Storage call: a FakeBucket replaces photoStorage._bucket()
(an in-memory object store), and a FakeImages client replaces
chatModel.client(), recording every images.edit call."""

import base64
import logging
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import chatModel
import imageModel
import photoStorage
from actions import PhotoActions
from ApiRequests import ApiRequests
from conftest import assertExactShape, createTestStore, wipeCollections
from mongoDb import db
from objects import GeneratedPhotosResponse, MyStoreResponse, StoreThemeResponse

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'GeneratedPhotos',
               'PhotoUsage']
ADMIN = 'ph-admin'
BUCKET = 'ph-test-bucket.firebasestorage.app'
JPEG = b'\xff\xd8\xff' + b'jpeg-bytes'

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

    def __init__(self, returned: int):
        self.returned = returned
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        encoded = base64.b64encode(JPEG).decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)
                                     for _ in range(self.returned)])


@pytest.fixture
def fakes(clean, monkeypatch):
    bucket = FakeBucket()
    images = FakeImages(returned=2)
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.setattr(chatModel, 'client',
                        lambda: SimpleNamespace(images=images))
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
    createTestStore(ADMIN)

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

    res = api.generateProductPhotos(ADMIN, [downloadUrl(source)], count=3,
                                    notes='from above')

    assertExactShape(GeneratedPhotosResponse, res)
    assert len(res['photos']) == 2
    [call] = fakes.images.calls
    assert call['model'] == imageModel.IMAGE_MODEL and call['n'] == 3
    assert call['image'] == [('reference0.jpg', JPEG, 'image/jpeg')]
    assert 'Style: white marble, morning light' in call['prompt']
    assert 'primary #112233' in call['prompt']
    assert 'Notes: from above' in call['prompt']
    saved = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert {doc['_id'] for doc in saved} == {p['_id'] for p in res['photos']}
    for doc in saved:
        assert doc['path'] == f'stores/{store["_id"]}/photos/{doc["_id"]}.jpg'
        assert fakes.bucket.objects[doc['path']]['data'] == JPEG
    assert all(u['if_generation_match'] == 0 for u in fakes.bucket.uploads)
    # 3 asked for, 2 returned: 1 given back, on both counters.
    counts = {doc['_id'].split(':')[1]: doc['count']
              for doc in db.read({}, PhotoActions.PHOTO_USAGE)}
    assert counts == {store['_id']: 2, PhotoActions.ALL_STORES: 2}


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
        api.generateProductPhotos(ADMIN, [url], count=2)

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

    res = api.generateProductPhotos(ADMIN, [url], count=2)

    assertExactShape(GeneratedPhotosResponse, res)
    assert len(res['photos']) == 1
    [saved] = db.read({'storeId': store['_id']}, 'GeneratedPhotos')
    assert saved['_id'] == res['photos'][0]['_id']
    # Both photos came back from Meta (billed): none is given back.
    assert usageCounts() == [2, 2]


def test_noUploadWorks_raises_andRecordsNothing(fakes):
    _, url = themedStoreWithPhoto(fakes)
    fakes.bucket.uploadsThatWork = 0

    with pytest.raises(Exception, match='photo storage'):
        api.generateProductPhotos(ADMIN, [url], count=1)
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
        api.generateProductPhotos(ADMIN, [url], count=2)
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
    monkeypatch.setattr(chatModel, 'client',
                        lambda: SimpleNamespace(images=fake))
    with caplog.at_level(logging.WARNING):
        assert imageModel.editImages([], 'p', 2,
                                     time.monotonic() + 60) == [JPEG]
        data[1] = SimpleNamespace(b64_json=None)
        assert imageModel.editImages([], 'p', 2,
                                     time.monotonic() + 60) == []
    events = [r.msg.get('event') for r in caplog.records
              if isinstance(r.msg, dict)]
    assert 'imageModelBadImage' in events and 'imageModelEmpty' in events

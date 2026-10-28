"""Product photos (PhotoActions, imageModel, photoStorage) and the store's
photo theme (updateMyStoreTheme).

No real Meta or Storage call: a FakeBucket replaces photoStorage._bucket()
(an in-memory object store), and a FakeImages client replaces
chatModel.client(), recording every images.edit call."""

import base64
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
        self.bucket.objects[self.name] = {'data': data,
                                          'contentType': content_type,
                                          'metadata': self.metadata}


class FakeBucket:

    def __init__(self):
        self.objects = {}

    def get_blob(self, name, **kwargs):
        stored = self.objects.get(name)
        if stored is None:
            return None
        blob = FakeBlob(self, name)
        blob.content_type = stored['contentType']
        blob.size = len(stored['data'])
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
    # 3 asked for, 2 returned: 1 given back.
    [usage] = db.read({}, PhotoActions.PHOTO_USAGE)
    assert usage['count'] == 2

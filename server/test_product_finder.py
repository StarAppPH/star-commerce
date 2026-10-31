"""Products found in a group photo (findProductsInPhoto, productFinder,
samModel) and their cut-outs on the new products form.

No real Meta or Storage call: test_photos' FakeBucket replaces
photoStorage._bucket(), and a FakeMeta client replaces chatModel.client():
its chat completion lists the products (Muse Spark), its responses.create
answers SAM with a box per phrase."""

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import chatForms
import chatModel
import photoStorage
import productFinder
import samModel
from actions import PhotoActions
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import assertExactShape, createTestStore, wipeCollections
from mongoDb import db
from objects import FoundProductsResponse
from services.__request import ServiceUnavailable
from test_photos import BUCKET, FakeBucket, downloadUrl

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'PhotoUsage']
ADMIN = 'pf-admin'
GROUP = f'users/{ADMIN}/chat/a/group.jpg'
# The photo: 400 x 200, a red product on the left, a blue one on the right.
WIDTH, HEIGHT = 400, 200
LISTING = {'products': [{'name': 'Red Mug', 'phrase': 'red mug'},
                        {'name': 'Blue Bowl', 'phrase': 'blue bowl'}]}
# SAM's boxes, in the photo's own pixels (its copy is no smaller).
BOXES = {'red mug': '<0f>0<|box;x1=20;y1=40;x2=119;y2=159;w=400;h=200|>'
                    '<|mask;x=0;y=0;data=1,1,~ab|>',
         'blue bowl': '<0f>0<|box;x1=250;y1=50;x2=349;y2=149;w=400;h=200|>'}

api = ApiRequests()
tools = ToolRequests()


def groupPhoto() -> bytes:
    image = Image.new('RGB', (WIDTH, HEIGHT), 'white')
    image.paste('red', (20, 40, 120, 160))
    image.paste('blue', (250, 50, 350, 150))
    out = io.BytesIO()
    image.save(out, 'JPEG')
    return out.getvalue()


class FakeMeta:

    def __init__(self):
        self.listing = json.dumps(LISTING)
        self.boxes = dict(BOXES)
        self.chatCalls, self.samCalls = [], []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._complete))
        self.responses = SimpleNamespace(create=self._segment)

    def _complete(self, **kwargs):
        self.chatCalls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.listing))])

    def _segment(self, **kwargs):
        self.samCalls.append(kwargs)
        [message] = kwargs['input']
        phrase = message['content'][0]['text']
        answer = self.boxes[phrase]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(output_text=answer)


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    wipeCollections(COLLECTIONS)
    bucket = FakeBucket()
    meta = FakeMeta()
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.setattr(chatModel, 'client', lambda: meta)
    store = createTestStore(ADMIN)
    bucket.objects[GROUP] = {'data': groupPhoto(),
                             'contentType': 'image/jpeg', 'metadata': None}
    yield SimpleNamespace(bucket=bucket, meta=meta, store=store)
    wipeCollections(COLLECTIONS)


def findCounts() -> list[int]:
    return sorted(doc['count'] for doc in db.read(
        {'_id': {'$regex': f'^{PhotoActions.FINDS_KIND}:'}},
        PhotoActions.PHOTO_USAGE))


def savedImage(bucket, imageUrl: str):
    path = photoStorage.objectPathFromUrl(imageUrl, 'imageUrl')
    stored = bucket.objects[path]
    assert stored['contentType'] == 'image/jpeg'
    return path, Image.open(io.BytesIO(stored['data']))


def test_findProductsInPhoto_namesAndCutsOutEachProduct(fakes):
    res = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assertExactShape(FoundProductsResponse, res)
    assert [p['name'] for p in res['products']] == ['Red Mug', 'Blue Bowl']
    assert res['notFound'] == []
    for product, color in zip(res['products'], ('red', 'blue')):
        path, image = savedImage(fakes.bucket, product['imageUrl'])
        assert path.startswith(f'users/{ADMIN}/found/')
        assert product['imageUrl'] == photoStorage.publicPathUrl(path)
        # The box and its margin: the product fills the middle.
        middle = image.getpixel((image.width // 2, image.height // 2))
        assert middle == pytest.approx(
            Image.new('RGB', (1, 1), color).getpixel((0, 0)), abs=12)
    _, red = savedImage(fakes.bucket, res['products'][0]['imageUrl'])
    assert red.size == (116, 140)
    # Spark sees the photo; SAM gets one phrase a call, never stored.
    [listing] = fakes.meta.chatCalls
    assert listing['messages'][1]['content'][0]['image_url']['url'].startswith(
        'data:image/jpeg;base64,')
    assert 'tools' not in listing
    assert sorted(c['input'][0]['content'][0]['text']
                  for c in fakes.meta.samCalls) == ['blue bowl', 'red mug']
    for call in fakes.meta.samCalls:
        assert call['model'] == samModel.SAM_MODEL and call['store'] is False
    assert findCounts() == [1, 1]


def test_aProductSamCannotLocate_isNotFound(fakes):
    fakes.meta.boxes['blue bowl'] = ''

    res = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assert [p['name'] for p in res['products']] == ['Red Mug']
    assert res['notFound'] == ['Blue Bowl']


def test_oneSamFailure_isNotFound_butAllFailing_isA503(fakes):
    fakes.meta.boxes['blue bowl'] = samModel.unavailable()
    res = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))
    assert res['notFound'] == ['Blue Bowl']

    fakes.meta.boxes['red mug'] = samModel.unavailable()
    with pytest.raises(ServiceUnavailable):
        tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))
    # The failed find is given back; the one before it counts.
    assert findCounts() == [1, 1]


def test_oneProductNamedTwice_isKeptOnce(fakes):
    fakes.meta.listing = json.dumps({'products': [
        *LISTING['products'], {'name': 'Mug', 'phrase': 'mug'}]})
    fakes.meta.boxes['mug'] = BOXES['red mug']

    res = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assert [p['name'] for p in res['products']] == ['Red Mug', 'Blue Bowl']
    assert res['notFound'] == []


def test_aPhotoWithNoProducts_findsNone(fakes):
    fakes.meta.listing = '{"products": []}'

    res = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assert res == {'products': [], 'notFound': []}
    assert fakes.meta.samCalls == []


def test_anUnreadableListing_isA503_andGivesTheFindBack(fakes):
    fakes.meta.listing = 'Here are your products: a mug and a bowl.'

    with pytest.raises(ServiceUnavailable):
        tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assert findCounts() == [0, 0]


def test_anotherUsersPhoto_isRefused_beforeAnyCall(fakes):
    theirs = 'users/someone-else/group.jpg'
    fakes.bucket.objects[theirs] = fakes.bucket.objects[GROUP]

    with pytest.raises(ValueError, match='^imageUrl must be a photo you '
                       'uploaded$'):
        tools.findProductsInPhoto(ADMIN, downloadUrl(theirs))

    assert fakes.meta.chatCalls == [] and findCounts() == []


def test_theStoresDailyCap_refuses(fakes):
    for _ in range(PhotoActions.STORE_DAILY_FIND_CAP):
        PhotoActions._takeFindQuota(fakes.store['_id'])

    with pytest.raises(ValueError, match=f'^{PhotoActions.FIND_CAP_REACHED}$'):
        tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))

    assert fakes.meta.chatCalls == []


def test_aCutOut_isTheNewProductsRowsPhoto(fakes):
    [found, _] = tools.findProductsInPhoto(ADMIN, downloadUrl(GROUP))[
        'products']

    path = photoStorage.objectPathFromUrl(found['imageUrl'], 'imageUrl')
    assert chatForms._draftPhoto(ADMIN, found['imageUrl']) == path


def test_parseBoxes_readsSamsRecords():
    text = ('<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10;c=0.9|>'
            '<|mask;x=0;y=0;data=3,3,~ab|>,1<|box;x1=5;y1=5;x2=9;y2=9;'
            'w=10;h=10|><|mask;x=0;y=0;data=5,5,~cd|>')

    first, second = samModel.parseBoxes(text)

    assert (first.left, first.top, first.right, first.bottom) == (1, 2, 4, 5)
    assert first.confidence == 0.9 and second.confidence is None
    assert (second.right, second.bottom) == (10, 10)
    assert samModel.parseBoxes('') == []
    with pytest.raises(ServiceUnavailable):
        samModel.parseBoxes('no boxes here')


def test_parseListing_keepsEachNameOnce_andCutsItsLimits():
    content = '```json\n' + json.dumps({'products': [
        {'name': 'Paper Cup', 'phrase': 'white paper cup'},
        {'name': 'paper cup', 'phrase': 'cup'},
        {'name': ' ', 'phrase': 'nothing'},
        {'name': 'x' * 200, 'phrase': 'y' * 200}]}) + '\n```'

    listed = productFinder.parseListing(content)

    assert listed[0] == ('Paper Cup', 'white paper cup')
    assert len(listed) == 2
    assert len(listed[1][0]) == 80
    assert len(listed[1][1]) == productFinder.FIND_PHRASE_MAX_LENGTH

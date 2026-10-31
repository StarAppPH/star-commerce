"""The main colors of a photo the admin attached (findColorsInPhoto,
colorFinder), the photoColors card and its brandColors answer.

No real Meta or Storage call: test_photos' FakeBucket replaces
photoStorage._bucket(), and test_product_finder's FakeMeta replaces
chatModel.client(): its responses.create answers SAM with a box per
phrase. test_chat's clock (autouse) is chatModel's time, so a deadline is
read from it."""

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

import chatCards
import chatForms
import chatModel
import colorFinder
import photoStorage
import samModel
from actions import PhotoActions
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from conftest import assertExactShape, createTestStore, wipeCollections
from mongoDb import db
from objects import PHOTO_COLORS_MAX, ChatReplyResponse, PhotoColorsResponse
from services.__request import ServiceUnavailable
from test_chat import (calls, clean, clock, frozenNow, model, offline, reply,
                       stubTool)
from test_chat_components import (adminSays, formAnswerDetails,
                                  lastUserContent, signedUpAdmin)
from test_photos import BUCKET, FakeBucket, downloadUrl
from test_product_finder import FakeMeta

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clean', 'clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = ['Users', 'Bootstrap', 'Stores', 'Policies', 'PhotoUsage',
               'Conversations', 'ChatTurns', 'ChatUsage']
ADMIN = 'cf-admin'
GREEN, RED = '#1F7A4D', '#C8102E'
# The photo: 400 x 200 on white, a green logo square (a white star in it)
# on the left, a red sign on the right.
WIDTH, HEIGHT = 400, 200
LOGO = f'users/{ADMIN}/chat/a/logo.png'
# SAM's boxes, in the photo's own pixels (its copy is no smaller).
BOXES = {'green square logo':
         '<0f>0<|box;x1=20;y1=20;x2=179;y2=179;w=400;h=200|>',
         'blue cup': ''}

api = ApiRequests()
tools = ToolRequests()


def photo(fmt: str = 'PNG') -> bytes:
    image = Image.new('RGB', (WIDTH, HEIGHT), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 179, 179), fill=GREEN)
    draw.polygon([(100, 40), (130, 150), (70, 150)], fill='white')
    draw.rectangle((240, 60, 379, 139), fill=RED)
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


def hexes(colors) -> list[str]:
    return [color['hex'] if isinstance(color, dict) else color.hex
            for color in colors]


def colorFindCounts() -> list[int]:
    return sorted(doc['count'] for doc in db.read(
        {'_id': {'$regex': f'^{PhotoActions.COLOR_FINDS_KIND}:'}},
        PhotoActions.PHOTO_USAGE))


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setenv(photoStorage.BUCKET_VARIABLE, BUCKET)
    for variable in photoStorage.EMULATOR_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    wipeCollections(COLLECTIONS)
    bucket = FakeBucket()
    meta = FakeMeta()
    meta.boxes = dict(BOXES)
    monkeypatch.setattr(photoStorage, '_bucket', lambda: bucket)
    monkeypatch.setattr(chatModel, 'client', lambda: meta)
    bucket.objects[LOGO] = {'data': photo(), 'contentType': 'image/png',
                            'metadata': None}
    yield SimpleNamespace(bucket=bucket, meta=meta)
    wipeCollections(COLLECTIONS)


# ------------------------------ colorFinder ------------------------------ #


def test_mainColors_areExact_biggestFirst_andMarkTheNeutrals():
    reading = colorFinder.readColors(photo(), None, 0)

    assert reading.focusFound is None
    assert hexes(reading.colors) == ['#FFFFFF', GREEN, RED]
    assert [c.neutral for c in reading.colors] == [True, False, False]
    assert sum(c.share for c in reading.colors) == pytest.approx(1, abs=0.02)


def test_aJpeg_isReadWithinItsCompression():
    reading = colorFinder.readColors(photo('JPEG'), None, 0)

    [_, green, red] = reading.colors
    for color, expected in ((green, GREEN), (red, RED)):
        got = Image.new('RGB', (1, 1), color.hex).getpixel((0, 0))
        want = Image.new('RGB', (1, 1), expected).getpixel((0, 0))
        assert got == pytest.approx(want, abs=6)


def test_aLosslessWebP_isReadExactly():
    out = io.BytesIO()
    Image.open(io.BytesIO(photo('PNG'))).save(out, 'WEBP', lossless=True)

    reading = colorFinder.readColors(out.getvalue(), None, 0)

    assert hexes(reading.colors) == hexes(
        colorFinder.readColors(photo('PNG'), None, 0).colors)


def test_transparentPixels_areLeftOut():
    logo = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((10, 10, 89, 89), fill=GREEN)

    reading = colorFinder.readColors(png(logo), None, 0)

    assert hexes(reading.colors) == [GREEN]
    assert reading.colors[0].share == 1
    empty = colorFinder.readColors(
        png(Image.new('RGBA', (10, 10), (0, 0, 0, 0))), None, 0)
    assert empty.colors == []


def test_blendedEdges_joinTheirColor_andSmallSharesAreDropped():
    # Drawn large and shrunk: the circle's edge is a ring of blends.
    big = Image.new('RGB', (800, 800), 'white')
    ImageDraw.Draw(big).ellipse((100, 100, 699, 699), fill=GREEN)
    # A speck under COLOR_MIN_SHARE.
    big.paste(RED, (0, 0, 40, 40))
    small = big.resize((200, 200), Image.Resampling.LANCZOS)

    reading = colorFinder.readColors(png(small), None, 0)

    assert hexes(reading.colors) == ['#FFFFFF', GREEN]


def test_atMostPhotoColorsMax_areReturned():
    stripes = Image.new('RGB', (100 * 8, 100))
    palette = ['#E6194B', '#3CB44B', '#FFE119', '#4363D8', '#F58231',
               '#911EB4', '#46F0F0', '#F032E6']
    for i, color in enumerate(palette):
        stripes.paste(color, (i * 100, 0, (i + 1) * 100, 100))

    reading = colorFinder.readColors(png(stripes), None, 0)

    assert len(reading.colors) == PHOTO_COLORS_MAX
    assert len(set(hexes(reading.colors))) == PHOTO_COLORS_MAX
    assert set(hexes(reading.colors)) <= set(palette)


def test_aFocus_readsOnlyWhatSamFound(fakes):
    reading = colorFinder.readColors(photo(), 'green square logo',
                                     chatModel.time.monotonic() + 30)

    assert reading.focusFound is True
    assert hexes(reading.colors) == [GREEN, '#FFFFFF']
    [call] = fakes.meta.samCalls
    assert call['input'][0]['content'][0]['text'] == 'green square logo'


def test_aFocusSamDoesNotFind_readsTheWholePhoto(fakes):
    reading = colorFinder.readColors(photo(), 'blue cup',
                                     chatModel.time.monotonic() + 30)

    assert reading.focusFound is False
    assert hexes(reading.colors) == ['#FFFFFF', GREEN, RED]


def test_notAPhoto_isRefused():
    with pytest.raises(ValueError):
        colorFinder.readColors(b'not a photo', None, 0)


# ---------------------------- findColorsInPhoto ---------------------------- #


def test_findColorsInPhoto_wholePhoto_takesNoQuotaAndCallsNoModel(fakes):
    createTestStore(ADMIN)

    res = tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO))

    assertExactShape(PhotoColorsResponse, res)
    assert hexes(res['colors']) == ['#FFFFFF', GREEN, RED]
    assert res['focus'] is None and res['focusFound'] is None
    assert fakes.meta.samCalls == [] and colorFindCounts() == []


def test_findColorsInPhoto_anEmptyFocus_isTheWholePhoto(fakes):
    createTestStore(ADMIN)

    res = tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), '  ')

    assert res['focus'] is None and fakes.meta.samCalls == []


def test_findColorsInPhoto_aFocus_isCountedOnce(fakes):
    createTestStore(ADMIN)

    res = tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO),
                                'green square logo')

    assertExactShape(PhotoColorsResponse, res)
    assert hexes(res['colors']) == [GREEN, '#FFFFFF']
    assert res['focus'] == 'green square logo' and res['focusFound'] is True
    # The store's counter and all stores'.
    assert colorFindCounts() == [1, 1]


def test_findColorsInPhoto_aFailedSamCall_givesTheQuotaBack(fakes):
    createTestStore(ADMIN)
    fakes.meta.boxes['green square logo'] = samModel.unavailable()

    with pytest.raises(ServiceUnavailable):
        tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), 'green square logo')

    assert colorFindCounts() == [0, 0]


def test_findColorsInPhoto_theStoreCap_refusesAFocusOnly(fakes, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(PhotoActions, 'STORE_DAILY_COLOR_FIND_CAP', 1)
    tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), 'green square logo')

    with pytest.raises(ValueError, match='^' + PhotoActions
                       .COLOR_FIND_CAP_REACHED.replace('(', r'\(') + '$'):
        tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), 'green square logo')
    assert tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO))['colors']


def test_findColorsInPhoto_turnedOff_refusesAFocus(fakes, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setenv(PhotoActions.COLOR_FIND_DAILY_CAP_ALL_VARIABLE, '0')

    with pytest.raises(ValueError, match=PhotoActions.COLOR_FINDS_TURNED_OFF
                       .split(':')[0]):
        tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), 'green square logo')


def test_findColorsInPhoto_readsOnlyTheCallersOwnPhotos(fakes):
    createTestStore(ADMIN)
    createTestStore('cf-other', name='Bakery B')
    theirs = 'users/cf-other/chat/a/logo.png'
    fakes.bucket.objects[theirs] = fakes.bucket.objects[LOGO]

    with pytest.raises(ValueError):
        tools.findColorsInPhoto(ADMIN, downloadUrl(theirs))
    with pytest.raises(ValueError):
        tools.findColorsInPhoto(ADMIN, downloadUrl(
            f'users/{ADMIN}/chat/a/logo.heic'))
    with pytest.raises(ValueError, match=PhotoActions.IMAGE_URL_MISSING):
        tools.findColorsInPhoto(ADMIN, ' ')


def test_findColorsInPhoto_aFocusTooLong_isRefused(fakes):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=PhotoActions.COLOR_FOCUS_LABEL):
        tools.findColorsInPhoto(ADMIN, downloadUrl(LOGO), 'x' * 61)


def test_findColorsInPhoto_answersAStoreNotSetUpYet(fakes, firebase):
    adminId = signedUpAdmin(firebase)
    path = f'users/{adminId}/chat/a/logo.png'
    fakes.bucket.objects[path] = fakes.bucket.objects[LOGO]

    res = tools.findColorsInPhoto(adminId, downloadUrl(path))

    assert hexes(res['colors']) == ['#FFFFFF', GREEN, RED]


# ------------------------------- the card ------------------------------- #


COLORS = {'colors': [
    {'hex': '#FFFFFF', 'share': 0.6, 'neutral': True},
    {'hex': GREEN, 'share': 0.3, 'neutral': False},
    {'hex': RED, 'share': 0.1, 'neutral': False}],
    'focus': 'green square logo', 'focusFound': True}


def test_aColorCard_isAnsweredWithItsColors(fakes, model, monkeypatch):
    createTestStore(ADMIN)
    stubTool(monkeypatch, 'admin', 'findColorsInPhoto',
             lambda userId, storeId, args: COLORS)
    model(calls(('c1', 'findColorsInPhoto',
                 json.dumps({'imageUrl': 'u', 'focus': 'green square logo'}))),
          reply('Pick your colors.'))
    res = adminSays('Use the green in my logo', userId=ADMIN)
    assertExactShape(ChatReplyResponse, res)
    [card] = res['components']
    assert card['kind'] == 'photoColors' and card['formId']
    assert {k: card[k] for k in COLORS} == COLORS

    def answer(primary, secondary):
        return adminSays(None, res['conversationId'],
                         {'kind': 'brandColors', 'formId': card['formId'],
                          'primaryColor': primary,
                          'secondaryColor': secondary}, userId=ADMIN)

    model()
    for primary, secondary, refusal in (
            ('#123456', None, chatForms.NOT_ON_THE_CARD),
            (GREEN, GREEN.lower(), chatForms.PICKED_TWICE),
            (None, None, chatForms.NOTHING_PICKED)):
        with pytest.raises(ValueError, match='^' + refusal + '$'):
            answer(primary, secondary)

    fake = model(reply('Saved.'))
    answer(GREEN.lower(), '#FFFFFF')
    content = lastUserContent(fake)
    assert content.startswith(
        f'Use {GREEN} as the primary color and #FFFFFF as the secondary '
        'color\n\n')
    assert formAnswerDetails(content) == {
        'kind': 'brandColors', 'primaryColor': GREEN,
        'secondaryColor': '#FFFFFF'}


def test_noColorsRead_makesNoCard():
    context = chatCards.CardContext('s1')
    steps = [{'role': 'assistant', 'toolCalls': [
        {'id': 'c1', 'name': 'findColorsInPhoto', 'arguments': '{}'}]},
        {'role': 'tool', 'toolCallId': 'c1', 'content': json.dumps(
            {'colors': [], 'focus': None, 'focusFound': None})}]

    assert chatCards.build(steps, {}, context) == []


def test_theSetupChat_canReadColors():
    assert 'findColorsInPhoto' in chatForms.SETUP_ROUTE_TOOLS
    assert 'findColorsInPhoto' in chatModel.CHAT_PROMPTS['adminSetup']
    assert 'findColorsInPhoto' in chatModel.CHAT_PROMPTS['admin']

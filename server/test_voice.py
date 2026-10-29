"""Voice chat (voiceModel.py, VoiceActions): smoke tests of each route and of
the HTTP layer.

No network: chatModel.client() is a fake playing one reply, the HTTP layer
is voiceModel.httpSession() replaced by a FakeSession (recording each
request, answering scripted FakeResponses), and the route tests replace
voiceModel.transcribe / speak / voiceExists / listVoices where they only
need the route. VoiceActions._now and ChatActions._now are frozen far in
the future (2099), so no TTL monitor deletes a counter mid-test."""

import base64
import datetime
import json
import struct
from types import SimpleNamespace

import pytest
import requests

import chatModel
import voiceModel
from actions import ChatActions, VoiceActions
from ApiRequests import ApiRequests
from conftest import (assertExactShape, createTestStore, storeWithProducts,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (SpokenReplyResponse, StoreThemeResponse,
                     VoiceListResponse, VoiceReplyResponse)
from services.__request import ServiceUnavailable

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Conversations', 'ChatTurns', 'ChatUsage', 'VoiceUsage'
]
ADMIN = 'vo-admin'
GUEST = 'vo-guest-uid'
UTC = datetime.timezone.utc
FAR = datetime.datetime(2099, 6, 15, 12, 0, 0, tzinfo=UTC)
DAY = datetime.date(2099, 6, 15)  # FAR in UTC+8
MP3 = b'ID3-fake-mp3-bytes'
SECRET_TRANSCRIPT = 'SENTINEL-TRANSCRIPT where is my order'
SECRET_REPLY = 'SENTINEL-REPLY **Your order** ships tomorrow.'

api = ApiRequests()


def wav(seconds: float = 1.0, rate: int = 16000, channels: int = 1,
        bits: int = 16, audioFormat: int = 1) -> bytes:
    """A RIFF/WAVE file of silence."""
    blockAlign = channels * bits // 8
    data = b'\0' * (int(seconds * rate) * blockAlign)
    fmt = struct.pack('<HHIIHH', audioFormat, channels, rate,
                      rate * blockAlign, blockAlign, bits)
    body = (b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt +
            b'data' + struct.pack('<I', len(data)) + data)
    return b'RIFF' + struct.pack('<I', len(body)) + body


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


class FakeResponse:

    def __init__(self, status: int = 200, body: bytes = b'',
                 headers: dict | None = None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        self.closed = True


class FakeSession:
    """httpSession(): answers the scripted items in order (a FakeResponse,
    or an exception to raise) and records each request's arguments."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append({'method': method, 'url': url, **kwargs})
        assert self.script, 'more requests than scripted'
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def jsonResponse(data: dict, status: int = 200) -> FakeResponse:
    return FakeResponse(status, json.dumps(data).encode(),
                        {'Content-Type': 'application/json'})


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test reaches Meta or ElevenLabs: keys set to dummies, and every
    HTTP call fails until a test installs a FakeSession."""
    monkeypatch.setenv('MODEL_API_KEY', 'test-meta-key')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-eleven-key')
    monkeypatch.setenv('ELEVENLABS_DEFAULT_VOICE_ID', 'defaultVoice1')
    for variable in ('VOICE_DAILY_SECONDS_ALL', 'VOICE_DAILY_CHARS_ALL'):
        monkeypatch.delenv(variable, raising=False)

    def noNetwork():
        raise AssertionError('a test reached for the real voice session')

    monkeypatch.setattr(voiceModel, 'httpSession', noNetwork)
    monkeypatch.setattr(VoiceActions, '_now', lambda: FAR)
    monkeypatch.setattr(ChatActions, '_now', lambda: FAR)


@pytest.fixture
def session(monkeypatch):
    """session(*script) installs and returns a FakeSession."""

    def install(*script) -> FakeSession:
        fake = FakeSession(list(script))
        monkeypatch.setattr(voiceModel, 'httpSession', lambda: fake)
        return fake

    return install


@pytest.fixture
def chatReply(monkeypatch):
    """The chat model answers SECRET_REPLY once, recording the request."""
    requests_ = []

    def create(**kwargs):
        requests_.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=SECRET_REPLY,
                                        tool_calls=None),
                finish_reason='stop')],
            usage=None)

    fake = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(chatModel, 'client', lambda: fake)
    return requests_


@pytest.fixture
def speech(monkeypatch):
    """transcribe() hears SECRET_TRANSCRIPT and speak() returns MP3, both
    recorded."""
    calls = {'transcribe': [], 'speak': []}

    def transcribe(data, deadline):
        calls['transcribe'].append(data)
        return SECRET_TRANSCRIPT

    def speak(text, voiceId, deadline):
        calls['speak'].append((text, voiceId))
        return MP3

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)
    monkeypatch.setattr(voiceModel, 'speak', speak)
    return calls


def counter(kind: str, owner: str) -> int | None:
    doc = db.read({'_id': VoiceActions.usageKey(kind, owner, DAY)},
                  'VoiceUsage', findOne=True)
    return None if doc is None else doc['count']


# ------------------------------ validWav ------------------------------ #


@pytest.mark.parametrize('rate', [16000, 24000])
def test_validWav_acceptsAMonoPcmClip(rate):
    data, seconds = voiceModel.validWav(b64(wav(2.0, rate)))

    assert data == wav(2.0, rate)
    assert seconds == 2.0


@pytest.mark.parametrize('clip, message', [
    (b64(wav(channels=2)), voiceModel.AUDIO_FORMAT),
    (b64(wav(rate=44100)), voiceModel.AUDIO_FORMAT),
    (b64(b'not a wav at all'), voiceModel.AUDIO_NOT_WAV),
    (b64(wav(0.1)), voiceModel.AUDIO_TOO_SHORT),
    ('***', voiceModel.AUDIO_NOT_BASE64),
    (123, voiceModel.AUDIO_NOT_BASE64),
], ids=['stereo', '44.1kHz', 'notRiff', 'tooShort', 'badBase64',
        'notText'])
def test_validWav_refusesWhatMetaCannotTake(clip, message):
    with pytest.raises(ValueError) as error:
        voiceModel.validWav(clip)

    assert str(error.value) == message


def test_spokenText_dropsTheMarkdown():
    assert voiceModel.spokenText(
        '## Hours\n- **Mon**: 9 to 5\n- See [our page](https://x.y/z) or '
        'https://x.y') == 'Hours. Mon: 9 to 5. See our page or link.'


# ------------------------------ the routes ------------------------------ #


def test_sendStoreVoiceMessage_answersTheTranscriptAndSpeaksTheReply(
        clean, firebase, speech, chatReply):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)

    res = api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav(1.5)))

    assertExactShape(VoiceReplyResponse, res)
    assert res['transcript'] == SECRET_TRANSCRIPT
    assert res['reply'] == SECRET_REPLY
    assert base64.b64decode(res['audio']) == MP3
    assert chatReply[0]['messages'][-1] == {'role': 'user',
                                            'content': SECRET_TRANSCRIPT}
    assert speech['speak'] == [(voiceModel.spokenText(SECRET_REPLY), None)]
    assert counter('sttCustomer', customerId) == 2
    assert counter('ttsStore', store['_id']) == len(
        voiceModel.spokenText(SECRET_REPLY))


def test_sendAdminVoiceMessage_whenSpeakingFails_stillSendsTheReply(
        clean, speech, chatReply, monkeypatch):
    store = createTestStore(ADMIN)

    def fail(text, voiceId, deadline):
        raise voiceModel.unavailable()

    monkeypatch.setattr(voiceModel, 'speak', fail)

    res = api.sendAdminVoiceMessage(ADMIN, b64(wav()))

    assertExactShape(VoiceReplyResponse, res)
    assert res['audio'] is None
    assert db.read({'conversationId': res['conversationId']}, 'ChatTurns')
    assert counter('ttsStore', store['_id']) == 0
    assert counter('sttCustomer', ADMIN) is None


def test_speakChatTurn_speaksTheSavedReply(clean, speech, chatReply):
    createTestStore(ADMIN)
    sent = api.sendAdminVoiceMessage(ADMIN, b64(wav()))

    res = api.speakChatTurn(ADMIN, sent['conversationId'], sent['turnSeq'])

    assertExactShape(SpokenReplyResponse, res)
    assert base64.b64decode(res['audio']) == MP3
    with pytest.raises(ValueError, match='^Conversation not found$'):
        api.speakChatTurn('someone-else', sent['conversationId'], 0)


def test_listVoices_offersDefaultVoices_neverClones(clean, session):
    createTestStore(ADMIN)
    fake = session(jsonResponse({
        'voices': [
            {'voice_id': 'abc123', 'name': 'Ana', 'category': 'premade',
             'labels': {'accent': 'filipino'}, 'description': 'warm',
             'preview_url': 'https://cdn.example/ana.mp3'},
            {'voice_id': 'clone1', 'name': 'Bob', 'category': 'cloned'},
        ],
        'has_more': False, 'next_page_token': None}))

    res = api.listVoices(ADMIN, search='warm')

    assertExactShape(VoiceListResponse, res)
    assert res == {'voices': [{'voiceId': 'abc123', 'name': 'Ana',
                               'description': 'warm',
                               'labels': {'accent': 'filipino'},
                               'previewUrl': 'https://cdn.example/ana.mp3'}],
                   'nextPageToken': '1:'}
    [request] = fake.requests
    assert request['url'] == voiceModel.VOICES_URL
    assert request['params']['voice_type'] == 'default'
    assert request['params']['search'] == 'warm'
    assert request['headers']['xi-api-key'] == 'test-eleven-key'
    assert request['allow_redirects'] is False


def test_updateMyStoreTheme_setsAndClearsTheVoice(clean, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(voiceModel, 'voiceExists',
                        lambda voiceId: voiceId == 'abc123')

    res = api.updateMyStoreTheme(ADMIN, voiceId='abc123')

    assertExactShape(StoreThemeResponse, res)
    assert api.getUserStore(ADMIN)['store']['voiceId'] == 'abc123'
    with pytest.raises(ValueError, match="^That voice isn't available"):
        api.updateMyStoreTheme(ADMIN, voiceId='nope')
    api.updateMyStoreTheme(ADMIN, voiceId='')
    assert 'voiceId' not in api.getUserStore(ADMIN)['store']


# ------------------------------ the HTTP layer ------------------------------ #


def test_transcribe_sendsTheMultipartRequest_andRetriesA503Once(
        session, monkeypatch):
    monkeypatch.setattr(voiceModel.time, 'sleep', lambda seconds: None)
    clip = wav()
    fake = session(FakeResponse(503),
                   jsonResponse({'sessionId': 's', 'transcript': ' hi ',
                                 'audioDurationMs': 1000, 'turns': []}))

    assert voiceModel.transcribe(clip, voiceModel.time.monotonic() + 15) == 'hi'

    assert len(fake.requests) == 2
    request = fake.requests[1]
    assert (request['method'], request['url']) == ('POST',
                                                   voiceModel.STT_URL)
    assert request['headers']['Authorization'] == 'Bearer test-meta-key'
    assert request['allow_redirects'] is False
    assert json.loads(request['files']['request'][1]) == {
        'model': 'muse-voice-transcribe-1.0', 'mode': 'PUSH_TO_TALK',
        'audioEncoding': 'WAV'}
    assert request['files']['audio'][1] == clip


def test_speak_fallsBackToTheDefaultVoice_whenTheStoreVoiceIsGone(session):
    fake = session(FakeResponse(404),
                   FakeResponse(200, MP3, {'Content-Type': 'audio/mpeg'}))

    assert voiceModel.speak('Hello.', 'goneVoice',
                            voiceModel.time.monotonic() + 15) == MP3

    assert [r['url'] for r in fake.requests] == [
        voiceModel.TTS_URL.format(voiceId='goneVoice'),
        voiceModel.TTS_URL.format(voiceId='defaultVoice1')]
    assert fake.requests[0]['json'] == {'text': 'Hello.',
                                        'model_id': 'eleven_flash_v2_5'}


def test_aConnectionErrorTwice_isUnavailable(session, monkeypatch):
    monkeypatch.setattr(voiceModel.time, 'sleep', lambda seconds: None)
    session(requests.ConnectionError('x'), requests.ConnectionError('x'))

    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hello.', None, voiceModel.time.monotonic() + 15)

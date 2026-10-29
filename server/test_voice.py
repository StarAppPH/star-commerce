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
import ipaddress
import json
import logging
import math
import socket
import struct
import threading
from types import SimpleNamespace

import pytest
import requests

import chatModel
import voiceModel
from actions import ChatActions, VoiceActions
from ApiRequests import ApiRequests
from actions import StoreActions
from actions.usageCounters import usageExpiry, usageKey
from conftest import (assertExactShape, createTestStore, createTestUser,
                      storeWithProducts, verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (ChatTurn, Conversation, SpokenReplyResponse,
                     StoreThemeResponse, VoiceListResponse,
                     VoiceReplyResponse, VoiceUsage)
from utils import generateRandomString
from services.__request import ServiceUnavailable

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Conversations', 'ChatTurns', 'ChatUsage', 'PhotoUsage',
    'VoiceUsage'
]
ADMIN = 'vo-admin'
OTHER_ADMIN = 'vo-admin-2'
OTHER_ADMIN = 'vo-admin-2'
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


LOOPBACK = {'localhost', '127.0.0.1', '::1'}
_realConnect = socket.socket.connect


@pytest.fixture(autouse=True)
def onlyLoopbackSockets(monkeypatch):
    """No test opens a socket to a non-loopback address (the local mongod and
    the conftest client use loopback ones, so connect is not blocked
    altogether)."""

    def connect(self, address):
        if isinstance(address, tuple):
            host = address[0]
            if host not in LOOPBACK and not ipaddress.ip_address(
                    host.split('%')[0]).is_loopback:
                raise AssertionError('a test opened a network socket')
        return _realConnect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)


LOOPBACK = {'localhost', '127.0.0.1', '::1'}
_realConnect = socket.socket.connect


def _isLoopback(host) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(str(host).split('%')[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def onlyLoopbackSockets(monkeypatch):
    """No test opens a socket to a non-loopback address. The local mongod
    and the conftest client use loopback ones, so connect is not blocked
    altogether."""

    def connect(self, address):
        if isinstance(address, tuple) and not _isLoopback(address[0]):
            raise AssertionError('a test opened a network socket')
        return _realConnect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)


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


# ------------------------------ more helpers ------------------------------ #

SNAPSHOT_COLLECTIONS = ['Conversations', 'ChatTurns', 'ChatUsage',
                        'VoiceUsage']
ALL = VoiceActions.ALL_STORES


class Clock:
    """A controllable time.monotonic()/sleep() for the HTTP layer."""

    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """voiceModel's and chatModel's clock (callWithRetry reads chatModel's)."""
    fake = Clock()
    monkeypatch.setattr(voiceModel, 'time', fake)
    monkeypatch.setattr(chatModel, 'time', fake)
    return fake


class ChunkedResponse(FakeResponse):
    """A body whose chunks each first run beforeChunk(index) (a test moves
    the clock there)."""

    def __init__(self, status, body, headers, beforeChunk):
        super().__init__(status, body, headers)
        self.beforeChunk = beforeChunk

    def iter_content(self, size):
        for index, start in enumerate(range(0, len(self.body), size)):
            self.beforeChunk(index)
            yield self.body[start:start + size]


def mp3Response(body: bytes = MP3) -> FakeResponse:
    return FakeResponse(200, body, {'Content-Type': 'audio/mpeg'})


def transcriptResponse(text: str = 'hello') -> FakeResponse:
    return jsonResponse({'transcript': text})


def voicesPage(*voices, hasMore: bool = False, token=None) -> FakeResponse:
    return jsonResponse({'voices': list(voices), 'has_more': hasMore,
                         'next_page_token': token})


def voice(voiceId='abc123', name='Ana', category='premade',
          **extra) -> dict:
    return {'voice_id': voiceId, 'name': name, 'category': category, **extra}


@pytest.fixture
def replySays(monkeypatch):
    """replySays(text): the chat model answers `text` (every call)."""

    def install(text: str) -> None:
        def create(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason='stop')],
                usage=None)

        fake = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(chatModel, 'client', lambda: fake)

    return install


def customerCtx(firebase):
    store = storeWithProducts(ADMIN)
    return store, verifiedGuestAt(firebase, store, GUEST)


def seedCounter(kind: str, owner: str, count: int,
                day: datetime.date = DAY) -> None:
    doc = VoiceUsage(_id=usageKey(kind, owner, day), count=count,
                     expireAt=FAR + datetime.timedelta(days=30), _version=0)
    db.create(doc.model_dump(by_alias=True), 'VoiceUsage')


def seedChatCounter(key: str, count: int) -> None:
    db.create({'_id': key, 'count': count,
               'expireAt': FAR + datetime.timedelta(days=30), '_version': 0},
              'ChatUsage')


def voiceCounters() -> dict:
    return {doc['_id']: doc['count']
            for doc in db.read({}, 'VoiceUsage') or []}


def usedCounters() -> dict:
    """The counters holding something (a refused or refunded take leaves
    its document at 0)."""
    return {key: count for key, count in voiceCounters().items() if count}


def snapshot() -> dict:
    return {name: sorted(db.read({}, name) or [], key=lambda d: d['_id'])
            for name in SNAPSHOT_COLLECTIONS}


def seedConversation(userId: str, storeId: str, audience: str = 'customer',
                     busyUntil=None) -> dict:
    conversation = Conversation(_id=generateRandomString(), userId=userId,
                                audience=audience, storeId=storeId,
                                title='hello', turnCount=0, createdAt=FAR,
                                lastMessageAt=FAR, busyUntil=busyUntil,
                                _version=0)
    return db.create(conversation.model_dump(by_alias=True), 'Conversations')


def seedTurn(conversation: dict, seq: int, replyText: str) -> dict:
    turn = ChatTurn(conversationId=conversation['_id'],
                    userId=conversation['userId'],
                    storeId=conversation['storeId'], seq=seq,
                    userText=f'q{seq}', steps=[], replyText=replyText,
                    toolCallCount=0,
                    usage={'promptTokens': 0, 'completionTokens': 0,
                           'cachedTokens': 0},
                    model=chatModel.CHAT_MODEL, createdAt=FAR, _version=0)
    return db.create(turn.model_dump(by_alias=True), 'ChatTurns')


def turnRows(conversationId: str) -> list:
    return db.read({'conversationId': conversationId}, 'ChatTurns') or []


def aware(moment: datetime.datetime) -> datetime.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def records(caplog, event: str) -> list:
    return [r for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == event]


def freeSlots(slots: threading.BoundedSemaphore) -> int:
    """How many slots are free (taken and given back)."""
    taken = 0
    while slots.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        slots.release()
    return taken


def sendAdmin(audio: str | None = None, conversationId=None) -> dict:
    return api.sendAdminVoiceMessage(ADMIN, audio or b64(wav()),
                                     conversationId)


# ------------------------------ no network ------------------------------ #


def test_theGuard_blocksANonLoopbackSocket_butNotTheDatabase(clean):
    with pytest.raises(AssertionError, match='network socket'):
        socket.create_connection(('192.0.2.1', 80), timeout=0.1)
    db.create({'_id': 'guard', 'count': 0, 'expireAt': FAR, '_version': 0},
              'VoiceUsage')
    assert voiceCounters() == {'guard': 0}


# ------------------------------ the caps ------------------------------ #


def test_theCapTable():
    assert VoiceActions.VOICE_LIMITS == {
        'stt': (1800, 6000, 'VOICE_DAILY_SECONDS_ALL', 30000),
        'tts': (20000, 100000, 'VOICE_DAILY_CHARS_ALL', 2000000),
    }
    assert VoiceActions.ALL_STORES == 'all'


@pytest.mark.parametrize('direction', ['stt', 'tts'])
@pytest.mark.parametrize('audience', ['customer', 'admin'])
def test_takeVoiceQuota_countersAndTheirKeys(clean, direction, audience):
    expected = {usageKey(f'{direction}Store', 's1', DAY): 3,
                usageKey(f'{direction}All', ALL, DAY): 3}
    customerKey = usageKey(f'{direction}Customer', 'u1', DAY)
    if audience == 'customer':
        expected[customerKey] = 3

    keys = VoiceActions._takeVoiceQuota(direction, audience, 'u1', 's1', 3)

    assert voiceCounters() == expected
    assert sorted(keys) == sorted(expected)
    if audience == 'customer':
        # keyed by the user alone: the same customer at another store shares
        # the customer counter, not the store's.
        assert 's1' not in customerKey
        VoiceActions._takeVoiceQuota(direction, audience, 'u1', 's2', 3)
        assert voiceCounters() == {
            **expected,
            customerKey: 6,
            usageKey(f'{direction}All', ALL, DAY): 6,
            usageKey(f'{direction}Store', 's2', DAY): 3,
        }


@pytest.mark.parametrize('direction', ['stt', 'tts'])
@pytest.mark.parametrize('level', ['Customer', 'Store', 'All'])
def test_takeVoiceQuota_refusesPastEachCap_andCountsNothing(
        clean, monkeypatch, direction, level):
    customerCap, storeCap, variable, _ = VoiceActions.VOICE_LIMITS[direction]
    monkeypatch.setenv(variable, '100')
    cap = {'Customer': customerCap, 'Store': storeCap, 'All': 100}[level]
    owner = {'Customer': 'u1', 'Store': 's1', 'All': ALL}[level]
    seeded = usageKey(f'{direction}{level}', owner, DAY)
    seedCounter(f'{direction}{level}', owner, cap - 4)

    with pytest.raises(ValueError) as error:
        VoiceActions._takeVoiceQuota(direction, 'customer', 'u1', 's1', 5)

    assert str(error.value) == VoiceActions.VOICE_CAP_REACHED
    assert usedCounters() == {seeded: cap - 4}
    VoiceActions._takeVoiceQuota(direction, 'customer', 'u1', 's1', 4)
    assert voiceCounters()[seeded] == cap


@pytest.mark.parametrize('direction', ['stt', 'tts'])
@pytest.mark.parametrize('bad', ['zzz-secret', '-5', '1.5', '0x10'])
def test_aBadEnvValue_fallsBackToTheDefault(clean, monkeypatch, caplog,
                                            direction, bad):
    _, _, variable, default = VoiceActions.VOICE_LIMITS[direction]
    monkeypatch.setenv(variable, bad)
    seedCounter(f'{direction}All', ALL, default - 4)

    with pytest.raises(ValueError) as error:
        VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 5)

    assert str(error.value) == VoiceActions.VOICE_CAP_REACHED
    VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 4)
    warnings = records(caplog, f'{direction}DailyCapAllInvalid')
    assert [w.msg for w in warnings] == [
        {'event': f'{direction}DailyCapAllInvalid', 'default': default}] * 2
    assert bad not in caplog.text or bad in ('-5', '1.5', '0x10')


@pytest.mark.parametrize('direction', ['stt', 'tts'])
def test_theAllCap_isReadAtEachCall(clean, monkeypatch, direction):
    variable = VoiceActions.VOICE_LIMITS[direction][2]
    monkeypatch.setenv(variable, '3')
    with pytest.raises(ValueError):
        VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 4)
    VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 3)
    monkeypatch.setenv(variable, '10')
    VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 7)
    assert voiceCounters()[usageKey(f'{direction}All', ALL, DAY)] == 10


def test_aCounter_expiresWithTheUsageExpiry(clean):
    VoiceActions._takeVoiceQuota('stt', 'customer', 'u1', 's1', 2)

    docs = db.read({}, 'VoiceUsage')
    assert len(docs) == 3
    for doc in docs:
        assert aware(doc['expireAt']) == usageExpiry(DAY)


def test_aZeroSecondsCap_turnsTranscriptionOff(clean, firebase, speech,
                                               monkeypatch):
    store, customerId = customerCtx(firebase)
    monkeypatch.setenv('VOICE_DAILY_SECONDS_ALL', '0')
    before = snapshot()

    with pytest.raises(ValueError) as error:
        api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav()))

    assert str(error.value) == VoiceActions.VOICE_TURNED_OFF
    assert speech['transcribe'] == []
    assert snapshot() == before
    assert voiceCounters() == {}


def test_aZeroCharsCap_turnsSpeechOff(clean, speech, chatReply, monkeypatch,
                                      caplog):
    caplog.set_level(logging.INFO)
    store = createTestStore(ADMIN)
    sent = sendAdmin()
    chars = len(voiceModel.spokenText(SECRET_REPLY))
    monkeypatch.setenv('VOICE_DAILY_CHARS_ALL', '0')

    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, sent['conversationId'], sent['turnSeq'])
    assert str(error.value) == VoiceActions.VOICE_TURNED_OFF
    again = sendAdmin(conversationId=sent['conversationId'])

    assertExactShape(VoiceReplyResponse, again)
    assert again['audio'] is None
    assert again['reply'] == SECRET_REPLY
    assert len(turnRows(sent['conversationId'])) == 2
    assert len(speech['speak']) == 1  # only the first message's
    [skipped] = records(caplog, 'voiceSpeakFailed')
    assert skipped.levelno == logging.INFO
    assert voiceCounters()[usageKey('ttsStore', store['_id'], DAY)] == chars


# --------------------- refusing a turn before it starts --------------------- #


def refusalCase(case: str, firebase):
    store, customerId = customerCtx(firebase)
    sid = store['_id']
    notFound = ChatActions.CONVERSATION_NOT_FOUND
    conversationId, message = None, notFound
    if case == 'badId':
        conversationId = 'no-such-id'
    elif case == 'notText':
        conversationId = 123
    elif case == 'bool':
        conversationId = True
    elif case == 'operator':
        conversationId = {'$ne': None}
    elif case == 'foreignUser':
        conversationId = seedConversation('someone-else', sid)['_id']
    elif case == 'foreignStore':
        other = storeWithProducts(OTHER_ADMIN, 'Bakery B')
        conversationId = seedConversation(customerId, other['_id'])['_id']
    elif case == 'adminAudience':
        conversationId = seedConversation(customerId, sid,
                                          audience='admin')['_id']
    elif case == 'busy':
        conversationId = seedConversation(
            customerId, sid,
            busyUntil=FAR + datetime.timedelta(seconds=60))['_id']
        message = ChatActions.STILL_ANSWERING
    elif case == 'customerCap':
        seedChatCounter(ChatActions.usageKey('customer', customerId, DAY),
                        ChatActions.CUSTOMER_DAILY_CHAT_CAP)
        message = ChatActions.CUSTOMER_CAP_REACHED
    elif case == 'storeCap':
        seedChatCounter(ChatActions.usageKey('store', sid, DAY),
                        ChatActions.STORE_DAILY_CHAT_CAP)
        message = ChatActions.STORE_CAP_REACHED
    return sid, customerId, conversationId, message


@pytest.mark.parametrize('case', [
    'badId', 'notText', 'bool', 'operator', 'foreignUser', 'foreignStore',
    'adminAudience', 'busy', 'customerCap', 'storeCap'])
def test_aTurnThatWouldBeRefused_isRefusedBeforeAnythingIsBilled(
        clean, firebase, speech, case):
    sid, customerId, conversationId, message = refusalCase(case, firebase)
    before = snapshot()

    with pytest.raises(ValueError) as error:
        api.sendStoreVoiceMessage(customerId, sid, b64(wav()),
                                  conversationId)

    assert str(error.value) == message
    assert speech['transcribe'] == [] and speech['speak'] == []
    assert snapshot() == before
    assert voiceCounters() == {}


def test_refuseTurnBeforeItStarts_readsOnly_andAdminsIgnoreChatCaps(clean):
    store = createTestStore(ADMIN)
    sid = store['_id']
    seedChatCounter(ChatActions.usageKey('customer', ADMIN, DAY),
                    ChatActions.CUSTOMER_DAILY_CHAT_CAP)
    seedChatCounter(ChatActions.usageKey('store', sid, DAY),
                    ChatActions.STORE_DAILY_CHAT_CAP)
    idle = seedConversation(ADMIN, sid, audience='admin',
                            busyUntil=FAR - datetime.timedelta(seconds=1))
    before = snapshot()

    assert ChatActions.refuseTurnBeforeItStarts('admin', ADMIN, sid,
                                                None) is None
    assert ChatActions.refuseTurnBeforeItStarts('admin', ADMIN, sid,
                                                '') is None
    assert ChatActions.refuseTurnBeforeItStarts('admin', ADMIN, sid,
                                                idle['_id']) is None
    with pytest.raises(ValueError):  # the same caps stop a customer
        ChatActions.refuseTurnBeforeItStarts('customer', ADMIN, sid, None)

    assert snapshot() == before


@pytest.mark.parametrize('failure', ['racedRefusal', 'turnRaises'])
def test_aTurnThatFailsAfterTranscribing_keepsTheSeconds(
        clean, firebase, speech, monkeypatch, failure):
    store, customerId = customerCtx(firebase)
    error = (ValueError(ChatActions.STILL_ANSWERING)
             if failure == 'racedRefusal' else RuntimeError('boom'))

    def turn(*args, **kwargs):
        raise error

    monkeypatch.setattr(ChatActions, '_slottedTurn', turn)

    with pytest.raises(type(error)):
        api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav(2.5)))

    seconds = math.ceil(2.5)
    assert voiceCounters() == {
        usageKey('sttCustomer', customerId, DAY): seconds,
        usageKey('sttStore', store['_id'], DAY): seconds,
        usageKey('sttAll', ALL, DAY): seconds,
    }
    assert db.read({}, 'ChatTurns') in ([], None)
    assert speech['speak'] == []


# ------------------------------ canonical WAV ------------------------------ #


def riff(chunks: bytes, sizeOverride: int | None = None) -> bytes:
    body = b'WAVE' + chunks
    return b'RIFF' + struct.pack(
        '<I', len(body) if sizeOverride is None else sizeOverride) + body


def fmtChunk(rate=16000, channels=1, bits=16, audioFormat=1,
             byteRate=None, blockAlign=None, size=16) -> bytes:
    blockAlign = blockAlign or channels * bits // 8
    payload = struct.pack('<HHIIHH', audioFormat, channels, rate,
                          byteRate or rate * blockAlign, blockAlign, bits)
    return b'fmt ' + struct.pack('<I', size) + payload[:size]


def dataChunk(samples: bytes, size: int | None = None) -> bytes:
    return b'data' + struct.pack(
        '<I', len(samples) if size is None else size) + samples


def chunk(chunkId: bytes, payload: bytes) -> bytes:
    return (chunkId + struct.pack('<I', len(payload)) + payload +
            (b'\0' if len(payload) % 2 else b''))


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b'\0' * (int(seconds * rate) * 2)


def test_validWav_sendsACanonical44ByteHeader_withOnlyTheSamples():
    samples = bytes(range(256)) * 125  # 32000 bytes: 1 second at 16 kHz
    messy = riff(chunk(b'LIST', b'INFOabc') + fmtChunk() +
                 chunk(b'junk', b'z' * 9) + dataChunk(samples) +
                 b'TRAILING-JUNK-AFTER-THE-DATA')

    sent, seconds = voiceModel.validWav(b64(messy))

    assert seconds == 1.0
    assert len(sent) == 44 + len(samples)
    assert sent[:4] == b'RIFF'
    assert struct.unpack_from('<I', sent, 4)[0] == len(sent) - 8
    assert sent[8:16] == b'WAVEfmt '
    assert struct.unpack_from('<IHHIIHH', sent, 16) == (
        16, 1, 1, 16000, 32000, 2, 16)
    assert sent[36:40] == b'data'
    assert struct.unpack_from('<I', sent, 40)[0] == len(samples)
    assert sent[44:] == samples
    assert b'JUNK' not in sent and b'INFO' not in sent
    # A plain file comes out byte for byte.
    assert voiceModel.validWav(b64(wav(1.0)))[0] == wav(1.0)


def test_validWav_dropsAnOddLastByte():
    sent, seconds = voiceModel.validWav(
        b64(riff(fmtChunk() + dataChunk(b'\0' * 16001))))

    assert len(sent) == 44 + 16000
    assert seconds == 0.5


REFUSED_WAVS = {
    'riffSizeZero': (riff(fmtChunk() + dataChunk(silence(1)), 0),
                     voiceModel.AUDIO_SIZES_UNSET),
    'riffSizeStreaming': (riff(fmtChunk() + dataChunk(silence(1)),
                               0xFFFFFFFF), voiceModel.AUDIO_SIZES_UNSET),
    'dataSizeStreaming': (riff(fmtChunk() +
                               dataChunk(silence(1), 0xFFFFFFFF)),
                          voiceModel.AUDIO_SIZES_UNSET),
    'dataSizeZeroWithBytes': (riff(fmtChunk() + dataChunk(silence(1), 0)),
                              voiceModel.AUDIO_SIZES_UNSET),
    'dataSizePastTheFile': (riff(fmtChunk() +
                                 dataChunk(silence(1), 40000)),
                            voiceModel.AUDIO_SIZES_UNSET),
    'headerOver4KB': (riff(chunk(b'LIST', b'x' * 5000) + fmtChunk() +
                           dataChunk(silence(1))),
                      voiceModel.AUDIO_HEADER_TOO_LONG),
    'dataStartsPastTheAllowance': (
        riff(fmtChunk() + chunk(b'LIST', b'x' * 4200) +
             dataChunk(silence(1))), voiceModel.AUDIO_HEADER_TOO_LONG),
    'chunkPastTheFile': (riff(chunk(b'LIST', b'x' * 10)[:-5]),
                         voiceModel.AUDIO_NOT_WAV),
    'dataBeforeFmt': (riff(dataChunk(silence(1)) + fmtChunk()),
                      voiceModel.AUDIO_NOT_WAV),
    'noData': (riff(fmtChunk()), voiceModel.AUDIO_NOT_WAV),
    'shortFmt': (riff(fmtChunk(size=14) + dataChunk(silence(1))),
                 voiceModel.AUDIO_NOT_WAV),
    'stereo': (riff(fmtChunk(channels=2) + dataChunk(silence(1))),
               voiceModel.AUDIO_FORMAT),
    '8bit': (riff(fmtChunk(bits=8) + dataChunk(silence(1))),
             voiceModel.AUDIO_FORMAT),
    '44.1kHz': (riff(fmtChunk(rate=44100) + dataChunk(silence(1))),
                voiceModel.AUDIO_FORMAT),
    'float': (riff(fmtChunk(audioFormat=3) + dataChunk(silence(1))),
              voiceModel.AUDIO_FORMAT),
    'wrongByteRate': (riff(fmtChunk(byteRate=1) + dataChunk(silence(1))),
                      voiceModel.AUDIO_FORMAT),
    'wrongBlockAlign': (riff(fmtChunk(blockAlign=4) +
                             dataChunk(silence(1))),
                        voiceModel.AUDIO_FORMAT),
    'tooShort': (riff(fmtChunk() + dataChunk(silence(0.29))),
                 voiceModel.AUDIO_TOO_SHORT),
    'noSamples': (riff(fmtChunk() + dataChunk(b'')),
                  voiceModel.AUDIO_TOO_SHORT),
    'over60s': (riff(fmtChunk() + dataChunk(silence(60.5))),
                voiceModel.AUDIO_TOO_LONG),
    'notWave': (b'RIFF\x04\0\0\0AVI ', voiceModel.AUDIO_NOT_WAV),
    'tinyFile': (b'RIFF', voiceModel.AUDIO_NOT_WAV),
}


@pytest.mark.parametrize('name', sorted(REFUSED_WAVS))
def test_validWav_refuses(name):
    clip, message = REFUSED_WAVS[name]

    with pytest.raises(ValueError) as error:
        voiceModel.validWav(b64(clip))

    assert str(error.value) == message


@pytest.mark.parametrize('audio', ['***', 'AAA', ' ' + b64(wav()),
                                   b64(wav()) + '\n', '', 123, None, True,
                                   b'bytes', ['x'], {'a': 1}],
                         ids=['symbols', 'badPadding', 'leadingSpace',
                              'trailingNewline', 'empty', 'int', 'none',
                              'bool', 'bytes', 'list', 'dict'])
def test_validWav_refusesWhatIsNotBase64Text(audio):
    with pytest.raises(ValueError) as error:
        voiceModel.validWav(audio)

    assert str(error.value) == voiceModel.AUDIO_NOT_BASE64


def test_validWav_checksTheBase64LengthBeforeDecoding(monkeypatch):
    def decode(*args, **kwargs):
        raise AssertionError('decoded an oversized upload')

    monkeypatch.setattr(voiceModel.base64, 'b64decode', decode)

    with pytest.raises(ValueError) as error:
        voiceModel.validWav('A' * (voiceModel.VOICE_AUDIO_MAX_BASE64 + 1))

    assert str(error.value) == voiceModel.AUDIO_TOO_LONG


def test_validWav_refusesMoreBytesThanTheCap():
    clip = riff(fmtChunk() + dataChunk(b'\0' * 2) +
                b'x' * voiceModel.VOICE_AUDIO_MAX_BYTES)

    with pytest.raises(ValueError) as error:
        voiceModel.validWav(b64(clip))

    assert str(error.value) == voiceModel.AUDIO_TOO_LONG


@pytest.mark.parametrize('seconds', [0.3, 60.0])
def test_validWav_acceptsTheBoundaries(seconds):
    sent, length = voiceModel.validWav(b64(wav(seconds, 16000)))

    assert length == seconds
    assert len(sent) == 44 + int(seconds * 16000) * 2


def test_validWav_aRefusalNeverEchoesItsInput(clean, speech):
    marker = 'SENTINEL-AUDIO-MARKER'
    clips = [riff(chunk(b'LIST', marker.encode() * 600) + fmtChunk() +
                  dataChunk(silence(1))),
             riff(fmtChunk(channels=2) + dataChunk(marker.encode())),
             marker.encode()]
    for clip in clips:
        with pytest.raises(ValueError) as error:
            voiceModel.validWav(b64(clip))
        assert marker not in str(error.value)
        assert b64(clip) not in str(error.value)
    with pytest.raises(ValueError) as error:
        voiceModel.validWav(marker + '***')
    assert marker not in str(error.value)
    createTestStore(ADMIN)
    with pytest.raises(ValueError) as error:
        api.sendAdminVoiceMessage(ADMIN, b64(clips[2]))
    assert marker not in str(error.value)
    assert speech['transcribe'] == []


# ------------------------- quota refunds and failures ------------------------- #


def sttKeys(customerId: str, storeId: str) -> list[str]:
    return [usageKey('sttCustomer', customerId, DAY),
            usageKey('sttStore', storeId, DAY), usageKey('sttAll', ALL, DAY)]


def test_nothingHeard_keepsTheSeconds(clean, firebase, monkeypatch):
    store, customerId = customerCtx(firebase)

    def transcribe(data, deadline):
        raise voiceModel.NothingHeard()

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)

    with pytest.raises(ValueError) as error:
        api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav(2.2)))

    assert str(error.value) == voiceModel.NOTHING_HEARD
    assert voiceCounters() == {key: 3
                               for key in sttKeys(customerId, store['_id'])}
    assert db.read({}, 'ChatTurns') in ([], None)
    assert db.read({}, 'ChatUsage') in ([], None)


@pytest.mark.parametrize('failure', [
    voiceModel.unavailable(), ValueError(voiceModel.RECORDING_UNREADABLE),
    RuntimeError('boom')], ids=['unavailable', 'unreadable', 'runtime'])
def test_anyOtherTranscriptionFailure_givesTheSecondsBack(
        clean, firebase, monkeypatch, failure):
    store, customerId = customerCtx(firebase)

    def transcribe(data, deadline):
        raise failure

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)

    with pytest.raises(type(failure)):
        api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav(2.2)))

    assert voiceCounters() == {key: 0
                               for key in sttKeys(customerId, store['_id'])}
    assert db.read({}, 'ChatTurns') in ([], None)
    assert db.read({}, 'ChatUsage') in ([], None)


@pytest.mark.parametrize('failure, level', [
    (voiceModel.unavailable(), logging.WARNING),
    (ValueError('x'), logging.INFO),
    (RuntimeError('boom'), logging.WARNING)],
    ids=['unavailable', 'valueError', 'runtime'])
def test_aSpeechFailure_givesTheCharactersBack_andKeepsTheReply(
        clean, speech, chatReply, monkeypatch, caplog, failure, level):
    caplog.set_level(logging.INFO)
    store = createTestStore(ADMIN)

    def fail(text, voiceId, deadline):
        raise failure

    monkeypatch.setattr(voiceModel, 'speak', fail)

    res = sendAdmin()

    assertExactShape(VoiceReplyResponse, res)
    assert res['audio'] is None
    assert res['transcript'] == SECRET_TRANSCRIPT
    assert res['reply'] == SECRET_REPLY
    [row] = turnRows(res['conversationId'])
    assert row['replyText'] == SECRET_REPLY
    assert voiceCounters()[usageKey('ttsStore', store['_id'], DAY)] == 0
    assert voiceCounters()[usageKey('ttsAll', ALL, DAY)] == 0
    assert voiceCounters()[usageKey('sttStore', store['_id'], DAY)] == 1
    [failed] = records(caplog, 'voiceSpeakFailed')
    assert failed.levelno == level
    assert failed.msg['errorClass'] == type(failure).__name__
    assert set(failed.msg) == {'event', 'conversationId', 'errorClass'}


def test_aReachedSpeechCap_leavesTheAudioNull_forTheRestOfTheDay(
        clean, speech, chatReply, caplog):
    caplog.set_level(logging.INFO)
    store = createTestStore(ADMIN)
    seedCounter('ttsStore', store['_id'], 100000)

    first = sendAdmin()
    second = sendAdmin(conversationId=first['conversationId'])

    assert first['audio'] is None and second['audio'] is None
    assert second['turnSeq'] == 1
    assert speech['speak'] == []
    assert voiceCounters()[usageKey('ttsStore', store['_id'], DAY)] == 100000
    assert [r.levelno for r in records(caplog, 'voiceSpeakFailed')] == [
        logging.INFO, logging.INFO]


def test_aReplyWithNothingToSay_isNotSpoken(clean, speech, replySays):
    replySays('```python\nprint(1)\n```')
    createTestStore(ADMIN)

    res = sendAdmin()

    assert res['audio'] is None
    assert res['reply'] == '```python\nprint(1)\n```'
    assert speech['speak'] == []
    assert not [k for k in voiceCounters() if k.startswith('tts')]


def test_aVoiceTurn_logsItsFieldsAndNoText(clean, speech, chatReply,
                                           caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN)

    res = sendAdmin(b64(wav(1.5)))

    [turn] = records(caplog, 'voiceTurn')
    assert set(turn.msg) == {'event', 'conversationId', 'audience',
                             'audioSeconds', 'spoken', 'durationMs'}
    assert turn.msg['conversationId'] == res['conversationId']
    assert turn.msg['audience'] == 'admin'
    assert turn.msg['audioSeconds'] == 1.5
    assert turn.msg['spoken'] is True
    assert isinstance(turn.msg['durationMs'], int)


def test_aConversationContinues_andEveryTurnIsSaved(clean, speech,
                                                    chatReply):
    store = createTestStore(ADMIN)

    first = sendAdmin()
    second = sendAdmin(conversationId=first['conversationId'])

    assertExactShape(VoiceReplyResponse, second)
    assert second['conversationId'] == first['conversationId']
    assert (first['turnSeq'], second['turnSeq']) == (0, 1)
    rows = sorted(turnRows(first['conversationId']), key=lambda r: r['seq'])
    assert [r['userText'] for r in rows] == [SECRET_TRANSCRIPT] * 2
    assert [r['replyText'] for r in rows] == [SECRET_REPLY] * 2
    [conversation] = db.read({'_id': first['conversationId']},
                             'Conversations')
    assert conversation['turnCount'] == 2
    assert conversation['busyUntil'] is None
    assert conversation['storeId'] == store['_id']
    assert chatReply[1]['messages'][-1]['content'] == SECRET_TRANSCRIPT
    assert len(chatReply[1]['messages']) > len(chatReply[0]['messages'])
    assert voiceCounters()[usageKey('sttStore', store['_id'], DAY)] == 2


# ------------------------------ time limits ------------------------------ #


@pytest.fixture
def voiceClock(monkeypatch):
    """VoiceActions' own time.monotonic() (it reads it directly)."""
    fake = Clock(0.0)
    monkeypatch.setattr(VoiceActions, 'time',
                        SimpleNamespace(monotonic=fake.monotonic))
    return fake


@pytest.mark.parametrize('spent, speaks', [(115.0, True), (114.9, True),
                                           (115.1, False), (119.0, False)])
def test_speechIsSkippedUnderFiveSecondsLeft(clean, voiceClock, chatReply,
                                             monkeypatch, caplog, spent,
                                             speaks):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN)
    spoken = []

    def transcribe(data, deadline):
        voiceClock.now += spent  # the transcription and turn took this long
        return SECRET_TRANSCRIPT

    def speak(text, voiceId, deadline):
        spoken.append(deadline)
        return MP3

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)
    monkeypatch.setattr(voiceModel, 'speak', speak)

    res = sendAdmin()

    assert (res['audio'] is not None) is speaks
    assert (len(spoken) == 1) is speaks
    assert bool(records(caplog, 'voiceSpeakSkipped')) is not speaks
    assert res['reply'] == SECRET_REPLY
    if not speaks:
        assert not [k for k in voiceCounters() if k.startswith('tts')]


def test_theDeadlines_followTheOneRequestBudget(clean, voiceClock,
                                                chatReply, monkeypatch):
    createTestStore(ADMIN)
    assert VoiceActions.VOICE_REQUEST_SECONDS == 15 + 90 + 15
    assert VoiceActions.TTS_MIN_SECONDS == 5
    seen = {}

    def transcribe(data, deadline):
        seen['stt'] = deadline
        voiceClock.now += seen['spent']
        return SECRET_TRANSCRIPT

    def speak(text, voiceId, deadline):
        seen['tts'] = deadline
        return MP3

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)
    monkeypatch.setattr(voiceModel, 'speak', speak)

    seen['spent'] = 20.0  # plenty left: speech has its own 15 s
    sendAdmin()
    assert seen['stt'] == 15.0
    assert seen['tts'] == 20.0 + voiceModel.TTS_DEADLINE_SECONDS

    voiceClock.now = 0.0
    seen['spent'] = 110.0  # 10 s left: speech gets what is left
    sendAdmin()
    assert seen['tts'] == 120.0


# ------------------------------ slots ------------------------------ #


def test_aVoiceTurnWithNoChatSlotFree_isRefusedBeforeAnythingIsBilled(
        clean, speech, chatReply):
    createTestStore(ADMIN)
    taken = 0
    try:
        while ChatActions._turnSlots.acquire(blocking=False):
            taken += 1
        before = snapshot()
        with pytest.raises(ServiceUnavailable):
            sendAdmin()
        assert snapshot() == before
        assert speech['transcribe'] == []
    finally:
        for _ in range(taken):
            ChatActions._turnSlots.release()
    assert freeSlots(ChatActions._turnSlots) == ChatActions.CHAT_TURNS_AT_ONCE
    sendAdmin()  # and it runs again once they are free
    assert freeSlots(ChatActions._turnSlots) == ChatActions.CHAT_TURNS_AT_ONCE


def test_aFailedVoiceTurn_releasesItsChatSlot(clean, monkeypatch):
    createTestStore(ADMIN)

    def transcribe(data, deadline):
        raise voiceModel.unavailable()

    monkeypatch.setattr(voiceModel, 'transcribe', transcribe)

    with pytest.raises(ServiceUnavailable):
        sendAdmin()

    assert freeSlots(ChatActions._turnSlots) == ChatActions.CHAT_TURNS_AT_ONCE


def test_speakChatTurn_hasItsOwnSlots(clean, speech, chatReply):
    createTestStore(ADMIN)
    sent = sendAdmin()
    chars = len(voiceModel.spokenText(SECRET_REPLY))
    taken = 0
    try:
        while VoiceActions._speakSlots.acquire(blocking=False):
            taken += 1
        assert taken == VoiceActions.SPEAK_CALLS_AT_ONCE
        with pytest.raises(ServiceUnavailable):
            api.speakChatTurn(ADMIN, sent['conversationId'],
                              sent['turnSeq'])
        assert len(speech['speak']) == 1  # only the voice message's
        assert freeSlots(ChatActions._turnSlots) == (
            ChatActions.CHAT_TURNS_AT_ONCE)
        assert voiceCounters()[usageKey('ttsAll', ALL, DAY)] == chars
    finally:
        for _ in range(taken):
            VoiceActions._speakSlots.release()
    assert freeSlots(VoiceActions._speakSlots) == (
        VoiceActions.SPEAK_CALLS_AT_ONCE)
    assert api.speakChatTurn(ADMIN, sent['conversationId'],
                             sent['turnSeq'])['audio']
    assert freeSlots(VoiceActions._speakSlots) == (
        VoiceActions.SPEAK_CALLS_AT_ONCE)


# ------------------------------ speakChatTurn ------------------------------ #


def test_speakChatTurn_anAdminWhoNoLongerOwnsTheStore_isRefused(
        clean, speech, chatReply):
    store = createTestStore(ADMIN)
    other = createTestStore(OTHER_ADMIN, 'Bakery B')
    sent = sendAdmin()
    conversationId = sent['conversationId']
    spoken = len(speech['speak'])
    before = voiceCounters()

    # The conversation belongs to another store than the admin's.
    db.db['Conversations'].update_one(
        {'_id': conversationId}, {'$set': {'storeId': other['_id']}})
    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, conversationId, 0)
    assert str(error.value) == ChatActions.CONVERSATION_NOT_FOUND
    db.db['Conversations'].update_one(
        {'_id': conversationId}, {'$set': {'storeId': store['_id']}})

    # The admin's store is gone, then the admin.
    saved = db.db['Stores'].find_one({'_id': store['_id']})
    db.db['Stores'].delete_one({'_id': store['_id']})
    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, conversationId, 0)
    assert str(error.value) == StoreActions.NO_STORE_YET
    db.db['Stores'].insert_one(saved)
    db.db['Users'].delete_one({'_id': ADMIN})
    with pytest.raises(PermissionError):
        api.speakChatTurn(ADMIN, conversationId, 0)

    assert len(speech['speak']) == spoken
    assert voiceCounters() == before


def test_speakChatTurn_aCustomerSpeaksTheirOwnTurn_inTheStoresVoice(
        clean, firebase, speech, chatReply, monkeypatch):
    store, customerId = customerCtx(firebase)
    monkeypatch.setattr(voiceModel, 'voiceExists', lambda voiceId: True)
    api.updateMyStoreTheme(ADMIN, voiceId='abc123')
    sent = api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav()))

    res = api.speakChatTurn(customerId, sent['conversationId'],
                            sent['turnSeq'])

    assertExactShape(SpokenReplyResponse, res)
    assert base64.b64decode(res['audio']) == MP3
    assert speech['speak'][-1] == (voiceModel.spokenText(SECRET_REPLY),
                                   'abc123')
    chars = len(voiceModel.spokenText(SECRET_REPLY))
    assert voiceCounters()[usageKey('ttsCustomer', customerId, DAY)] == (
        2 * chars)


def test_speakChatTurn_refusesWhoIsNotTheOwner(clean, firebase, speech,
                                               chatReply):
    store, customerId = customerCtx(firebase)
    sent = api.sendStoreVoiceMessage(customerId, store['_id'], b64(wav()))
    conversationId = sent['conversationId']
    spoken = len(speech['speak'])
    notFound = ChatActions.CONVERSATION_NOT_FOUND

    for userId, conversation in [('someone-else', conversationId),
                                 (ADMIN, conversationId),
                                 (customerId, 'no-such-id'),
                                 (customerId, 123), (customerId, None),
                                 (customerId, {'$ne': None})]:
        with pytest.raises(ValueError) as error:
            api.speakChatTurn(userId, conversation, 0)
        assert str(error.value) == notFound

    # The customer, no longer a customer.
    db.db['Customers'].delete_one({'_id': customerId})
    with pytest.raises((ValueError, PermissionError)):
        api.speakChatTurn(customerId, conversationId, 0)
    assert len(speech['speak']) == spoken


def test_ownConversation_isTheCallersOwnOnly(clean):
    store = createTestStore(ADMIN)
    mine = seedConversation(ADMIN, store['_id'], audience='admin')

    assert ChatActions.ownConversation(mine['_id'], ADMIN,
                                       {'storeId': 1})['storeId'] == (
        store['_id'])
    for userId, conversationId in [('someone-else', mine['_id']),
                                   (ADMIN, 'no-such-id'), (ADMIN, None),
                                   (ADMIN, 5)]:
        with pytest.raises(ValueError) as error:
            ChatActions.ownConversation(conversationId, userId, {'_id': 1})
        assert str(error.value) == ChatActions.CONVERSATION_NOT_FOUND


@pytest.mark.parametrize('turnSeq', [True, False, -1, 2 ** 31, 1.5, '0',
                                     None, 5, [0]])
def test_speakChatTurn_aBadOrMissingTurnSeq_isNotFound(clean, speech,
                                                       chatReply, turnSeq):
    createTestStore(ADMIN)
    sent = sendAdmin()
    spoken = len(speech['speak'])

    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, sent['conversationId'], turnSeq)

    assert str(error.value) == ChatActions.CONVERSATION_NOT_FOUND
    assert len(speech['speak']) == spoken


def test_speakChatTurn_speaksTheRequestedTurn_notAnotherUsersTurn(
        clean, speech):
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], audience='admin')
    seedTurn(conversation, 0, 'First reply.')
    seedTurn(conversation, 1, 'Second reply.')
    db.db['ChatTurns'].update_one(
        {'conversationId': conversation['_id'], 'seq': 1},
        {'$set': {'userId': 'someone-else'}})

    api.speakChatTurn(ADMIN, conversation['_id'], 0)
    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, conversation['_id'], 1)

    assert str(error.value) == ChatActions.CONVERSATION_NOT_FOUND
    assert speech['speak'] == [('First reply.', None)]


def test_speakChatTurn_aStoredReplyWithNothingToSay_isRefused(clean,
                                                              speech):
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], audience='admin')
    seedTurn(conversation, 0, '```\ncode only\n```')

    with pytest.raises(ValueError) as error:
        api.speakChatTurn(ADMIN, conversation['_id'], 0)

    assert str(error.value) == VoiceActions.NOTHING_TO_SAY
    assert speech['speak'] == []
    assert voiceCounters() == {}


def test_speakChatTurn_aSpeechFailure_givesTheCharactersBack(
        clean, monkeypatch):
    store = createTestStore(ADMIN)
    conversation = seedConversation(ADMIN, store['_id'], audience='admin')
    seedTurn(conversation, 0, 'Hello there.')

    def fail(text, voiceId, deadline):
        raise voiceModel.unavailable()

    monkeypatch.setattr(voiceModel, 'speak', fail)

    with pytest.raises(ServiceUnavailable):
        api.speakChatTurn(ADMIN, conversation['_id'], 0)

    assert voiceCounters() == {
        usageKey('ttsStore', store['_id'], DAY): 0,
        usageKey('ttsAll', ALL, DAY): 0}
    assert freeSlots(VoiceActions._speakSlots) == (
        VoiceActions.SPEAK_CALLS_AT_ONCE)


def test_speakChatTurn_logsItsFieldsAndNoText(clean, speech, chatReply,
                                              caplog):
    caplog.set_level(logging.INFO)
    createTestStore(ADMIN)
    sent = sendAdmin()

    api.speakChatTurn(ADMIN, sent['conversationId'], sent['turnSeq'])

    [spoken] = records(caplog, 'voiceSpokenTurn')
    assert set(spoken.msg) == {'event', 'conversationId', 'audience',
                               'durationMs'}
    assert spoken.msg['audience'] == 'admin'
    assert spoken.msg['conversationId'] == sent['conversationId']


# ------------------------------ listVoices ------------------------------ #


def test_listVoices_pagesTheDefaultListingThenTheSavedOne(clean, session):
    createTestStore(ADMIN)
    fake = session(
        voicesPage(voice('a1'), hasMore=True, token='tok1'),
        voicesPage(voice('a2'), hasMore=False),
        voicesPage(voice('s1'), hasMore=True, token='tok2'),
        voicesPage(voice('s2'), hasMore=False))

    first = api.listVoices(ADMIN)
    second = api.listVoices(ADMIN, pageToken=first['nextPageToken'])
    third = api.listVoices(ADMIN, pageToken=second['nextPageToken'])
    fourth = api.listVoices(ADMIN, pageToken=third['nextPageToken'])

    assert [first['nextPageToken'], second['nextPageToken'],
            third['nextPageToken'], fourth['nextPageToken']] == [
        '0:tok1', '1:', '1:tok2', None]
    assert [[v['voiceId'] for v in page['voices']]
            for page in (first, second, third, fourth)] == [
        ['a1'], ['a2'], ['s1'], ['s2']]
    assert [r['params'] for r in fake.requests] == [
        {'voice_type': 'default', 'page_size': 20,
         'include_total_count': 'false'},
        {'voice_type': 'default', 'page_size': 20,
         'next_page_token': 'tok1', 'include_total_count': 'false'},
        {'voice_type': 'saved', 'page_size': 20,
         'include_total_count': 'false'},
        {'voice_type': 'saved', 'page_size': 20,
         'next_page_token': 'tok2', 'include_total_count': 'false'},
    ]


def test_listVoices_offersOnlyPremadeAndGeneratedVoices(clean, session):
    session(voicesPage(
        voice('p1'), voice('g1', category='generated'),
        voice('c1', category='cloned'),
        voice('c2', category='professional'),
        voice('c3', category='famous'),
        {'voice_id': 'n1', 'name': 'No category'},
        voice('bad id'), voice('../x'), voice('x' * 65), voice(123),
        {'name': 'No id', 'category': 'premade'},
        voice('noname', name=''), voice('nonstr', name=7),
        'not a dict', None))

    voices = voiceModel.listVoices(None, None)[0]

    assert [v['voiceId'] for v in voices] == ['p1', 'g1']


def test_listVoices_aVoicesFieldThatIsNoList_isNoVoices(clean, session):
    session(jsonResponse({'voices': 'nope', 'has_more': False}))

    assert voiceModel.listVoices(None, None) == ([], '1:')


def test_listVoices_sanitisesThirdPartyText(clean, session):
    long = 'w' * 500
    session(voicesPage(voice(
        'v1', name='  An\x00a‮ ​Bob  Cruz  ' + long,
        description='d\x07e ' + long,
        labels={'k' * 50 + '\x00': 'v' * 80, 'ok': ' fine\n ',
                'num': 5, 'empty​': '​', 7: 'x',
                'l3': 'a', 'l4': 'a', 'l5': 'a', 'l6': 'a', 'l7': 'a'},
        preview_url='https://cdn.example/a.mp3')))

    [row] = voiceModel.listVoices(None, None)[0]

    assert row['name'].startswith('Ana Bob Cruz www')
    assert len(row['name']) == 60 and len(row['description']) == 100
    assert row['description'].startswith('de www')
    assert len(row['labels']) == 6
    assert row['labels']['k' * 20] == 'v' * 30
    assert row['labels']['ok'] == 'fine'
    assert 'num' not in row['labels'] and 'empty' not in row['labels']
    for text in (row['name'], row['description'], *row['labels'],
                 *row['labels'].values()):
        assert not any(c in text for c in '\x00\x07‮​\n\t')


@pytest.mark.parametrize('previewUrl, kept', [
    ('https://cdn.example/a.mp3', True),
    ('http://cdn.example/a.mp3', False),
    ('javascript:alert(1)', False),
    ('data:audio/mp3;base64,AAAA', False),
    ('ftp://x/y', False),
    ('//cdn.example/a.mp3', False),
    ('https://cdn.example/a b.mp3', False),
    ('https://cdn.example/a\n.mp3', False),
    ('https://cdn.example/‮a.mp3', False),
    ('https://x.example/' + 'a' * 300, False),
    (None, False), (5, False)])
def test_listVoices_previewUrlIsHttpsOnly(clean, session, previewUrl, kept):
    session(voicesPage(voice('v1', preview_url=previewUrl)))

    [row] = voiceModel.listVoices(None, None)[0]

    assert row['previewUrl'] == (previewUrl if kept else None)


def test_listVoices_isForAdminsOnly_andValidatesBeforeCalling(clean,
                                                              firebase):
    store, customerId = customerCtx(firebase)
    createTestUser('vo-nostore')

    with pytest.raises(PermissionError):
        api.listVoices(customerId)
    with pytest.raises(ValueError) as error:
        api.listVoices('vo-nostore')
    assert str(error.value) == StoreActions.NO_STORE_YET
    # (no session installed: reaching ElevenLabs would fail the test)
    for kwargs in ({'search': 'x' * 101}, {'search': 7},
                   {'pageToken': 'x' * 513}, {'pageToken': 7}):
        with pytest.raises(ValueError):
            api.listVoices(ADMIN, **kwargs)


@pytest.mark.parametrize('token', ['x', '2:abc', '0', ':abc', '0:ab cd',
                                   'a:b', '10:abc', '0:é',
                                   '0:٠'])
def test_listVoices_aTokenThatIsNotOurs_isRefused(clean, token):
    createTestStore(ADMIN)  # (no session installed: no call is made)

    with pytest.raises(ValueError) as error:
        api.listVoices(ADMIN, pageToken=token)

    assert str(error.value) == voiceModel.PAGE_TOKEN_INVALID


@pytest.mark.parametrize('theirToken', ['', 'bad token', 'a' * 511, None, 5,
                                        'é'])
@pytest.mark.parametrize('pageToken, expected', [(None, '1:'),
                                                 ('1:', None)])
def test_listVoices_aTokenWeCannotPage_isDroppedAndLogged(
        clean, session, caplog, theirToken, pageToken, expected):
    session(voicesPage(voice('v1'), hasMore=True, token=theirToken))

    voices, nextToken = voiceModel.listVoices(None, pageToken)

    assert nextToken == expected
    assert [v['voiceId'] for v in voices] == ['v1']
    [dropped] = records(caplog, 'voiceListTokenDropped')
    assert dropped.msg == {'event': 'voiceListTokenDropped',
                           'voiceType': 'default' if pageToken is None
                           else 'saved'}
    assert 'bad token' not in caplog.text


@pytest.mark.parametrize('status', [401, 403, 404])
def test_listVoices_aRefusedKey_isUnavailableWithoutARetry(clean, session,
                                                           status):
    fake = session(FakeResponse(status))

    with pytest.raises(ServiceUnavailable):
        voiceModel.listVoices(None, None)

    assert len(fake.requests) == 1


@pytest.mark.parametrize('body', [b'not json', b'[1, 2]', b'"text"',
                                  b'\xff\xfe'])
def test_listVoices_aBodyThatIsNoObject_isUnavailable(clean, session,
                                                      caplog, body):
    session(FakeResponse(200, body, {'Content-Type': 'application/json'}))

    with pytest.raises(ServiceUnavailable):
        voiceModel.listVoices(None, None)

    [bad] = records(caplog, 'voiceListError')
    assert bad.msg == {'event': 'voiceListError', 'errorClass': 'BadBody'}


# ------------------------- the store's voice (theme) ------------------------- #


def storeVersion(storeId: str) -> int:
    return db.read({'_id': storeId}, 'Stores', findOne=True)['_version']


def test_updateMyStoreTheme_setsAndClearsTheVoice(clean, session):
    store = createTestStore(ADMIN)
    fake = session(voicesPage(), voicesPage(voice('abc123',
                                                  category='generated')))
    version = storeVersion(store['_id'])

    res = api.updateMyStoreTheme(ADMIN, voiceId=' abc123 ')

    assertExactShape(StoreThemeResponse, res)
    assert [(r['params']['voice_type'], r['params']['voice_ids'])
            for r in fake.requests] == [('default', 'abc123'),
                                        ('saved', 'abc123')]
    assert api.getUserStore(ADMIN)['store']['voiceId'] == 'abc123'
    assert storeVersion(store['_id']) == version + 1
    for empty in ('', '   '):
        api.updateMyStoreTheme(ADMIN, voiceId=empty)
        assert 'voiceId' not in api.getUserStore(ADMIN)['store']
    assert len(fake.requests) == 2  # clearing asks ElevenLabs nothing
    assert storeVersion(store['_id']) == version + 3
    assert db.read({'_id': store['_id']}, 'Stores',
                   findOne=True).get('voiceId') is None


@pytest.mark.parametrize('voiceId', ['nope', '../../x', 'a b', 'x' * 65,
                                     123, True, ['abc'], {'$ne': 1}])
def test_updateMyStoreTheme_anUnavailableVoice_isRefused(clean, monkeypatch,
                                                         voiceId):
    store = createTestStore(ADMIN)
    monkeypatch.setattr(voiceModel, 'voiceExists', lambda value: False)
    version = storeVersion(store['_id'])

    with pytest.raises(ValueError) as error:
        api.updateMyStoreTheme(ADMIN, voiceId=voiceId)

    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE
    assert storeVersion(store['_id']) == version


@pytest.mark.parametrize('voiceId', ['../../x', 'a b', 'x' * 65, 123, True])
def test_updateMyStoreTheme_aBadlyShapedVoice_neverReachesElevenLabs(
        clean, voiceId):
    createTestStore(ADMIN)  # (no session installed: a call fails the test)

    with pytest.raises(ValueError) as error:
        api.updateMyStoreTheme(ADMIN, voiceId=voiceId)

    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE


def test_updateMyStoreTheme_aVoiceAloneIsAChange_andNothingIsNot(
        clean, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(voiceModel, 'voiceExists', lambda value: True)

    with pytest.raises(ValueError) as error:
        api.updateMyStoreTheme(ADMIN)
    assert str(error.value) == StoreActions.NO_THEME_SENT
    api.updateMyStoreTheme(ADMIN, voiceId='abc123')


def test_updateMyStoreTheme_checksInOrder(clean, monkeypatch):
    calls = []

    def exists(voiceId):
        calls.append(voiceId)
        return voiceId == 'good1'

    monkeypatch.setattr(voiceModel, 'voiceExists', exists)
    createTestUser('vo-nostore')

    with pytest.raises(PermissionError):  # no admin: before any lookup
        api.updateMyStoreTheme('nobody', voiceId='good1')
    assert calls == []
    with pytest.raises(ValueError) as error:  # the voice, before the store
        api.updateMyStoreTheme('vo-nostore', voiceId='bad1')
    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE
    with pytest.raises(ValueError) as error:  # nothing to change
        api.updateMyStoreTheme('vo-nostore')
    assert str(error.value) == StoreActions.NO_THEME_SENT
    with pytest.raises(ValueError) as error:  # then the store
        api.updateMyStoreTheme('vo-nostore', voiceId='good1')
    assert str(error.value) == StoreActions.NO_STORE_YET
    assert calls == ['bad1', 'good1']


def test_updateMyStoreTheme_readsTheVersionAfterTheLookup(clean,
                                                          monkeypatch):
    store = createTestStore(ADMIN)

    def exists(voiceId):  # the store changes while ElevenLabs is asked
        db.db['Stores'].update_one({'_id': store['_id']},
                                   {'$inc': {'_version': 1}})
        return True

    monkeypatch.setattr(voiceModel, 'voiceExists', exists)

    api.updateMyStoreTheme(ADMIN, voiceId='abc123')

    assert api.getUserStore(ADMIN)['store']['voiceId'] == 'abc123'


def test_voiceExists_usesOneDeadlineForBothListings(clean, session, clock,
                                                    monkeypatch):
    fake = session(voicesPage(), voicesPage(voice('abc123')))
    seen = []
    real = voiceModel._voicesPage
    start = clock.now

    def spy(params, deadline):
        seen.append(deadline)
        clock.now += 4
        return real(params, deadline)

    monkeypatch.setattr(voiceModel, '_voicesPage', spy)

    assert voiceModel.voiceExists('abc123') is True

    assert seen == [start + voiceModel.VOICES_DEADLINE_SECONDS] * 2
    assert len(fake.requests) == 2


def test_voiceExists_isFalseForAClone_andForABadIdWithoutACall(clean,
                                                               session):
    fake = session(voicesPage(voice('abc123', category='cloned')),
                   voicesPage(voice('abc123', category='professional')))

    assert voiceModel.voiceExists('abc123') is False
    assert len(fake.requests) == 2
    for bad in ('', 'a b', None, 5, '../x'):
        assert voiceModel.voiceExists(bad) is False
    assert len(fake.requests) == 2


# ------------------------------ spokenText ------------------------------ #


@pytest.mark.parametrize('reply, spoken', [
    ('Before.\n```python\nprint("SECRET")\n```\nAfter.', 'Before. After.'),
    ('Before.\n~~~\nSECRET\n~~~\nAfter.', 'Before. After.'),
    ('Before.\n  ```js\n  SECRET\n  ```\nAfter.', 'Before. After.'),
    ('Before.\n```\nSECRET never closed', 'Before.'),
    ('Use `npm start` now', 'Use npm start now.'),
    ('```\nonly code\n```', ''), ('---\n***', ''), ('', ''), (None, ''),
])
def test_spokenText_fencesAreDroppedWhole(reply, spoken):
    assert voiceModel.spokenText(reply) == spoken


def test_spokenText_cutsAtTheLastSentenceEndPastHalfTheWindow():
    first = 'x' * 1300 + '.'
    text = first + ' ' + 'y' * 1500 + '.'

    assert voiceModel.spokenText(text) == first


def test_spokenText_aSentenceEndAtOrBeforeHalf_cutsAtTheLastSpace():
    words = ' '.join(['bb'] * 1000)
    for early in ('a' * 99 + '. ', 'w' * 1249 + '. '):  # 1250 is not past
        result = voiceModel.spokenText(early + words)

        assert 1250 < len(result) <= voiceModel.SPOKEN_REPLY_MAX_CHARS
        assert result.endswith('bb') and not result.endswith(' ')
        assert (early + words).startswith(result)


def test_spokenText_withoutAnySpace_cutsAtTheWindow_andExactFitIsKept():
    assert voiceModel.spokenText('z' * 3000) == 'z' * 2500
    exact = 'q' * 2499 + '.'
    assert voiceModel.spokenText(exact) == exact


# ------------------------------ the HTTP layer ------------------------------ #


def test_httpSession_isOneSessionThatIgnoresTheEnvironment(monkeypatch):
    monkeypatch.undo()  # httpSession as built, not the guard's
    voiceModel._session = None
    try:
        first = voiceModel.httpSession()
        assert voiceModel.httpSession() is first
        assert first.trust_env is False
    finally:
        voiceModel._session = None


def test_transcribe_requestShape(session, clock):
    clip = wav()
    fake = session(transcriptResponse(' hi \n'))

    assert voiceModel.transcribe(clip, clock.now + 40) == 'hi'

    [request] = fake.requests
    assert (request['method'], request['url']) == ('POST',
                                                   voiceModel.STT_URL)
    assert request['headers'] == {'Authorization': 'Bearer test-meta-key',
                                  'Accept': 'application/json'}
    assert request['allow_redirects'] is False
    assert request['stream'] is True
    assert request['timeout'] == 15
    assert request['files']['request'] == (
        None, json.dumps({'model': 'muse-voice-transcribe-1.0',
                          'mode': 'PUSH_TO_TALK', 'audioEncoding': 'WAV'}),
        'application/json')
    assert request['files']['audio'] == ('audio.wav', clip, 'audio/wav')


def test_speak_requestShape(session, clock):
    fake = session(FakeResponse(200, MP3,
                                {'Content-Type': 'Audio/MPEG; x=y'}))

    assert voiceModel.speak('Hi.', 'storeVoice9', clock.now + 40) == MP3

    [request] = fake.requests
    assert (request['method'], request['url']) == (
        'POST', voiceModel.TTS_URL.format(voiceId='storeVoice9'))
    assert request['params'] == {'output_format': 'mp3_44100_64'}
    assert request['headers'] == {'xi-api-key': 'test-eleven-key',
                                  'Accept': 'audio/mpeg'}
    assert request['json'] == {'text': 'Hi.', 'model_id': 'eleven_flash_v2_5'}
    assert request['allow_redirects'] is False and request['stream'] is True


@pytest.mark.parametrize('voiceId', ['../../x', 'a b', '', 'x' * 65, 7,
                                     'ab\n'])
def test_speak_aBadVoiceId_neverGoesIntoAUrl(session, clock, voiceId):
    fake = session(mp3Response())

    voiceModel.speak('Hi.', voiceId, clock.now + 40)

    assert fake.requests[0]['url'] == voiceModel.TTS_URL.format(
        voiceId='defaultVoice1')


@pytest.mark.parametrize('response', [
    FakeResponse(200, MP3, {'Content-Type': 'text/html'}),
    FakeResponse(200, MP3, {}),
    FakeResponse(200, b'', {'Content-Type': 'audio/mpeg'})],
    ids=['html', 'noType', 'empty'])
def test_speak_anAnswerThatIsNoMp3_isUnavailable(session, clock, caplog,
                                                 response):
    fake = session(response)

    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', None, clock.now + 40)

    assert len(fake.requests) == 1
    assert records(caplog, 'voiceSpeakError')[0].msg['errorClass'] == 'BadBody'


CALLS = {
    'transcribe': lambda deadline: voiceModel.transcribe(wav(), deadline),
    'speak': lambda deadline: voiceModel.speak('Hi.', None, deadline),
    'listVoices': lambda deadline: voiceModel.listVoices(None, None),
}
OK = {
    'transcribe': transcriptResponse,
    'speak': mp3Response,
    'listVoices': lambda: voicesPage(voice()),
}


@pytest.mark.parametrize('call', sorted(CALLS))
@pytest.mark.parametrize('failure', [
    lambda: FakeResponse(500), lambda: FakeResponse(502),
    lambda: FakeResponse(503), lambda: FakeResponse(504),
    lambda: FakeResponse(429), lambda: requests.Timeout(),
    lambda: requests.ConnectionError('http://secret.example')],
    ids=['500', '502', '503', '504', '429', 'timeout', 'connection'])
def test_aRetryableFailure_isRetriedOnce(session, clock, call, failure):
    fake = session(failure(), OK[call]())

    CALLS[call](clock.now + 40)

    assert len(fake.requests) == 2
    assert clock.sleeps == [chatModel.RETRY_BACKOFF_SECONDS]


@pytest.mark.parametrize('call', sorted(CALLS))
def test_twoRetryableFailures_areUnavailable(session, clock, call):
    fake = session(FakeResponse(503), requests.Timeout())

    with pytest.raises(ServiceUnavailable):
        CALLS[call](clock.now + 40)

    assert len(fake.requests) == 2
    assert clock.sleeps == [1]


@pytest.mark.parametrize('call', sorted(CALLS))
@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 301])
def test_aRefusedStatus_isNeverRetried(session, clock, call, status):
    fake = session(FakeResponse(status), OK[call]())

    with pytest.raises((ServiceUnavailable, ValueError)):
        CALLS[call](clock.now + 40)

    assert len(fake.requests) == 1
    assert clock.sleeps == []


def test_transcribe_aMeta400_isAnUnreadableRecording(session, clock):
    session(FakeResponse(400))

    with pytest.raises(ValueError) as error:
        voiceModel.transcribe(wav(), clock.now + 40)

    assert str(error.value) == voiceModel.RECORDING_UNREADABLE


@pytest.mark.parametrize('status', [401, 403, 404, 413, 301])
def test_transcribe_otherRefusalsAreUnavailable(session, clock, status):
    session(FakeResponse(status))

    with pytest.raises(ServiceUnavailable):
        voiceModel.transcribe(wav(), clock.now + 40)


def test_aRetryNeedsTimeLeft(session, clock):
    fake = session(FakeResponse(503), mp3Response())

    # After the 1 s pause 4.5 s would be left: under RETRY_MIN_SECONDS.
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', None, clock.now + 5.5)

    assert len(fake.requests) == 1 and clock.sleeps == []


def test_theAttemptTimeout_followsTheDeadline(session, clock):
    fake = session(FakeResponse(503), mp3Response())
    voiceModel.speak('Hi.', None, clock.now + 8)
    assert [r['timeout'] for r in fake.requests] == [8, 7]

    fake = session(mp3Response())
    voiceModel.speak('Hi.', None, clock.now + 100)
    assert fake.requests[0]['timeout'] == 15

    fake = session()
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', None, clock.now - 1)
    assert fake.requests == []


@pytest.mark.parametrize('status', [400, 403, 404, 422])
def test_speak_aRefusedStoreVoice_fallsBackToTheDefault(session, clock,
                                                        caplog, status):
    fake = session(FakeResponse(status), mp3Response())

    assert voiceModel.speak('Hi.', 'goneVoice9', clock.now + 40) == MP3

    assert [r['url'] for r in fake.requests] == [
        voiceModel.TTS_URL.format(voiceId='goneVoice9'),
        voiceModel.TTS_URL.format(voiceId='defaultVoice1')]
    [fallback] = records(caplog, 'voiceFallback')
    assert fallback.msg == {'event': 'voiceFallback', 'status': status}
    assert 'goneVoice9' not in caplog.text and clock.sleeps == []


def test_speak_a401_isTheKey_notTheVoice(session, clock):
    fake = session(FakeResponse(401), mp3Response())

    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', 'goneVoice9', clock.now + 40)

    assert len(fake.requests) == 1


def test_speak_a429_isRetriedAsItself_notFallenBack(session, clock):
    fake = session(FakeResponse(429), mp3Response())

    voiceModel.speak('Hi.', 'goneVoice9', clock.now + 40)

    assert [r['url'] for r in fake.requests] == [
        voiceModel.TTS_URL.format(voiceId='goneVoice9')] * 2


def test_speak_aRefusedDefaultVoice_isUnavailable(session, clock):
    fake = session(FakeResponse(404), FakeResponse(404))
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', 'goneVoice9', clock.now + 40)
    assert len(fake.requests) == 2

    fake = session(FakeResponse(404))
    with pytest.raises(ServiceUnavailable):  # the store uses the default
        voiceModel.speak('Hi.', 'defaultVoice1', clock.now + 40)
    assert len(fake.requests) == 1


@pytest.mark.parametrize('default', ['', '../x', 'a b'])
def test_speak_withoutAUsableDefaultVoice_isUnavailable(session, clock,
                                                        monkeypatch, caplog,
                                                        default):
    monkeypatch.setenv('ELEVENLABS_DEFAULT_VOICE_ID', default)
    fake = session()

    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', None, clock.now + 40)

    assert fake.requests == []
    if default:
        assert records(caplog, 'voiceDefaultInvalid')
        assert default not in caplog.text


def padded(size: int) -> bytes:
    """A transcript JSON object of exactly `size` bytes."""
    empty = len(json.dumps({'transcript': 'hi', 'pad': ''}))
    return json.dumps({'transcript': 'hi',
                       'pad': 'x' * (size - empty)}).encode()


@pytest.mark.parametrize('call, limit', [
    ('transcribe', voiceModel.STT_RESPONSE_MAX_BYTES),
    ('speak', voiceModel.TTS_RESPONSE_MAX_BYTES)])
def test_theBodyCap(session, clock, call, limit):
    def body(size: int) -> FakeResponse:
        if call == 'speak':
            return mp3Response(b'x' * size)
        return FakeResponse(200, padded(size),
                            {'Content-Type': 'application/json'})

    session(body(limit))
    result = CALLS[call](clock.now + 40)
    assert result == ('hi' if call == 'transcribe' else b'x' * limit)

    fake = session(body(limit + 1), body(limit))
    with pytest.raises(ServiceUnavailable):  # too large: never retried
        CALLS[call](clock.now + 40)
    assert len(fake.requests) == 1


def test_theVoicesBodyCap(session, clock):
    big = FakeResponse(200, b'{"voices": []}' + b' ' * (
        voiceModel.VOICES_RESPONSE_MAX_BYTES), {})
    fake = session(big)

    with pytest.raises(ServiceUnavailable):
        voiceModel.listVoices(None, None)

    assert len(fake.requests) == 1


def test_aLateLastChunkIsKept_aLateEarlierChunkIsATimeout(session, clock):
    def jump(index):
        clock.now += 16  # past the attempt's 15 s

    def noJump(index):
        pass

    audio = {'Content-Type': 'audio/mpeg'}
    session(ChunkedResponse(200, b'a' * 100, audio, noJump))
    assert voiceModel.speak('Hi.', None, clock.now + 60) == b'a' * 100

    # The last (here the only) chunk lands late: the whole body is kept.
    fake = session(ChunkedResponse(200, b'b' * 100, audio, jump))
    assert voiceModel.speak('Hi.', None, clock.now + 60) == b'b' * 100
    assert len(fake.requests) == 1

    # A chunk is late with another to come: a timeout, retried.
    slow = ChunkedResponse(
        200, b'c' * (voiceModel.READ_CHUNK_BYTES + 10), audio, jump)
    fine = mp3Response()
    fake = session(slow, fine)
    assert voiceModel.speak('Hi.', None, clock.now + 60) == MP3
    assert len(fake.requests) == 2 and clock.sleeps == [1]
    assert slow.closed and fine.closed


def test_everyResponseIsClosed(session, clock):
    busy, refused, ok = FakeResponse(503), FakeResponse(404), mp3Response()
    session(busy, ok)
    voiceModel.speak('Hi.', None, clock.now + 40)
    session(refused)
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak('Hi.', None, clock.now + 40)

    assert busy.closed and ok.closed and refused.closed


@pytest.mark.parametrize('variable, call', [
    ('MODEL_API_KEY', 'transcribe'), ('ELEVENLABS_API_KEY', 'speak'),
    ('ELEVENLABS_API_KEY', 'listVoices')])
@pytest.mark.parametrize('key', ['', '   ', None, 'kéy-SECRETKEY',
                                 'with space', 'tab\there', 'new\nline'])
def test_aKeyThatCannotBeUsed_is503WithoutACall(session, clock, monkeypatch,
                                                caplog, variable, call, key):
    if key is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, key)
    fake = session()

    with pytest.raises(ServiceUnavailable) as error:
        CALLS[call](clock.now + 40)

    assert fake.requests == []
    for text in (str(error.value), caplog.text, repr(error.value.args)):
        assert 'SECRETKEY' not in text and 'with space' not in text
    if key and key.strip():
        [bad] = records(caplog, 'voiceKeyInvalid')
        assert bad.msg == {'event': 'voiceKeyInvalid', 'variable': variable}


# ------------------------------ logs ------------------------------ #


def recordText(record: logging.LogRecord) -> str:
    return ' '.join([record.getMessage(), repr(record.args),
                     repr(record.__dict__), record.exc_text or '',
                     repr(record.exc_info)])


def test_noLogLineHoldsATranscript_aReply_audio_orAKey(
        clean, session, chatReply, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    metaKey, elevenKey = 'META-KEY-SENTINEL', 'ELEVEN-KEY-SENTINEL'
    monkeypatch.setenv('MODEL_API_KEY', metaKey)
    monkeypatch.setenv('ELEVENLABS_API_KEY', elevenKey)
    monkeypatch.setattr(voiceModel.time, 'sleep', lambda seconds: None)
    body = b'BODY-SENTINEL-FROM-THE-UPSTREAM'
    clip = b64(wav())
    store = createTestStore(ADMIN)

    # success
    session(transcriptResponse(SECRET_TRANSCRIPT), mp3Response())
    ok = sendAdmin(clip)
    assert ok['audio'] and ok['reply'] == SECRET_REPLY
    # transcription failures
    session(FakeResponse(400, body))
    with pytest.raises(ValueError):
        sendAdmin(clip)
    session(FakeResponse(503, body), FakeResponse(500, body))
    with pytest.raises(ServiceUnavailable):
        sendAdmin(clip)
    session(FakeResponse(200, body))
    with pytest.raises(ServiceUnavailable):
        sendAdmin(clip)
    # a chat failure
    session(transcriptResponse(SECRET_TRANSCRIPT))

    def turn(*args, **kwargs):
        raise RuntimeError(SECRET_REPLY + SECRET_TRANSCRIPT)

    with monkeypatch.context() as patch:
        patch.setattr(ChatActions, '_slottedTurn', turn)
        with pytest.raises(RuntimeError):
            sendAdmin(clip)
    # a speech failure
    session(transcriptResponse(SECRET_TRANSCRIPT), FakeResponse(500, body),
            FakeResponse(500, body))
    assert sendAdmin(clip)['audio'] is None
    # the store voice fallback
    db.db['Stores'].update_one({'_id': store['_id']},
                               {'$set': {'voiceId': 'goneVoice9'}})
    session(transcriptResponse(SECRET_TRANSCRIPT), FakeResponse(404, body),
            mp3Response())
    assert sendAdmin(clip)['audio']
    # voices
    session(voicesPage(voice(name='SENTINEL-VOICE-NAME', description='d'),
                       hasMore=True, token='bad token'))
    api.listVoices(ADMIN)

    assert records(caplog, 'voiceTurn') and records(caplog, 'voiceFallback')
    forbidden = [SECRET_TRANSCRIPT, SECRET_REPLY, clip, b64(MP3), metaKey,
                 elevenKey, body.decode(), 'SENTINEL-VOICE-NAME',
                 'bad token']
    for record in caplog.records:
        text = recordText(record)
        for secret in forbidden:
            assert secret not in text, (secret, record.getMessage())


# ------------------------------ TTL indexes ------------------------------ #

TTL_COLLECTIONS = ['ChatUsage', 'PhotoUsage', 'VoiceUsage']


@pytest.fixture
def usageIndexes():
    for name in TTL_COLLECTIONS:
        db.db[name].drop_indexes()
    yield
    db.ensureIndexes()


def ttlIndexes(collection: str) -> list[dict]:
    return [info for info in db.db[collection].index_information().values()
            if info['key'] == [('expireAt', 1)]]


def test_ensureIndexes_buildsTheDailyCounterTtlIndexes(usageIndexes):
    for name in TTL_COLLECTIONS:
        assert ttlIndexes(name) == []

    db.ensureIndexes()

    for name in TTL_COLLECTIONS:
        [index] = ttlIndexes(name)
        assert index['expireAfterSeconds'] == 0
    built = {name: db.db[name].index_information()
             for name in TTL_COLLECTIONS}
    db.ensureIndexes()  # a second run changes nothing
    assert {name: db.db[name].index_information()
            for name in TTL_COLLECTIONS} == built

"""The store's voice (voiceModel.py, VoiceActions): the daily speech caps,
listVoices, the store's voice (updateMyStoreTheme), spokenText and the HTTP
layer. Voice mode's streamed replies are test_voice_stream.py's, the
thinking texts' clips test_chat_thinking.py's.

No network: the HTTP layer is voiceModel.httpSession() replaced by a
FakeSession (recording each request, answering scripted FakeResponses),
and the tool tests replace voiceModel.voiceExists / listVoices where they
only need the tool. VoiceActions._now and ChatActions._now are frozen far
in the future (2099), so no TTL monitor deletes a counter mid-test."""

import base64
import datetime
import ipaddress
import json
import logging
import socket

import pytest
import requests

import chatModel
import voiceModel
from actions import ChatActions, VoiceActions
from ApiRequests import ApiRequests
from ToolRequests import ToolRequests
from actions import StoreActions
from actions.usageCounters import usageExpiry, usageKey
from conftest import (assertExactShape, createTestStore, createTestUser,
                      storeWithProducts, verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import (Conversation, StoreThemeResponse,
                     VoiceListResponse, VoiceUsage)
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
SECRET_REPLY = 'SENTINEL-REPLY **Your order** ships tomorrow.'

api = ApiRequests()
tools = ToolRequests()


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

    res = tools.listVoices(ADMIN, search='warm')

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


# ------------------------------ more helpers ------------------------------ #

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


def voicesPage(*voices, hasMore: bool = False, token=None) -> FakeResponse:
    return jsonResponse({'voices': list(voices), 'has_more': hasMore,
                         'next_page_token': token})


def voice(voiceId='abc123', name='Ana', category='premade',
          **extra) -> dict:
    return {'voice_id': voiceId, 'name': name, 'category': category, **extra}


def customerCtx(firebase):
    store = storeWithProducts(ADMIN)
    return store, verifiedGuestAt(firebase, store, GUEST)


def seedCounter(kind: str, owner: str, count: int,
                day: datetime.date = DAY) -> None:
    doc = VoiceUsage(_id=usageKey(kind, owner, day), count=count,
                     expireAt=FAR + datetime.timedelta(days=30), _version=0)
    db.create(doc.model_dump(by_alias=True), 'VoiceUsage')


def voiceCounters() -> dict:
    return {doc['_id']: doc['count']
            for doc in db.read({}, 'VoiceUsage') or []}


def usedCounters() -> dict:
    """The counters holding something (a refused or refunded take leaves
    its document at 0)."""
    return {key: count for key, count in voiceCounters().items() if count}


def seedConversation(userId: str, storeId: str, audience: str = 'customer',
                     busyUntil=None) -> dict:
    conversation = Conversation(_id=generateRandomString(), userId=userId,
                                audience=audience, storeId=storeId,
                                title='hello', turnCount=0, createdAt=FAR,
                                lastMessageAt=FAR, busyUntil=busyUntil,
                                _version=0)
    return db.create(conversation.model_dump(by_alias=True), 'Conversations')


def aware(moment: datetime.datetime) -> datetime.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def records(caplog, event: str) -> list:
    return [r for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == event]


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
        'tts': (20000, 100000, 'VOICE_DAILY_CHARS_ALL', 2000000),
    }
    assert VoiceActions.ALL_STORES == 'all'


@pytest.mark.parametrize('direction', ['tts'])
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
    for doc in db.read({}, 'VoiceUsage'):
        assert aware(doc['expireAt']) == usageExpiry(DAY)
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


@pytest.mark.parametrize('direction', ['tts'])
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


@pytest.mark.parametrize('direction', ['tts'])
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


@pytest.mark.parametrize('direction', ['tts'])
def test_theAllCap_isReadAtEachCall(clean, monkeypatch, direction):
    variable = VoiceActions.VOICE_LIMITS[direction][2]
    monkeypatch.setenv(variable, '3')
    with pytest.raises(ValueError):
        VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 4)
    VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 3)
    monkeypatch.setenv(variable, '10')
    VoiceActions._takeVoiceQuota(direction, 'admin', 'u1', 's1', 7)
    assert voiceCounters()[usageKey(f'{direction}All', ALL, DAY)] == 10


# ------------------------------ ownConversation ------------------------------ #


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


# ------------------------------ listVoices ------------------------------ #


def test_listVoices_pagesTheDefaultListingThenTheSavedOne(clean, session):
    createTestStore(ADMIN)
    fake = session(
        voicesPage(voice('a1'), hasMore=True, token='tok1'),
        voicesPage(voice('a2'), hasMore=False),
        voicesPage(voice('s1'), hasMore=True, token='tok2'),
        voicesPage(voice('s2'), hasMore=False))

    first = tools.listVoices(ADMIN)
    second = tools.listVoices(ADMIN, pageToken=first['nextPageToken'])
    third = tools.listVoices(ADMIN, pageToken=second['nextPageToken'])
    fourth = tools.listVoices(ADMIN, pageToken=third['nextPageToken'])

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
        tools.listVoices(customerId)
    with pytest.raises(ValueError) as error:
        tools.listVoices('vo-nostore')
    assert str(error.value) == StoreActions.NO_STORE_YET
    # (no session installed: reaching ElevenLabs would fail the test)
    for kwargs in ({'search': 'x' * 101}, {'search': 7},
                   {'pageToken': 'x' * 513}, {'pageToken': 7}):
        with pytest.raises(ValueError):
            tools.listVoices(ADMIN, **kwargs)


@pytest.mark.parametrize('token', ['x', '2:abc', '0', ':abc', '0:ab cd',
                                   'a:b', '10:abc', '0:é',
                                   '0:٠'])
def test_listVoices_aTokenThatIsNotOurs_isRefused(clean, token):
    createTestStore(ADMIN)  # (no session installed: no call is made)

    with pytest.raises(ValueError) as error:
        tools.listVoices(ADMIN, pageToken=token)

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


@pytest.mark.parametrize('response, logged', [
    (FakeResponse(401), False), (FakeResponse(403), False),
    (FakeResponse(404), False),
    (FakeResponse(200, b'not json', {}), True),
    (FakeResponse(200, b'[1, 2]', {}), True),
    (FakeResponse(200, b'"text"', {}), True),
    (FakeResponse(200, b'\xff\xfe', {}), True)],
    ids=['401', '403', '404', 'notJson', 'list', 'text', 'badBytes'])
def test_listVoices_aRefusedKeyOrABodyThatIsNoObject_isUnavailable(
        clean, session, caplog, response, logged):
    fake = session(response)

    with pytest.raises(ServiceUnavailable):
        voiceModel.listVoices(None, None)

    assert len(fake.requests) == 1  # never retried
    if logged:
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

    res = tools.updateMyStoreTheme(ADMIN, voiceId=' abc123 ')

    assertExactShape(StoreThemeResponse, res)
    assert [(r['params']['voice_type'], r['params']['voice_ids'])
            for r in fake.requests] == [('default', 'abc123'),
                                        ('saved', 'abc123')]
    assert tools.getUserStore(ADMIN)['store']['voiceId'] == 'abc123'
    assert storeVersion(store['_id']) == version + 1
    for empty in ('', '   '):
        tools.updateMyStoreTheme(ADMIN, voiceId=empty)
        assert 'voiceId' not in tools.getUserStore(ADMIN)['store']
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
        tools.updateMyStoreTheme(ADMIN, voiceId=voiceId)

    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE
    assert storeVersion(store['_id']) == version


@pytest.mark.parametrize('voiceId', ['../../x', 'a b', 'x' * 65, 123, True])
def test_updateMyStoreTheme_aBadlyShapedVoice_neverReachesElevenLabs(
        clean, voiceId):
    createTestStore(ADMIN)  # (no session installed: a call fails the test)

    with pytest.raises(ValueError) as error:
        tools.updateMyStoreTheme(ADMIN, voiceId=voiceId)

    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE


def test_updateMyStoreTheme_aVoiceAloneIsAChange_andNothingIsNot(
        clean, monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(voiceModel, 'voiceExists', lambda value: True)

    with pytest.raises(ValueError) as error:
        tools.updateMyStoreTheme(ADMIN)
    assert str(error.value) == StoreActions.NO_THEME_SENT
    tools.updateMyStoreTheme(ADMIN, voiceId='abc123')


def test_updateMyStoreTheme_checksInOrder(clean, monkeypatch):
    calls = []

    def exists(voiceId):
        calls.append(voiceId)
        return voiceId == 'good1'

    monkeypatch.setattr(voiceModel, 'voiceExists', exists)
    createTestUser('vo-nostore')

    with pytest.raises(PermissionError):  # no admin: before any lookup
        tools.updateMyStoreTheme('nobody', voiceId='good1')
    assert calls == []
    with pytest.raises(ValueError) as error:  # the voice, before the store
        tools.updateMyStoreTheme('vo-nostore', voiceId='bad1')
    assert str(error.value) == StoreActions.VOICE_NOT_AVAILABLE
    with pytest.raises(ValueError) as error:  # nothing to change
        tools.updateMyStoreTheme('vo-nostore')
    assert str(error.value) == StoreActions.NO_THEME_SENT
    with pytest.raises(ValueError) as error:  # then the store
        tools.updateMyStoreTheme('vo-nostore', voiceId='good1')
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

    tools.updateMyStoreTheme(ADMIN, voiceId='abc123')

    assert tools.getUserStore(ADMIN)['store']['voiceId'] == 'abc123'


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


FIRST_SENTENCE = 'x' * 1300 + '.'
SPOKEN = [
    ('## Hours\n- **Mon**: 9 to 5\n- See [our page](https://x.y/z) or '
     'https://x.y', 'Hours. Mon: 9 to 5. See our page or link.'),
    ('Before.\n```python\nprint("SECRET")\n```\nAfter.', 'Before. After.'),
    ('Before.\n~~~\nSECRET\n~~~\nAfter.', 'Before. After.'),
    ('Before.\n  ```js\n  SECRET\n  ```\nAfter.', 'Before. After.'),
    ('Before.\n```\nSECRET never closed', 'Before.'),
    ('Use `npm start` now', 'Use npm start now.'),
    ('```\nonly code\n```', ''), ('---\n***', ''), ('', ''), (None, ''),
    # past 2500: the last sentence end past half the window, else the
    # window itself (no space), and an exact fit is kept.
    (FIRST_SENTENCE + ' ' + 'y' * 1500 + '.', FIRST_SENTENCE),
    ('z' * 3000, 'z' * 2500),
    ('q' * 2499 + '.', 'q' * 2499 + '.'),
]


@pytest.mark.parametrize('reply, spoken', SPOKEN)
def test_spokenText(reply, spoken):
    assert voiceModel.spokenText(reply) == spoken


def test_spokenText_aSentenceEndAtOrBeforeHalf_cutsAtTheLastSpace():
    words = ' '.join(['bb'] * 1000)
    for early in ('a' * 99 + '. ', 'w' * 1249 + '. '):  # 1250 is not past
        result = voiceModel.spokenText(early + words)

        assert 1250 < len(result) <= voiceModel.SPOKEN_REPLY_MAX_CHARS
        assert result.endswith('bb') and not result.endswith(' ')
        assert (early + words).startswith(result)


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
    'speak': lambda deadline: voiceModel.speak('Hi.', None, deadline),
    'listVoices': lambda deadline: voiceModel.listVoices(None, None),
}
OK = {
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


@pytest.mark.parametrize('voiceId, script, urls, result', [
    # a 401 is the key, not the voice: no fallback
    ('goneVoice9', [FakeResponse(401)], ['goneVoice9'], None),
    # a 429 is retried as itself, not fallen back
    ('goneVoice9', [FakeResponse(429), mp3Response()],
     ['goneVoice9'] * 2, MP3),
    # the default refused too
    ('goneVoice9', [FakeResponse(404), FakeResponse(404)],
     ['goneVoice9', 'defaultVoice1'], None),
    # a store using the default has nothing to fall back to
    ('defaultVoice1', [FakeResponse(404)], ['defaultVoice1'], None)],
    ids=['401', '429', 'defaultRefused', 'storeUsesTheDefault'])
def test_speak_otherOutcomesOfARefusedStoreVoice(session, clock, voiceId,
                                                 script, urls, result):
    fake = session(*script)

    if result is None:
        with pytest.raises(ServiceUnavailable):
            voiceModel.speak('Hi.', voiceId, clock.now + 40)
    else:
        assert voiceModel.speak('Hi.', voiceId, clock.now + 40) == result

    assert [r['url'] for r in fake.requests] == [
        voiceModel.TTS_URL.format(voiceId=v) for v in urls]


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


@pytest.mark.parametrize('call, limit', [
    ('speak', voiceModel.TTS_RESPONSE_MAX_BYTES),
    ('listVoices', voiceModel.VOICES_RESPONSE_MAX_BYTES)])
def test_theBodyCap(session, clock, call, limit):
    def body(size: int) -> FakeResponse:
        if call == 'speak':
            return mp3Response(b'x' * size)
        empty = b'{"voices": []}'
        return FakeResponse(200, empty + b' ' * (size - len(empty)),
                            {'Content-Type': 'application/json'})

    session(body(limit))
    result = CALLS[call](clock.now + 40)
    assert result == {'speak': b'x' * limit,
                      'listVoices': ([], '1:')}[call]

    fake = session(body(limit + 1), body(limit))
    with pytest.raises(ServiceUnavailable):  # too large: never retried
        CALLS[call](clock.now + 40)
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
    ('ELEVENLABS_API_KEY', 'speak'), ('ELEVENLABS_API_KEY', 'listVoices')])
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


def test_noLogLineHoldsAReply_audio_orAKey(clean, session, clock,
                                          monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    elevenKey = 'ELEVEN-KEY-SENTINEL'
    monkeypatch.setenv('ELEVENLABS_API_KEY', elevenKey)
    body = b'BODY-SENTINEL-FROM-THE-UPSTREAM'
    createTestStore(ADMIN)

    # success
    session(mp3Response())
    assert voiceModel.speak(SECRET_REPLY, None, clock.now + 40) == MP3
    # speech failures
    session(FakeResponse(500, body), FakeResponse(500, body))
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak(SECRET_REPLY, None, clock.now + 40)
    session(FakeResponse(200, body))
    with pytest.raises(ServiceUnavailable):
        voiceModel.speak(SECRET_REPLY, None, clock.now + 40)
    # the store voice fallback
    session(FakeResponse(404, body), mp3Response())
    assert voiceModel.speak(SECRET_REPLY, 'goneVoice9', clock.now + 40)
    # voices
    session(voicesPage(voice(name='SENTINEL-VOICE-NAME', description='d'),
                       hasMore=True, token='bad token'))
    tools.listVoices(ADMIN)

    assert records(caplog, 'voiceFallback')
    forbidden = [SECRET_REPLY, b64(MP3), elevenKey, body.decode(),
                 'SENTINEL-VOICE-NAME', 'bad token']
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

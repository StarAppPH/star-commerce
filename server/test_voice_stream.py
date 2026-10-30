"""A reply spoken straight from ElevenLabs: the chat routes' `speak` asks for
`speech` (VoiceActions.replySpeech), a single-use token
(voiceModel.speechToken) with the store's voice and the spoken text,
counted against the per-minute token caps and the daily character caps."""

import requests
import pytest

import voiceModel
from actions import VoiceActions
from ApiRequests import ApiRequests
from conftest import (assertExactShape, createTestStore, storeWithProducts,
                      verifiedGuestAt, wipeCollections)
from mongoDb import db
from objects import ChatReplyResponse, ChatSpeech
from test_chat import (ADMIN, DAY, FAR, GUEST, clock, frozenNow, model,
                       offline, reply)
from test_voice import FakeResponse, jsonResponse

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Conversations', 'ChatTurns', 'ChatUsage', 'VoiceUsage'
]
DEFAULT_VOICE = 'defaultVoice1'
STORE_VOICE = 'storeVoice1'
TOKEN = 'SENTINEL-single-use-token'
REPLY = 'Your **order** ships tomorrow.'
MINUTE = FAR.strftime('%Y-%m-%dT%H:%M')
api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


@pytest.fixture
def voice(monkeypatch):
    """ElevenLabs gives TOKEN for every token request, each one counted."""
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-eleven-key')
    monkeypatch.setenv('ELEVENLABS_DEFAULT_VOICE_ID', DEFAULT_VOICE)
    monkeypatch.delenv('VOICE_DAILY_CHARS_ALL', raising=False)
    monkeypatch.setattr(VoiceActions, '_now', lambda: FAR)
    made = []

    def speechToken():
        made.append(TOKEN)
        return TOKEN

    monkeypatch.setattr(voiceModel, 'speechToken', speechToken)
    return made


def counter(key: str) -> int | None:
    doc = db.read({'_id': key}, 'VoiceUsage', findOne=True)
    return None if doc is None else doc['count']


def daily(kind: str, owner: str) -> int | None:
    return counter(VoiceActions.usageKey(kind, owner, DAY))


# ----------------------------- the routes ----------------------------- #


@pytest.mark.parametrize('speak', [None, False])
def test_withoutSpeak_thereIsNoSpeech_andNoToken(clean, voice, model, speak):
    createTestStore(ADMIN)
    model(reply(REPLY))

    res = api.sendAdminChatMessage(ADMIN, 'Where is my order?', None, None,
                                   None, None, speak)

    assertExactShape(ChatReplyResponse, res)
    assert res['speech'] is None
    assert voice == []
    assert db.read({}, 'VoiceUsage') == []


def test_speak_givesATokenTheStoresVoiceAndTheSpokenText(clean, voice,
                                                          model):
    store = createTestStore(ADMIN)
    db.findOneAndUpdate({'_id': store['_id']},
                        {'$set': {'voiceId': STORE_VOICE}}, 'Stores')
    model(reply(REPLY))

    res = api.sendAdminChatMessage(ADMIN, 'Where is my order?', None, None,
                                   None, None, True)

    assertExactShape(ChatReplyResponse, res)
    text = voiceModel.spokenText(REPLY)
    assert res['speech'] == {'token': TOKEN, 'voiceId': STORE_VOICE,
                             'modelId': voiceModel.TTS_MODEL, 'text': text}
    assertExactShape(ChatSpeech, res['speech'])
    assert '**' not in text
    # An admin counts against the store's and all stores' characters only.
    assert daily('ttsStore', store['_id']) == len(text)
    assert daily('ttsCustomer', ADMIN) is None
    assert counter(f'speechTokenUser:{ADMIN}:{MINUTE}') == 1
    assert counter(f'speechTokenStore:{store["_id"]}:{MINUTE}') == 1


def test_aCustomersSpeech_countsAgainstTheirOwnCapToo(clean, firebase, voice,
                                                      model):
    store = storeWithProducts(ADMIN)
    customerId = verifiedGuestAt(firebase, store, GUEST)
    model(reply(REPLY))

    res = api.sendStoreChatMessage(customerId, store['_id'],
                                   'Where is my order?', None, None, None,
                                   None, True)

    assert res['speech']['voiceId'] == DEFAULT_VOICE
    text = voiceModel.spokenText(REPLY)
    assert daily('ttsCustomer', customerId) == len(text)
    assert daily('ttsStore', store['_id']) == len(text)


def test_pastThePerMinuteCap_theReplyComesUnspoken(clean, voice, model,
                                                   monkeypatch):
    store = createTestStore(ADMIN)
    monkeypatch.setattr(VoiceActions, 'SPEECH_TOKENS_PER_MINUTE_USER', 2)
    model(*[reply(REPLY) for _ in range(3)])

    spoken = [api.sendAdminChatMessage(ADMIN, f'Question {i}', None, None,
                                       None, None, True)['speech']
              for i in range(3)]

    assert [s is not None for s in spoken] == [True, True, False]
    assert len(voice) == 2
    assert counter(f'speechTokenUser:{ADMIN}:{MINUTE}') == 2
    assert daily('ttsStore', store['_id']) == 2 * len(
        voiceModel.spokenText(REPLY))


def test_pastTheDailyCharacterCap_theReplyComesUnspoken(clean, voice, model,
                                                        monkeypatch):
    store = createTestStore(ADMIN)
    monkeypatch.setenv('VOICE_DAILY_CHARS_ALL', '0')
    model(reply(REPLY))

    res = api.sendAdminChatMessage(ADMIN, 'Where is my order?', None, None,
                                   None, None, True)

    assert res['reply'] == REPLY and res['speech'] is None
    assert voice == []
    assert counter(f'speechTokenUser:{ADMIN}:{MINUTE}') in (None, 0)
    assert daily('ttsStore', store['_id']) in (None, 0)


def test_aFailingTokenRequest_givesEveryCounterBack(clean, voice, model,
                                                    monkeypatch, caplog):
    store = createTestStore(ADMIN)

    def unavailable():
        raise voiceModel.unavailable()

    monkeypatch.setattr(voiceModel, 'speechToken', unavailable)
    model(reply(REPLY))

    res = api.sendAdminChatMessage(ADMIN, 'Where is my order?', None, None,
                                   None, None, True)

    assert res['reply'] == REPLY and res['speech'] is None
    assert counter(f'speechTokenUser:{ADMIN}:{MINUTE}') == 0
    assert counter(f'speechTokenStore:{store["_id"]}:{MINUTE}') == 0
    assert daily('ttsStore', store['_id']) == 0
    assert any(isinstance(r.msg, dict)
               and r.msg.get('event') == 'voiceTokenSkipped'
               for r in caplog.records)


def test_aReplyWithNothingToSay_getsNoToken(clean, voice, model,
                                            monkeypatch):
    createTestStore(ADMIN)
    monkeypatch.setattr(voiceModel, 'spokenText', lambda reply: '')
    model(reply(REPLY))

    res = api.sendAdminChatMessage(ADMIN, 'Where is my order?', None, None,
                                   None, None, True)

    assert res['speech'] is None and voice == []


# ----------------------------- voiceModel.speechToken ----------------------------- #


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-eleven-key')

    def install(*script):
        from test_voice import FakeSession
        fake = FakeSession(list(script))
        monkeypatch.setattr(voiceModel, 'httpSession', lambda: fake)
        return fake

    return install


def test_speechToken_asksElevenLabsWithOurKey(session, caplog):
    fake = session(jsonResponse({'token': TOKEN}))

    assert voiceModel.speechToken() == TOKEN

    [request] = fake.requests
    assert request['method'] == 'POST'
    assert request['url'] == voiceModel.SPEECH_TOKEN_URL
    assert request['headers'] == {'xi-api-key': 'test-eleven-key'}
    assert request['allow_redirects'] is False
    assert TOKEN not in caplog.text


@pytest.mark.parametrize('body', [
    {}, {'token': ''}, {'token': 7}, {'token': 'has space'},
    {'token': 'x' * (voiceModel.SPEECH_TOKEN_MAX_LENGTH + 1)},
])
def test_speechToken_refusesABadBody(session, body):
    session(jsonResponse(body))

    with pytest.raises(Exception) as error:
        voiceModel.speechToken()

    assert type(error.value) is type(voiceModel.unavailable())


def test_speechToken_retriesOnceOnA5xx_andRefusesA4xx(session):
    fake = session(FakeResponse(503), jsonResponse({'token': TOKEN}))
    assert voiceModel.speechToken() == TOKEN
    assert len(fake.requests) == 2

    session(FakeResponse(401))
    with pytest.raises(Exception) as error:
        voiceModel.speechToken()
    assert type(error.value) is type(voiceModel.unavailable())


def test_speechToken_withoutAKey_isUnavailable(session, monkeypatch):
    monkeypatch.delenv('ELEVENLABS_API_KEY')

    with pytest.raises(Exception) as error:
        voiceModel.speechToken()

    assert type(error.value) is type(voiceModel.unavailable())


def test_aConnectionError_isRetriedOnce(session):
    fake = session(requests.ConnectionError('down'),
                   jsonResponse({'token': TOKEN}))

    assert voiceModel.speechToken() == TOKEN
    assert len(fake.requests) == 2

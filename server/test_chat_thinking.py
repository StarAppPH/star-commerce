"""The thinking texts: each @mcp_tool's (mcpTools), what a chat turn is
doing while it runs (chatThinking.note/clear, getChatThinking), that text
spoken once per voice and reused (speakChatThinking), and the clips kept in
step with the texts (syncThinkingSpeech)."""

import base64

import pytest

import chatThinking
import chatTools
import voiceModel
from actions import VoiceActions
from ApiRequests import ApiRequests
from conftest import assertExactShape, createTestStore, wipeCollections
from mcpCreator import toolSpecs
from mcpTools import MIN_THINKING_TEXTS, mcp_tool
from mongoDb import db
from objects import ChatThinkingResponse, SpokenThinkingResponse
from test_chat import (ADMIN, DAY, FAR, OTHER_ADMIN, calls, clock, frozenNow,
                       model, offline, reply)

# The fixtures imported above are used by name (pytest finds them here).
__all__ = ['clock', 'frozenNow', 'model', 'offline']

COLLECTIONS = [
    'Users', 'Bootstrap', 'Stores', 'Policies', 'Customers', 'Products',
    'Orders', 'Conversations', 'ChatTurns', 'ChatUsage', 'VoiceUsage',
    'ChatThinking', 'ThinkingSpeech'
]
TURN_KEY = 'turn-key-0123456789abcdef'
MP3 = b'ID3-fake-thinking-mp3'
DEFAULT_VOICE = 'defaultVoice1'
STORE_VOICE = 'storeVoice1'
POLICY_TEXTS = chatTools.THINKING_TEXTS['getMyStorePolicy']
api = ApiRequests()


@pytest.fixture
def clean():
    wipeCollections(COLLECTIONS)
    yield
    wipeCollections(COLLECTIONS)


@pytest.fixture
def voice(monkeypatch):
    """ElevenLabs answers MP3 for every text, each call recorded as
    (text, voiceId)."""
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-eleven-key')
    monkeypatch.setenv('ELEVENLABS_DEFAULT_VOICE_ID', DEFAULT_VOICE)
    monkeypatch.delenv('VOICE_DAILY_CHARS_ALL', raising=False)
    monkeypatch.setattr(VoiceActions, '_now', lambda: FAR)
    spoken = []

    def speak(text, voiceId, deadline):
        spoken.append((text, voiceId))
        return MP3

    monkeypatch.setattr(voiceModel, 'speak', speak)
    return spoken


def thinkingDoc(userId: str = ADMIN, turnKey: str = TURN_KEY) -> dict | None:
    return db.read({'_id': f'{userId}:{turnKey}'}, 'ChatThinking',
                   findOne=True)


def seedThinking(storeId: str, text: str, userId: str = ADMIN) -> None:
    chatThinking.note(userId, storeId, TURN_KEY, 'getMyStorePolicy')
    db.findOneAndUpdate({'_id': f'{userId}:{TURN_KEY}'},
                        {'$set': {'thinkingText': text}}, 'ChatThinking')


def clips() -> list[dict]:
    return db.read({}, 'ThinkingSpeech')


def ttsCount(kind: str, owner: str) -> int | None:
    doc = db.read({'_id': VoiceActions.usageKey(kind, owner, DAY)},
                  'VoiceUsage', findOne=True)
    return None if doc is None else doc['count']


# ----------------------------- the decorator ----------------------------- #


def test_mcpTool_needsThinkingText():
    with pytest.raises(TypeError, match='thinkingText'):
        mcp_tool('admin')


@pytest.mark.parametrize('thinkingText, problem', [
    ('Reading your business', 'must be a list of strings'),
    (['Reading', 'Accessing'], f'at least {MIN_THINKING_TEXTS} texts'),
    (['Reading', 'Accessing', ''], 'only non-empty strings'),
    (['Reading', 'Accessing', 3], 'only non-empty strings'),
    (['Reading', 'Accessing', ' Getting'], 'must be trimmed'),
    (['Reading', 'Accessing', 'x' * 61], 'at most 60 characters'),
    (['Reading', 'Accessing', 'Reading'], 'lists a text twice'),
])
def test_mcpTool_refusesBadThinkingText(thinkingText, problem):
    with pytest.raises(ValueError, match=problem):
        mcp_tool('admin', thinkingText=thinkingText)


def test_everyTool_hasItsThinkingTexts_inChatTools():
    specs = toolSpecs(ApiRequests)

    assert all(len(spec.thinkingText) >= MIN_THINKING_TEXTS
               for spec in specs)
    assert chatTools.THINKING_TEXTS == {spec.name: spec.thinkingText
                                        for spec in specs}


# ----------------------------- a turn's thinking ----------------------------- #


def test_aTurn_savesOneOfTheRunningToolsTexts_thenClearsIt(clean, model):
    store = createTestStore(ADMIN)
    fake = model(calls(('call-1', 'getMyStorePolicy', '{}')),
                 reply('No returns policy yet.'))
    seen = []
    fake.onCall = lambda i: seen.append(
        (thinkingDoc(), api.getChatThinking(ADMIN, TURN_KEY)))

    res = api.sendAdminChatMessage(ADMIN, 'What is my policy?', None, None,
                                   None, TURN_KEY)

    assert res['reply'] == 'No returns policy yet.'
    # Before the first call no tool ran; before the second, the policy tool.
    [(before, beforeRes), (during, duringRes)] = seen
    assert before is None and beforeRes == {'thinkingText': None}
    assert during['thinkingText'] in POLICY_TEXTS
    assert during['userId'] == ADMIN and during['storeId'] == store['_id']
    assertExactShape(ChatThinkingResponse, duringRes)
    assert duringRes == {'thinkingText': during['thinkingText']}
    assert thinkingDoc() is None
    assert api.getChatThinking(ADMIN, TURN_KEY) == {'thinkingText': None}


def test_aTurnThatFails_clearsItsThinkingToo(clean, model):
    createTestStore(ADMIN)
    model(calls(('call-1', 'getMyStorePolicy', '{}')),
          RuntimeError('the model broke'))

    with pytest.raises(Exception):
        api.sendAdminChatMessage(ADMIN, 'What is my policy?', None, None,
                                 None, TURN_KEY)

    assert thinkingDoc() is None


def test_aTurnWithoutTurnKey_savesNothing(clean, model):
    createTestStore(ADMIN)
    model(calls(('call-1', 'getMyStorePolicy', '{}')),
          reply('No returns policy yet.'))

    api.sendAdminChatMessage(ADMIN, 'What is my policy?')

    assert db.read({}, 'ChatThinking') == []


def test_aToolWithoutTexts_savesNothing(clean):
    chatThinking.note(ADMIN, 'store-1', TURN_KEY, 'showStoreLocationForm')
    chatThinking.note(ADMIN, 'store-1', TURN_KEY, 'madeUpTool')

    assert thinkingDoc() is None


def test_aSaveThatFails_neverFailsTheTurn(clean, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('the database is down')

    monkeypatch.setattr(chatThinking.db, 'findOneAndUpdate', broken)

    chatThinking.note(ADMIN, 'store-1', TURN_KEY, 'getMyStorePolicy')

    assert 'chatThinkingNotSaved' in [r.msg['event'] for r in caplog.records
                                      if isinstance(r.msg, dict)]


def test_anotherUser_neverReadsTheTurn(clean):
    store = createTestStore(ADMIN)
    seedThinking(store['_id'], POLICY_TEXTS[0])

    assert api.getChatThinking(OTHER_ADMIN, TURN_KEY) == {
        'thinkingText': None}
    assert api.getChatThinking(ADMIN, TURN_KEY) == {
        'thinkingText': POLICY_TEXTS[0]}


@pytest.mark.parametrize('turnKey', [
    'short', 'x' * 65, 'has spaces in it ok', 'semi;colon-0123456789', 7])
def test_aBadTurnKey_isRefused_beforeTheModel(clean, turnKey):
    createTestStore(ADMIN)

    with pytest.raises(ValueError, match=chatThinking.TURN_KEY_INVALID):
        api.getChatThinking(ADMIN, turnKey)
    # offline: reaching the model would raise an AssertionError instead.
    with pytest.raises(ValueError, match=chatThinking.TURN_KEY_INVALID):
        api.sendAdminChatMessage(ADMIN, 'hello', None, None, None, turnKey)


# ----------------------------- spoken thinking ----------------------------- #


def test_speakChatThinking_speaksATextOnce_thenReusesItsClip(clean, voice):
    store = createTestStore(ADMIN)
    text = POLICY_TEXTS[0]
    seedThinking(store['_id'], text)

    first = api.speakChatThinking(ADMIN, TURN_KEY)
    second = api.speakChatThinking(ADMIN, TURN_KEY)

    assertExactShape(SpokenThinkingResponse, first)
    assert first == second == {
        'audio': base64.b64encode(MP3).decode('ascii')}
    assert voice == [(text, DEFAULT_VOICE)]
    [clip] = clips()
    assert clip['_id'] == chatThinking.speechKey(DEFAULT_VOICE, text)
    assert (clip['voiceId'], clip['text']) == (DEFAULT_VOICE, text)
    # Only the first time counts, against the store's and all stores' caps.
    assert ttsCount('ttsStore', store['_id']) == len(text)
    assert ttsCount('ttsCustomer', ADMIN) is None


def test_speakChatThinking_usesTheStoresVoice(clean, voice):
    store = createTestStore(ADMIN)
    db.findOneAndUpdate({'_id': store['_id']},
                        {'$set': {'voiceId': STORE_VOICE}}, 'Stores')
    seedThinking(store['_id'], POLICY_TEXTS[1])

    api.speakChatThinking(ADMIN, TURN_KEY)

    assert voice == [(POLICY_TEXTS[1], STORE_VOICE)]


def test_speakChatThinking_withNothingToSay_speaksNothing(clean, voice):
    createTestStore(ADMIN)

    assert api.speakChatThinking(ADMIN, TURN_KEY) == {'audio': None}
    assert voice == []


def test_speakChatThinking_failing_givesTheCharactersBack(
        clean, voice, monkeypatch):
    store = createTestStore(ADMIN)
    seedThinking(store['_id'], POLICY_TEXTS[0])

    def unavailable(text, voiceId, deadline):
        raise voiceModel.unavailable()

    monkeypatch.setattr(voiceModel, 'speak', unavailable)

    res = api.speakChatThinking(ADMIN, TURN_KEY)

    assert res == {'audio': None}
    assert clips() == []
    assert ttsCount('ttsStore', store['_id']) in (None, 0)


def test_speakChatThinking_pastTheCap_isNull(clean, voice, monkeypatch):
    store = createTestStore(ADMIN)
    seedThinking(store['_id'], POLICY_TEXTS[0])
    monkeypatch.setenv('VOICE_DAILY_CHARS_ALL', '0')

    assert api.speakChatThinking(ADMIN, TURN_KEY)['audio'] is None
    assert voice == []


# ----------------------------- keeping clips in step ----------------------------- #


def test_sync_removesClipsOfRemovedTexts_andSpeaksTheNewOnes(clean, voice):
    store = createTestStore(ADMIN)
    db.findOneAndUpdate({'_id': store['_id']},
                        {'$set': {'voiceId': STORE_VOICE}}, 'Stores')
    kept = POLICY_TEXTS[0]
    chatThinking.speakAndSave(DEFAULT_VOICE, kept, float('inf'))
    chatThinking.speakAndSave(DEFAULT_VOICE, 'A text no tool has now',
                              float('inf'))
    voice.clear()

    counts = chatThinking.syncThinkingSpeech()

    texts = chatThinking.allTexts()
    assert counts == {'removed': 1, 'spoken': 2 * len(texts) - 1,
                      'failed': 0}
    assert (DEFAULT_VOICE, kept) not in voice
    assert {(clip['voiceId'], clip['text']) for clip in clips()} == {
        (v, text) for v in (DEFAULT_VOICE, STORE_VOICE) for text in texts}
    # A second run finds nothing to do.
    voice.clear()
    assert chatThinking.syncThinkingSpeech() == {
        'removed': 0, 'spoken': 0, 'failed': 0}
    assert voice == []


def test_sync_withoutAKey_onlyRemoves(clean, voice, monkeypatch):
    chatThinking.speakAndSave(DEFAULT_VOICE, 'A text no tool has now',
                              float('inf'))
    voice.clear()
    monkeypatch.delenv('ELEVENLABS_API_KEY')

    assert chatThinking.syncThinkingSpeech() == {
        'removed': 1, 'spoken': 0, 'failed': 0}
    assert voice == [] and clips() == []


def test_sync_neverRaises(clean, voice, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('ElevenLabs is down')

    monkeypatch.setattr(voiceModel, 'speak', broken)
    monkeypatch.setattr(chatThinking, 'SYNC_MAX_CLIPS', 2)

    assert chatThinking.syncThinkingSpeech() == {
        'removed': 0, 'spoken': 0, 'failed': 2}
    assert clips() == []


def test_theClipKey_isTheVoiceAndTheText():
    assert chatThinking.speechKey('a', 'b') != chatThinking.speechKey('b',
                                                                      'a')
    assert chatThinking.speechKey('a', 'b') == chatThinking.speechKey('a',
                                                                      'b')


def test_aThinkingDocument_expiresAfterTheTurnsLock():
    from actions.ChatActions import CONVERSATION_LOCK_SECONDS
    assert chatThinking.THINKING_SECONDS > CONVERSATION_LOCK_SECONDS

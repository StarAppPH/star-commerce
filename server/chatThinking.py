"""chatThinking.py — what the assistant is doing while a chat turn runs.

Every @mcp_tool route has thinking texts (mcpTools: at least
MIN_THINKING_TEXTS, in chatTools.THINKING_TEXTS). A chat turn runs in one
blocking request, so the client gives it a turnKey (a random id of its
own) and asks while it waits:

  - note(): just before the turn runs a route tool (ChatActions._runModel),
    one of the tool's texts, picked at random, is saved on the ChatThinking
    document "<userId>:<turnKey>" (a form tool saves none). clear() deletes
    it when the turn ends; a TTL index on expireAt deletes one left behind;
  - getChatThinking (ChatActions) reads the caller's own document, so a
    caller only ever sees their own turns;
  - speakChatThinking (VoiceActions) speaks it in the store's voice. Each
    (voice, text) is spoken by ElevenLabs once and kept as a ThinkingSpeech
    clip (cachedSpeech, speakAndSave), reused by every turn after;
  - syncThinkingSpeech() (job_predeploy.py, at every deploy) keeps the clips
    in step with the texts: it deletes those of texts no tool has any more
    (an edited text is a removed one and a new one) and speaks the new ones
    in the default voice and every store's.

A text is the app's own UI string, never the user's: it is no transcript
and no reply. Saving or reading it never fails the turn (logged as a
warning).
"""

import base64
import datetime
import hashlib
import logging
import os
import random
import re
import time

import voiceModel
from mongoDb import db
from objects import TURN_KEY_PATTERN, ChatThinking, ThinkingSpeech

THINKING = 'ChatThinking'
THINKING_SPEECH = 'ThinkingSpeech'
# A document outlives no turn: longer than a conversation's lock
# (ChatActions.CONVERSATION_LOCK_SECONDS).
THINKING_SECONDS = 120
# The most clips one syncThinkingSpeech run speaks, and its time: the rest
# are spoken on first use.
SYNC_MAX_CLIPS = 500
SYNC_SECONDS = 240
TURN_KEY_INVALID = 'turnKey must be 16 to 64 letters, digits, - or _'
UTC = datetime.timezone.utc
_TURN_KEY = re.compile(TURN_KEY_PATTERN)


def _now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def thinkingTexts() -> dict[str, tuple[str, ...]]:
    """Each route tool's thinking texts, by tool name."""
    from chatTools import THINKING_TEXTS
    return THINKING_TEXTS


def allTexts() -> set[str]:
    return {text for texts in thinkingTexts().values() for text in texts}


def validTurnKey(turnKey) -> str | None:
    """`turnKey` checked: None (or "") is a turn nobody asks about."""
    if turnKey is None or turnKey == '':
        return None
    if not isinstance(turnKey, str) or not _TURN_KEY.match(turnKey):
        raise ValueError(TURN_KEY_INVALID)
    return turnKey


def _key(userId: str, turnKey: str) -> str:
    return f'{userId}:{turnKey}'


def note(userId: str, storeId: str, turnKey: str | None,
         toolName) -> None:
    """Save one of `toolName`'s thinking texts, at random, as what the turn
    `turnKey` of `userId` is doing. Nothing for no turnKey or a tool with
    none (a form tool, a name the model made up)."""
    texts = thinkingTexts().get(toolName) if isinstance(toolName,
                                                        str) else None
    if turnKey is None or not texts:
        return
    key = _key(userId, turnKey)
    thinking = ChatThinking(_id=key,
                            userId=userId,
                            storeId=storeId,
                            thinkingText=random.choice(texts),
                            expireAt=_now() + datetime.timedelta(
                                seconds=THINKING_SECONDS)).model_dump(
                                    by_alias=True, exclude={'id', 'version'})
    try:
        db.findOneAndUpdate({'_id': key}, {'$set': thinking}, THINKING,
                            upsert=True, projection={'_id': 1})
    except Exception as error:
        logging.warning({'event': 'chatThinkingNotSaved',
                         'errorClass': type(error).__name__})


def clear(userId: str, turnKey: str | None) -> None:
    """Delete what the turn `turnKey` was doing: it is over."""
    if turnKey is None:
        return
    try:
        db.delete({'_id': _key(userId, turnKey)}, THINKING)
    except Exception as error:
        logging.warning({'event': 'chatThinkingNotCleared',
                         'errorClass': type(error).__name__})


def current(userId: str, turnKey) -> dict | None:
    """What the caller's turn `turnKey` is doing ({storeId, thinkingText}),
    or None: before its first tool, once it is over, or someone else's."""
    turnKey = validTurnKey(turnKey)
    if turnKey is None:
        return None
    thinking = db.read({'_id': _key(userId, turnKey), 'userId': userId},
                       THINKING,
                       projection={'storeId': 1, 'thinkingText': 1},
                       findOne=True)
    if thinking is None or thinking['thinkingText'] not in allTexts():
        return None
    return thinking


def speakingVoice(voiceId: str | None) -> str:
    """The voice a clip is spoken in: the store's, else the default
    (voiceModel.defaultVoiceId: unavailable() when unset)."""
    return (voiceId if voiceModel.validVoiceId(voiceId) else
            voiceModel.defaultVoiceId())


def speechKey(voiceId: str, text: str) -> str:
    return hashlib.sha256(f'{voiceId}\n{text}'.encode()).hexdigest()


def cachedSpeech(voiceId: str, text: str) -> str | None:
    """The clip of `text` in `voiceId` (base64 MP3), or None."""
    clip = db.read({'_id': speechKey(voiceId, text)}, THINKING_SPEECH,
                   projection={'audio': 1}, findOne=True)
    return None if clip is None else clip['audio']


def speakAndSave(voiceId: str, text: str, deadline: float) -> str:
    """`text` spoken in `voiceId` by ElevenLabs, kept as its clip (base64
    MP3). Two racing first uses both speak it; the last save wins."""
    audio = base64.b64encode(voiceModel.speak(text, voiceId,
                                              deadline)).decode('ascii')
    clip = ThinkingSpeech(_id=speechKey(voiceId, text),
                          voiceId=voiceId,
                          text=text,
                          audio=audio,
                          createdAt=_now()).model_dump(
                              by_alias=True, exclude={'id', 'version'})
    db.findOneAndUpdate({'_id': speechKey(voiceId, text)}, {'$set': clip},
                        THINKING_SPEECH, upsert=True,
                        projection={'_id': 1})
    return audio


def _storeVoices() -> set[str]:
    stores = db.read({'voiceId': {'$type': 'string'}}, 'Stores',
                     projection={'voiceId': 1})
    return {store['voiceId'] for store in stores
            if voiceModel.validVoiceId(store['voiceId'])}


def syncThinkingSpeech() -> dict:
    """Keep the clips in step with the thinking texts: delete those of a
    text no tool has any more, then speak each missing (voice, text) of the
    default voice and every store's, up to SYNC_MAX_CLIPS within
    SYNC_SECONDS (the rest are spoken on first use). Never raises: a
    failure is logged and the rest are spoken on first use. Returns the
    counts."""
    counts = {'removed': 0, 'spoken': 0, 'failed': 0}
    try:
        texts = sorted(allTexts())
        removed = db.delete({'text': {'$nin': texts}}, THINKING_SPEECH)
        counts['removed'] = removed if isinstance(removed, int) else 0
        if not os.getenv(voiceModel.ELEVENLABS_KEY_VARIABLE):
            return counts
        voices = _storeVoices()
        if voiceModel.validVoiceId(
                (os.getenv(voiceModel.DEFAULT_VOICE_VARIABLE) or '').strip()):
            voices.add(voiceModel.defaultVoiceId())
        ends = time.monotonic() + SYNC_SECONDS
        for voice in sorted(voices):
            for text in texts:
                if (counts['spoken'] + counts['failed'] >= SYNC_MAX_CLIPS
                        or time.monotonic() >= ends):
                    return counts
                if cachedSpeech(voice, text) is not None:
                    continue
                try:
                    speakAndSave(voice, text, min(
                        ends, time.monotonic() +
                        voiceModel.TTS_DEADLINE_SECONDS))
                    counts['spoken'] += 1
                except Exception as error:
                    counts['failed'] += 1
                    logging.warning({'event': 'thinkingSpeechNotSpoken',
                                     'errorClass': type(error).__name__})
    except Exception as error:
        logging.warning({'event': 'thinkingSpeechSyncFailed',
                         'errorClass': type(error).__name__})
    return counts

"""Voice chat: a recorded message in (Meta's speech-to-text), the reply
spoken back (ElevenLabs), and the store voices an admin picks from
(VoiceActions, voiceModel.py).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field
from typing import Literal

from objects.base import MongoModel, ResponseModel
from objects.chat import CHAT_MESSAGE_MAX_LENGTH
from objects.store import VOICE_ID_PATTERN


# ──────────────────────────────────────────────
# Voice chat
# ──────────────────────────────────────────────

# What a spoken reply is encoded as (base64 in the response).
VoiceAudioFormat = Literal['mp3']
# A voice of listVoices as it is sent: ElevenLabs' text cut to these (it is
# third-party data, and a page of voices must fit in one chat tool result).
VOICE_NAME_MAX_LENGTH = 60
VOICE_DESCRIPTION_MAX_LENGTH = 100
VOICE_LABELS_MAX = 6
VOICE_LABEL_KEY_MAX_LENGTH = 20
VOICE_LABEL_VALUE_MAX_LENGTH = 30
VOICE_PREVIEW_URL_MAX_LENGTH = 300
VOICE_SEARCH_MAX_LENGTH = 100
# ElevenLabs does not document its page tokens' length: ours wraps one.
VOICE_PAGE_TOKEN_MAX_LENGTH = 512


class VoiceUsage(MongoModel):
    """A VoiceUsage document: the seconds of speech transcribed, or the
    characters spoken, of a customer at a store, a store, or all stores on
    one day (UTC+8). Its _id is "<kind>:<owner>:<YYYY-MM-DD>"
    (usageCounters.usageKey); a TTL index on expireAt deletes it once the
    day is over."""
    count: int = Field(..., ge=0,
                       description='Seconds or characters used that day')
    expireAt: datetime.datetime = Field(...,
                                        description='When it is deleted')


class VoiceReplyResponse(ResponseModel):
    """What a voice chat route sends: what it heard, the assistant's reply
    (as ChatReplyResponse), and the reply spoken, null when speaking it
    failed (the reply is saved all the same)."""
    conversationId: str = Field(..., description='The Conversations _id')
    turnSeq: int = Field(..., ge=0,
                         description='The seq of the turn just saved')
    transcript: str = Field(..., min_length=1,
                            max_length=CHAT_MESSAGE_MAX_LENGTH,
                            description='What the recording said, sent to '
                            'the assistant as the message')
    reply: str = Field(..., description="The assistant's reply")
    audio: str | None = Field(
        ..., description='The reply spoken (base64); null when it could not '
        'be spoken (speakChatTurn tries again)')
    audioFormat: VoiceAudioFormat = Field(..., description="audio's encoding")


class SpokenReplyResponse(ResponseModel):
    """What speakChatTurn sends: a saved reply, spoken."""
    audio: str = Field(..., min_length=1,
                       description='The reply spoken (base64)')
    audioFormat: VoiceAudioFormat = Field(..., description="audio's encoding")


class VoiceRow(ResponseModel):
    """One voice a store can speak with: an ElevenLabs default voice or one
    saved to the app's account, never a person's clone. Its text is
    ElevenLabs' (third-party data), cut short."""
    voiceId: str = Field(..., pattern=VOICE_ID_PATTERN,
                         description="ElevenLabs' voice id")
    name: str = Field(..., min_length=1, max_length=VOICE_NAME_MAX_LENGTH,
                      description="The voice's name")
    description: str | None = Field(
        ..., max_length=VOICE_DESCRIPTION_MAX_LENGTH,
        description='How it sounds, as ElevenLabs describes it')
    labels: dict[str, str] = Field(
        ..., max_length=VOICE_LABELS_MAX,
        description='Its labels (accent, age, gender, use case...)')
    previewUrl: str | None = Field(
        ..., max_length=VOICE_PREVIEW_URL_MAX_LENGTH,
        description='A short sample to listen to (https)')


class VoiceListResponse(ResponseModel):
    voices: list[VoiceRow] = Field(..., description='A page of voices')
    nextPageToken: str | None = Field(
        ..., max_length=VOICE_PAGE_TOKEN_MAX_LENGTH,
        description="The next page's pageToken, sent back verbatim; null "
        'on the last page')

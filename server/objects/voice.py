"""The store's voice: replies and thinking texts spoken (ElevenLabs), and
the store voices an admin picks from (VoiceActions, voiceModel.py).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field

from objects.base import MongoModel, ResponseModel
from objects.store import VOICE_ID_PATTERN


# ──────────────────────────────────────────────
# Voice chat
# ──────────────────────────────────────────────

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
    """A VoiceUsage document: the characters spoken of a customer at a
    store, a store, or all stores on one day (UTC+8). Its _id is
    "<kind>:<owner>:<YYYY-MM-DD>" (usageCounters.usageKey); a TTL index on
    expireAt deletes it once the day is over. (The speech tokens' per-minute
    counters share the collection, keyed by the minute.)"""
    count: int = Field(..., ge=0,
                       description='Characters used that day')
    expireAt: datetime.datetime = Field(...,
                                        description='When it is deleted')


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

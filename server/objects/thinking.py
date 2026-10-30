"""What the assistant is doing while a chat turn runs: the thinking text of
the tool it is running (mcpTools' thinkingText), and that text spoken in a
store's voice (chatThinking.py).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field

from mcpTools import THINKING_TEXT_MAX_LENGTH
from objects.base import MongoModel, ResponseModel

# The key a client gives a chat turn, to ask what it is doing while it runs
# (a crypto.randomUUID() fits).
TURN_KEY_PATTERN = r'^[A-Za-z0-9_-]{16,64}$'


class ChatThinking(MongoModel):
    """A ChatThinking document: the thinking text of the tool a turn is
    running. Its _id is "<userId>:<turnKey>", so a caller only ever reads
    their own; deleted when the turn ends, and by a TTL index on expireAt
    otherwise."""
    userId: str = Field(..., description='The Users _id of the caller')
    storeId: str = Field(..., description='The Stores _id the turn is at')
    thinkingText: str = Field(..., min_length=1,
                              max_length=THINKING_TEXT_MAX_LENGTH,
                              description='What the assistant is doing')
    expireAt: datetime.datetime = Field(...,
                                        description='When it is deleted')


class ThinkingSpeech(MongoModel):
    """A ThinkingSpeech document: one thinking text spoken once in one
    ElevenLabs voice, reused for every turn after. Its _id is
    chatThinking.speechKey(voiceId, text). syncThinkingSpeech deletes the
    clips of texts no tool has any more."""
    voiceId: str = Field(..., description="ElevenLabs' voice id")
    text: str = Field(..., min_length=1,
                      max_length=THINKING_TEXT_MAX_LENGTH,
                      description='The thinking text spoken')
    audio: str = Field(..., min_length=1,
                       description='The text spoken (base64 MP3)')
    createdAt: datetime.datetime = Field(...,
                                         description='When it was spoken')


class ChatThinkingResponse(ResponseModel):
    """What getChatThinking sends: what the turn is doing right now."""
    thinkingText: str | None = Field(
        ..., max_length=THINKING_TEXT_MAX_LENGTH,
        description='The running tool\'s thinking text; null before the '
        'first tool, and once the turn is over')


class SpokenThinkingResponse(ResponseModel):
    """What speakChatThinking sends: the turn's thinking text, spoken."""
    audio: str | None = Field(
        ..., description='The thinking text spoken (base64 MP3); null when '
        'the turn is doing nothing to say or it could not be spoken')

"""What the assistant is doing while a chat turn runs: the thinking text of
the tool it is running (mcpTools' thinkingText), and that text spoken in a
store's voice (chatThinking.py).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime
from typing import Literal

from pydantic import Field

from mcpTools import THINKING_TEXT_MAX_LENGTH
from objects.base import MongoModel, ResponseModel

# The key a client gives a chat turn, to ask what it is doing while it runs
# (a crypto.randomUUID() fits).
TURN_KEY_PATTERN = r'^[A-Za-z0-9_-]{16,64}$'
# Where a turn is (chatTurnKeys.py): a ChatTurnKey document's status; and
# as getChatTurnStatus tells it, 'failed' for one running past its time
# (its process stopped) and 'unknown' for a turnKey never claimed, released
# (the turn ended unsaved) or expired.
TurnKeyStatus = Literal['running', 'saved']
TurnStatus = TurnKeyStatus | Literal['failed', 'unknown']


def turnKeyId(userId: str, turnKey: str) -> str:
    """The _id of a caller's turn's documents (ChatThinking, ChatTurnKey):
    "<userId>:<turnKey>", so a caller only ever reads their own."""
    return f'{userId}:{turnKey}'


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


class ChatTurnKey(MongoModel):
    """A ChatTurnKeys document: the claim of one chat turn's turnKey, so a
    message sent again with it (a phone that lost the reply and retried)
    runs no second turn (chatTurnKeys.py). Its _id is turnKeyId(userId,
    turnKey), so a caller only ever reads their own; deleted when the turn
    ends unsaved, and by a TTL index on expireAt."""
    userId: str = Field(..., description='The Users or Customers _id')
    claimId: str = Field(..., min_length=1,
                         description='The claiming turn\'s own random id: '
                         'only it marks the claim saved or releases it')
    status: TurnKeyStatus = Field(
        ..., description='running until the turn is saved (saved); a turn '
        'that ends unsaved deletes it')
    conversationId: str | None = Field(
        None, description='The conversation it was saved in; null before')
    runningUntil: datetime.datetime = Field(
        ..., description='Past it, a turn still running is failed (its '
        'process stopped): as long as a conversation lock')
    expireAt: datetime.datetime = Field(...,
                                        description='When it is deleted')


class ChatTurnStatusResponse(ResponseModel):
    """What getChatTurnStatus sends: where the caller's turn is."""
    status: TurnStatus = Field(
        ..., description="running; saved (its reply is in the conversation); "
        "failed (it ran past its time: send it again); unknown (never seen, "
        "ended without saving, or too long ago: send it again)")
    conversationId: str | None = Field(
        ..., description='The conversation it was saved in, once saved')

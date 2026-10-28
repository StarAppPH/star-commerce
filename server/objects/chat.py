"""Chat: the conversations of the admin and customer assistants.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field
from typing import Literal

from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)


# ──────────────────────────────────────────────
# Chat (conversations with the assistant)
# ──────────────────────────────────────────────

# Who the assistant talks to: a store's admin, or a customer at a store.
ChatAudience = Literal['admin', 'customer']
# The longest message a user can send.
CHAT_MESSAGE_MAX_LENGTH = 2000
# A conversation's title: its first message, cut to this length.
CHAT_TITLE_MAX_LENGTH = 80
# A model's tool call is stored only within these: a longer id, name or
# arguments is refused as a tool error and stored bounded
# (chatModel.toolCallProblem).
CHAT_TOOL_CALL_ID_MAX_LENGTH = 128
CHAT_TOOL_NAME_MAX_LENGTH = 64
CHAT_TOOL_ARGUMENTS_MAX_LENGTH = 16000


class Conversation(MongoModel):
    """A Conversations document: one chat of one user at one store. Its
    turns are ChatTurns documents, one per user message, so a long
    conversation never nears MongoDB's document size limit. Kept forever.

    An admin's conversation is at the store the admin owns; a customer's at
    the store being visited. busyUntil is the lock a turn holds while the
    assistant answers, so two messages of one conversation never run at
    once; null (or past) when none is running."""
    userId: str = Field(..., min_length=1,
                        description='The Users or Customers _id it belongs to')
    audience: ChatAudience = Field(..., description='Who the assistant talks to')
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    title: str | None = Field(None, max_length=CHAT_TITLE_MAX_LENGTH,
                              description='The first message, cut short')
    turnCount: int = Field(..., ge=0, description='How many turns it holds')
    createdAt: datetime.datetime = Field(..., description='When it started')
    lastMessageAt: datetime.datetime = Field(
        ..., description='When its last turn was saved')
    busyUntil: datetime.datetime | None = Field(
        None, description='A turn is running until then')


class ConversationView(StoredDocumentKeys, Conversation):
    """A Conversations document as stored."""


class ChatToolCall(AppModel):
    """One tool call of the model, as Meta sent it."""
    id: str = Field(..., max_length=CHAT_TOOL_CALL_ID_MAX_LENGTH,
                    description="Meta's id of the call")
    name: str = Field(..., max_length=CHAT_TOOL_NAME_MAX_LENGTH,
                      description='The tool called')
    arguments: str = Field(..., max_length=CHAT_TOOL_ARGUMENTS_MAX_LENGTH,
                           description='Its arguments, a JSON string')


class ChatStep(AppModel):
    """One message of a turn between the user's text and the reply, exactly
    as sent to Meta: an assistant message asking for tool calls, or one
    tool's result (toolCallId names the call it answers)."""
    role: Literal['assistant', 'tool'] = Field(...,
                                               description='Who sent it')
    content: str | None = Field(None, description='Its text')
    toolCalls: list[ChatToolCall] | None = Field(
        None, description="An assistant message's tool calls")
    toolCallId: str | None = Field(None,
                                   description='The call a tool result answers')


class ChatTokenUsage(AppModel):
    """The tokens a turn's model calls used, summed over its calls."""
    promptTokens: int = Field(..., ge=0, description='Input tokens')
    completionTokens: int = Field(..., ge=0, description='Output tokens')
    cachedTokens: int = Field(..., ge=0,
                              description='Input tokens read from the cache')


class ChatTurn(MongoModel):
    """A ChatTurns document: one user message and the reply to it, with the
    tool steps in between. Replayed whole as the history of the next turns,
    so every tool_calls message is followed by its tool messages, as Meta
    requires. Saved only once the turn succeeded. (conversationId, seq) is
    unique (mongoDb.ensureIndexes)."""
    conversationId: str = Field(..., min_length=1,
                                description='The Conversations _id')
    userId: str = Field(..., min_length=1,
                        description='The Users or Customers _id it belongs to')
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    seq: int = Field(..., ge=0, description='Its place in the conversation')
    userText: str = Field(..., min_length=1,
                          max_length=CHAT_MESSAGE_MAX_LENGTH,
                          description='What the user sent')
    steps: list[ChatStep] = Field(..., description='The tool steps, in order')
    replyText: str = Field(..., description="The assistant's reply")
    toolCallCount: int = Field(..., ge=0, description='Tool calls it ran')
    usage: ChatTokenUsage = Field(..., description='The tokens it used')
    model: str = Field(..., description='The model that answered')
    createdAt: datetime.datetime = Field(..., description='When it was saved')


class ChatUsage(MongoModel):
    """A ChatUsage document: how many chat messages a customer, or a store's
    customers, sent on one day (UTC+8). Its _id is
    "<customer|store>:<id>:<YYYY-MM-DD>"; a TTL index on expireAt deletes
    it once the day is over."""
    count: int = Field(..., ge=0, description='Messages sent that day')
    expireAt: datetime.datetime = Field(...,
                                        description='When it is deleted')


class ChatReplyResponse(ResponseModel):
    """What a chat route sends: the assistant's reply to the message."""
    conversationId: str = Field(..., description='The Conversations _id')
    reply: str = Field(..., description="The assistant's reply")
    turnSeq: int = Field(..., ge=0,
                         description='The seq of the turn just saved')


class ConversationRow(ResponseModel):
    """One conversation of listMyConversations. Each field restates
    Conversation's."""
    id: str = Field(..., alias='_id', description='Conversations _id')
    title: str | None = Field(..., max_length=CHAT_TITLE_MAX_LENGTH,
                              description='The first message, cut short')
    lastMessageAt: datetime.datetime = Field(
        ..., description='When its last turn was saved')
    turnCount: int = Field(..., ge=0, description='How many turns it holds')


class ConversationListResponse(ResponseModel):
    conversations: list[ConversationRow] = Field(
        ..., description='The conversations, newest first')
    nextBefore: str | None = Field(
        ..., description="The next page's `before`, sent back verbatim: "
        "the last row's lastMessageAt as ISO 8601 UTC with milliseconds; "
        'null when this page is the last')


class ChatTurnRow(ResponseModel):
    """One turn of getConversationTurns: the user's text and the reply,
    never the tool steps. Each field restates ChatTurn's."""
    seq: int = Field(..., ge=0, description='Its place in the conversation')
    userText: str = Field(..., min_length=1,
                          max_length=CHAT_MESSAGE_MAX_LENGTH,
                          description='What the user sent')
    replyText: str = Field(..., description="The assistant's reply")
    createdAt: datetime.datetime = Field(..., description='When it was saved')


class ConversationTurnsResponse(ResponseModel):
    turns: list[ChatTurnRow] = Field(..., description='The turns, newest first')

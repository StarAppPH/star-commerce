"""ChatActions: the chat with the store's assistant (Meta's Muse model).

A message goes to the model with its audience's tools (chatTools.py,
generated from the @mcp_tool routes), and the model's tool calls run here,
in-process, as the signed-in user at the conversation's store
(chatModel.runTool). The loop ends at the model's text reply, and the turn
is saved: a Conversations document per chat, a ChatTurns document per
message (objects/chat.py). Conversations are kept forever.

Who and where come only from the JWT's userId and the conversation's
storeId: an admin chats at the store they own (requireOwnerStore), a
customer at the store being visited (customerAtStore, setUpStore). The
model never chooses either.

A customer's messages are capped per day (UTC+8), per customer and per
store (ChatUsage counters, deleted by a TTL index); an admin is not capped.
At most CHAT_TURNS_AT_ONCE turns run at once per process: a message past
that is a 503 before any lock or quota is taken.

A failed turn (the model unavailable, the deadline passed) saves nothing,
and its conversation's lock is released; its quota is given back only when
the model never answered (so no tool ran). Tool calls it already ran stay
done (an order placed, say): the user can ask again, and the order routes
guard their own state.

The clock: every read of now is _now(), which reads this module's
`datetime`, so a test freezes it by replacing ChatActions.datetime.
"""

import datetime
import json
import logging
import threading
import time

import chatModel
from actions.OrderActions import customerAtStore
from actions.ProductActions import _validLimit
from actions.callSlots import slot
from actions.StoreActions import _validText, requireOwnerStore, setUpStore
# QUOTA_ATTEMPTS is imported for the tests, which read it here.
from actions.usageCounters import (QUOTA_ATTEMPTS, appDay, giveBackQuota,
                                   takeQuota, usageExpiry, usageKey)
from mongoDb import db
from objects import (CHAT_MESSAGE_MAX_LENGTH, CHAT_TITLE_MAX_LENGTH,
                     CHAT_TOOL_CALL_ID_MAX_LENGTH,
                     ChatReplyResponse, ChatStep, ChatTokenUsage,
                     ChatToolCall, ChatTurn, ChatTurnRow, Conversation,
                     ConversationListResponse, ConversationRow,
                     ConversationTurnsResponse)
from route_config import route_config
from services.__request import ServiceUnavailable
from utils import generateRandomString, parseIsoInstant

CONVERSATIONS = 'Conversations'
CHAT_TURNS = 'ChatTurns'
CHAT_USAGE = 'ChatUsage'
# Read at import, so it stays the real timezone when a test replaces this
# module's `datetime`.
UTC = datetime.timezone.utc
# The most messages a day (UTC+8) one customer sends, and one store's
# customers send together. An admin is not capped.
CUSTOMER_DAILY_CHAT_CAP = 50
STORE_DAILY_CHAT_CAP = 2000
# The turns one process runs at once: each holds one of gunicorn's 8
# threads for up to TURN_DEADLINE_SECONDS, so the others stay free for the
# rest of the app.
CHAT_TURNS_AT_ONCE = 4
_turnSlots = threading.BoundedSemaphore(CHAT_TURNS_AT_ONCE)
# The most tool calls the model runs for one message: past it, the rest are
# answered with TOOL_LIMIT_REACHED and the model answers without tools.
MAX_TOOL_CALLS_PER_MESSAGE = 8
# The turns replayed as history, always whole: the last
# HISTORY_TOOL_STEP_TURNS with their tool steps, the older ones as the
# user's text and the reply only (a tool_calls message is dropped with its
# tool messages). The oldest are dropped until the rest are under
# HISTORY_MAX_CHARS.
HISTORY_TURNS = 20
HISTORY_TOOL_STEP_TURNS = 5
HISTORY_MAX_CHARS = 200_000
# The conversation's lock is the limit a turn must stay within: every model
# attempt and every tool call starts before the turn's deadline, and a
# model attempt also ends by it (chatModel.complete), so a turn is done
# well before its lock runs out. gunicorn's --timeout 120 is above both.
TURN_DEADLINE_SECONDS = 90
CONVERSATION_LOCK_SECONDS = 110
CHAT_LIST_DEFAULT_LIMIT = 20
CHAT_LIST_MAX_LIMIT = 50
MESSAGE_LABEL = 'Your message'
CONVERSATION_NOT_FOUND = 'Conversation not found'
STILL_ANSWERING = 'Still answering your last message'
CUSTOMER_CAP_REACHED = "You've reached today's chat limit, try again tomorrow"
STORE_CAP_REACHED = "This store's chat is busy today, try again tomorrow"
TOOL_LIMIT_REACHED = 'Tool limit reached for this message'
TOO_LONG_REPLY = ('Sorry, that answer was too long. Please ask again more '
                  'simply.')
NO_REPLY = "Sorry, I couldn't answer that. Please ask again."
BEFORE_SEQ = 'beforeSeq is a whole number, 0 or more'
BEFORE_INSTANT = 'before must be an ISO 8601 date-time with a timezone'


def _now() -> datetime.datetime:
    """Now, timezone aware, in UTC: the one clock of the chat routes."""
    return datetime.datetime.now(UTC)


def _validConversationId(conversationId) -> str | None:
    """The conversation id sent: None (or "") starts a new conversation;
    anything but text is not found, never quoted."""
    if conversationId is None or conversationId == '':
        return None
    if not isinstance(conversationId, str):
        raise ValueError(CONVERSATION_NOT_FOUND)
    return conversationId


# ── Daily caps ──────────────────────────────

def _takeQuota(key: str, cap: int, expireAt: datetime.datetime) -> bool:
    """Count one message on the ChatUsage counter `key`, unless it is at
    `cap` (usageCounters.takeQuota). False when it is."""
    return takeQuota(CHAT_USAGE, key, cap, expireAt)


def _giveBackQuota(*keys: str) -> None:
    for key in keys:
        giveBackQuota(CHAT_USAGE, key)


def _takeCustomerQuota(userId: str, storeId: str) -> tuple[str, str]:
    """Count the customer's message against both daily caps, refused when
    either is reached (and then counted against neither). Returns the two
    counters' keys."""
    day = appDay(_now())
    expireAt = usageExpiry(day)
    customerKey = usageKey('customer', userId, day)
    storeKey = usageKey('store', storeId, day)
    if not _takeQuota(customerKey, CUSTOMER_DAILY_CHAT_CAP, expireAt):
        raise ValueError(CUSTOMER_CAP_REACHED)
    if not _takeQuota(storeKey, STORE_DAILY_CHAT_CAP, expireAt):
        _giveBackQuota(customerKey)
        raise ValueError(STORE_CAP_REACHED)
    return customerKey, storeKey


# ── The conversation and its lock ───────────

def _lockConversation(conversationId: str, userId: str, storeId: str,
                      audience: str, now: datetime.datetime) -> dict:
    """Take the lock (busyUntil) of the caller's conversation, unless a turn
    holds it: one findOneAndUpdate, so of two racing messages one wins.
    Only when it matches nothing does one read tell a conversation that is
    not the caller's (or none) from one still answering."""
    query = {'_id': conversationId, 'userId': userId, 'storeId': storeId,
             'audience': audience}
    locked = db.findOneAndUpdate(
        {**query, '$or': [{'busyUntil': None}, {'busyUntil': {'$lte': now}}]},
        {'$set': {'busyUntil': now + datetime.timedelta(
            seconds=CONVERSATION_LOCK_SECONDS)}},
        CONVERSATIONS,
        upsert=False,
        projection={'turnCount': 1, 'busyUntil': 1})
    if locked is not None:
        return locked
    if db.read(query, CONVERSATIONS, projection={'_id': 1},
               findOne=True) is None:
        raise ValueError(CONVERSATION_NOT_FOUND)
    raise ValueError(STILL_ANSWERING)


def _releaseLock(conversationId: str, busyUntil: datetime.datetime) -> None:
    """Clear the lock this turn took, never one a later turn took after it
    ran out."""
    db.findOneAndUpdate({'_id': conversationId, 'busyUntil': busyUntil},
                        {'$set': {'busyUntil': None}},
                        CONVERSATIONS,
                        upsert=False,
                        projection={'_id': 1})


# ── Messages, as Meta's chat API takes them ─

def _stepMessage(step: dict) -> dict:
    if step['role'] == 'tool':
        return {'role': 'tool', 'tool_call_id': step['toolCallId'],
                'content': step['content']}
    message = {'role': 'assistant', 'content': step.get('content')}
    if step.get('toolCalls'):
        message['tool_calls'] = [{
            'id': call['id'],
            'type': 'function',
            'function': {'name': call['name'],
                         'arguments': call['arguments']},
        } for call in step['toolCalls']]
    return message


def _turnMessages(turn: dict) -> list[dict]:
    """A stored turn as messages: the user's text, its steps (as read: none
    for an older turn), then the reply."""
    return [{'role': 'user', 'content': turn['userText']},
            *[_stepMessage(step) for step in turn['steps']],
            {'role': 'assistant', 'content': turn['replyText']}]


def _history(conversationId: str, seq: int) -> list[dict]:
    """The last HISTORY_TURNS turns before the turn `seq`, oldest first, as
    messages: the last HISTORY_TOOL_STEP_TURNS with their steps (an older
    turn's steps are not even read), then as many older ones as keep the
    whole under HISTORY_MAX_CHARS."""
    turns = db.aggregate([
        {'$match': {'conversationId': conversationId}},
        {'$sort': {'seq': -1}},
        {'$limit': HISTORY_TURNS},
        {'$project': {
            'userText': 1,
            'replyText': 1,
            'steps': {'$cond': [
                {'$gte': ['$seq', seq - HISTORY_TOOL_STEP_TURNS]},
                '$steps', []]},
        }},
    ], CHAT_TURNS)
    kept, size = [], 0
    for turn in turns:  # newest first
        messages = _turnMessages(turn)
        size += len(json.dumps(messages))
        if size >= HISTORY_MAX_CHARS:
            break
        kept.append(messages)
    return [message for messages in reversed(kept) for message in messages]


def _assistantStep(message) -> tuple[dict, dict]:
    """The model's tool_calls message as a ChatStep, and the refusal of
    each call that cannot run, by its stored id (chatModel.toolCallProblem:
    an over-long id, name or arguments, or no tool name). Such a call is
    stored and replayed bounded, never as sent: as UNKNOWN_TOOL with no
    arguments. A missing or over-long id, or one an earlier call of the
    message already has, is replaced by a fresh one, so the stored ids are
    unique and each tool message answers its own call. A call that is no
    function call is refused as no tool name."""
    calls, refusals, seen = [], {}, set()
    for call in message.tool_calls:
        function = getattr(call, 'function', None)
        callId = getattr(call, 'id', None)
        name = getattr(function, 'name', None)
        arguments = getattr(function, 'arguments', None) or '{}'
        problem = chatModel.toolCallProblem(callId, name, arguments)
        if (not isinstance(callId, str) or not callId
                or len(callId) > CHAT_TOOL_CALL_ID_MAX_LENGTH
                or callId in seen):
            callId = generateRandomString()
        seen.add(callId)
        if problem is not None:
            refusals[callId] = problem
            name, arguments = chatModel.UNKNOWN_TOOL, '{}'
        calls.append(ChatToolCall(id=callId, name=name, arguments=arguments))
    step = ChatStep(role='assistant', content=message.content or None,
                    toolCalls=calls).model_dump()
    return step, refusals


def _toolStep(callId: str, content: str) -> dict:
    return ChatStep(role='tool', content=content,
                    toolCallId=callId).model_dump()


class _Usage:
    """The tokens of a turn's model calls, summed."""

    def __init__(self):
        self.prompt = self.completion = self.cached = 0

    def add(self, usage) -> None:
        if usage is None:
            return
        self.prompt += usage.prompt_tokens or 0
        self.completion += usage.completion_tokens or 0
        details = getattr(usage, 'prompt_tokens_details', None)
        self.cached += getattr(details, 'cached_tokens', None) or 0

    def model(self) -> ChatTokenUsage:
        return ChatTokenUsage(promptTokens=self.prompt,
                              completionTokens=self.completion,
                              cachedTokens=self.cached)


def _runModel(audience: str, userId: str, storeId: str,
              messages: list[dict], progress: dict) -> dict:
    """Call the model until it replies in text, running its tool calls in
    between (at most MAX_TOOL_CALLS_PER_MESSAGE; past it, one last call
    without tools). Returns the reply, the steps, the tools called (as the
    log names them) and the usage. Sets progress['answered'] once the model
    answered (before any tool runs). Raises unavailable() past
    TURN_DEADLINE_SECONDS."""
    from chatTools import TOOLS
    deadline = time.monotonic() + TURN_DEADLINE_SECONDS
    steps, toolNames, usage = [], [], _Usage()
    toolsAllowed = True
    while True:
        completion = chatModel.complete(
            messages, TOOLS[audience] if toolsAllowed else None, deadline)
        progress['answered'] = True
        usage.add(completion.usage)
        choice = completion.choices[0] if completion.choices else None
        message = choice.message if choice is not None else None
        if toolsAllowed and message is not None and message.tool_calls:
            step, refusals = _assistantStep(message)
            steps.append(step)
            messages.append(_stepMessage(step))
            for call in step['toolCalls']:
                if len(toolNames) >= MAX_TOOL_CALLS_PER_MESSAGE:
                    toolsAllowed = False
                    content = chatModel.toolError(TOOL_LIMIT_REACHED)
                elif call['id'] in refusals:
                    toolNames.append(chatModel.UNKNOWN_TOOL)
                    content = chatModel.toolError(refusals[call['id']])
                else:
                    toolNames.append(chatModel.toolLogName(audience,
                                                           call['name']))
                    chatModel.remainingSeconds(deadline)
                    content = chatModel.runTool(audience, call['name'],
                                                call['arguments'], userId,
                                                storeId)
                toolStep = _toolStep(call['id'], content)
                steps.append(toolStep)
                messages.append(_stepMessage(toolStep))
            continue
        reply = ((message.content if message is not None else None)
                 or '').strip()
        if not reply:
            reply = (TOO_LONG_REPLY if choice is not None
                     and choice.finish_reason == 'length' else NO_REPLY)
        return {'reply': reply, 'steps': steps, 'toolNames': toolNames,
                'usage': usage.model()}


def _saveTurn(conversation: dict | None, conversationId: str, userId: str,
              storeId: str, seq: int, userText: str, answer: dict,
              busyUntil: datetime.datetime | None) -> None:
    """Store the turn and move its conversation on, in one transaction (on
    a replica set). `conversation` is the new conversation to insert, or
    None for an existing one (its lock, `busyUntil`, is cleared). The
    conversation is written first: without a transaction (a local mongod),
    a lost lock then stores no turn, and a turn is never stored without its
    conversation."""
    now = _now()
    turn = ChatTurn(conversationId=conversationId,
                    userId=userId,
                    storeId=storeId,
                    seq=seq,
                    userText=userText,
                    steps=[ChatStep.model_validate(step)
                           for step in answer['steps']],
                    replyText=answer['reply'],
                    toolCallCount=len(answer['toolNames']),
                    usage=answer['usage'],
                    model=chatModel.CHAT_MODEL,
                    createdAt=now,
                    _version=0).model_dump(by_alias=True)

    def save(session) -> None:
        if conversation is not None:
            db.create({**conversation, 'turnCount': 1, 'lastMessageAt': now},
                      CONVERSATIONS, session=session)
        else:
            updated = db.findOneAndUpdate(
                {'_id': conversationId, 'busyUntil': busyUntil},
                {'$inc': {'turnCount': 1, '_version': 1},
                 '$set': {'lastMessageAt': now, 'busyUntil': None}},
                CONVERSATIONS,
                upsert=False,
                session=session,
                projection={'_id': 1})
            if updated is None:
                # The lock outlives the deadline, so this needs a turn that
                # overran it: no turn is stored.
                raise RuntimeError('The conversation lock ran out mid-turn')
        db.create(turn, CHAT_TURNS, session=session)

    db.createTransaction(save)


def _chatTurn(audience: str, userId: str, storeId: str, message,
              conversationId) -> dict:
    """One message of `userId` at `storeId` and the assistant's reply,
    saved as a turn of the conversation `conversationId` (None: a new
    one). Refused (503) while CHAT_TURNS_AT_ONCE turns run."""
    with slot(_turnSlots, chatModel.unavailable):
        return _slottedTurn(audience, userId, storeId, message,
                            conversationId)


def _slottedTurn(audience: str, userId: str, storeId: str, message,
                 conversationId) -> dict:
    userText = _validText(message, MESSAGE_LABEL, CHAT_MESSAGE_MAX_LENGTH)
    conversationId = _validConversationId(conversationId)
    now = _now()
    newConversation, busyUntil, seq, history = None, None, 0, []
    if conversationId is None:
        # A new conversation is inserted only with its first turn: nobody
        # knows its id before, so it needs no lock.
        conversationId = generateRandomString()
        newConversation = Conversation(
            _id=conversationId,
            userId=userId,
            audience=audience,
            storeId=storeId,
            title=userText[:CHAT_TITLE_MAX_LENGTH],
            turnCount=0,
            createdAt=now,
            lastMessageAt=now,
            busyUntil=None,
            _version=0).model_dump(by_alias=True)
    else:
        locked = _lockConversation(conversationId, userId, storeId, audience,
                                   now)
        busyUntil, seq = locked['busyUntil'], locked['turnCount']
    started = time.monotonic()
    try:
        quotaKeys = (_takeCustomerQuota(userId, storeId)
                     if audience == 'customer' else ())
        if newConversation is None:
            history = _history(conversationId, seq)
        messages = [{'role': 'developer',
                     'content': chatModel.CHAT_PROMPTS[audience]},
                    *history,
                    {'role': 'user', 'content': userText}]
        progress = {'answered': False}
        try:
            answer = _runModel(audience, userId, storeId, messages, progress)
        except ServiceUnavailable:
            # The model never answered, so nothing ran: the message is not
            # counted.
            if not progress['answered']:
                _giveBackQuota(*quotaKeys)
            raise
        _saveTurn(newConversation, conversationId, userId, storeId, seq,
                  userText, answer, busyUntil)
        busyUntil = None  # cleared by the save
    finally:
        if busyUntil is not None:
            _releaseLock(conversationId, busyUntil)
    usage = answer['usage']
    # Never the message, the tool arguments, a name the model made up, or
    # the key.
    logging.info({
        'event': 'chatTurn',
        'conversationId': conversationId,
        'audience': audience,
        'tools': answer['toolNames'],
        'promptTokens': usage.promptTokens,
        'completionTokens': usage.completionTokens,
        'cachedTokens': usage.cachedTokens,
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return {'conversationId': conversationId, 'reply': answer['reply'],
            'turnSeq': seq}


def _validBeforeSeq(beforeSeq) -> dict:
    if beforeSeq is None:
        return {}
    if (not isinstance(beforeSeq, int) or isinstance(beforeSeq, bool)
            or beforeSeq < 0):
        raise ValueError(BEFORE_SEQ)
    return {'seq': {'$lt': beforeSeq}}


def _validBefore(before) -> dict:
    """The lastMessageAt filter of a page of conversations: those before
    the ISO instant `before` (None or "": the newest). Anything but a
    timezone-aware ISO date-time is refused, never quoted."""
    if before is None or before == '':
        return {}
    if not isinstance(before, str):
        raise ValueError(BEFORE_INSTANT)
    try:
        instant = parseIsoInstant(before, 'before')
    except ValueError:
        raise ValueError(BEFORE_INSTANT) from None
    if instant.utcoffset() is None:
        raise ValueError(BEFORE_INSTANT)
    return {'lastMessageAt': {'$lt': instant}}


def pagingCursor(instant: datetime.datetime) -> str:
    """`instant` as a paging cursor: ISO 8601 UTC with milliseconds and a
    Z, the precision MongoDB stores, so _validBefore reads it back to the
    same instant (the RFC 1123 dates Flask sends drop the milliseconds)."""
    return instant.astimezone(UTC).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')


class ChatActions():

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Reply sent',
                  responseModel=ChatReplyResponse)
    def sendAdminChatMessage(self,
                             userId: str,
                             message: str,
                             conversationId: str | None = None) -> dict:
        """Send the admin's message to the store assistant, at the store
        the admin owns, and return its reply. No conversationId starts a
        new conversation."""
        store = requireOwnerStore(userId, {'_id': 1})
        return _chatTurn('admin', userId, store['_id'], message,
                         conversationId)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Reply sent',
                  responseModel=ChatReplyResponse)
    def sendStoreChatMessage(self,
                             userId: str,
                             storeId: str,
                             message: str,
                             conversationId: str | None = None) -> dict:
        """Send the customer's message to the shopping assistant of the
        store being visited, and return its reply. No conversationId starts
        a new conversation. Capped per customer and per store each day."""
        customerAtStore(userId, storeId)
        setUpStore(storeId, {'_id': 1})
        return _chatTurn('customer', userId, storeId, message,
                         conversationId)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Conversations fetched',
                  responseModel=ConversationListResponse)
    def listMyConversations(self,
                            userId: str,
                            storeId: str | None = None,
                            before: str | None = None,
                            limit: int = CHAT_LIST_DEFAULT_LIMIT) -> dict:
        """The caller's conversations, newest first: a customer's at the
        store `storeId`, an admin's (no storeId) at the store they own.
        A full page carries nextBefore: send it back verbatim as `before`
        for the next page (the conversations last written before it); it
        is null on the last page. A lastMessageAt as sent (RFC 1123, whole
        seconds) is no cursor: it would skip rows."""
        if storeId is None or storeId == '':
            store = requireOwnerStore(userId, {'_id': 1})
            match = {'userId': userId, 'storeId': store['_id'],
                     'audience': 'admin'}
        else:
            customerAtStore(userId, storeId)
            match = {'userId': userId, 'storeId': storeId,
                     'audience': 'customer'}
        limit = _validLimit(limit, CHAT_LIST_DEFAULT_LIMIT,
                            CHAT_LIST_MAX_LIMIT)
        rows = db.aggregateResponses(ConversationRow, [
            {'$match': {**match, **_validBefore(before)}},
            {'$sort': {'lastMessageAt': -1}},
            {'$limit': limit},
        ], CONVERSATIONS)
        return {
            'conversations': rows,
            'nextBefore': (pagingCursor(rows[-1]['lastMessageAt'])
                           if len(rows) == limit else None),
        }

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Turns fetched',
                  responseModel=ConversationTurnsResponse)
    def getConversationTurns(self,
                             userId: str,
                             conversationId: str,
                             beforeSeq: int | None = None,
                             limit: int = CHAT_LIST_DEFAULT_LIMIT) -> dict:
        """The turns of the caller's own conversation, newest first: its
        user texts and replies, never its tool steps. `beforeSeq` pages
        back: only the turns before it."""
        conversationId = _validConversationId(conversationId)
        if conversationId is None or db.read(
                {'_id': conversationId, 'userId': userId}, CONVERSATIONS,
                projection={'_id': 1}, findOne=True) is None:
            raise ValueError(CONVERSATION_NOT_FOUND)
        return {
            'turns': db.aggregateResponses(ChatTurnRow, [
                {'$match': {'conversationId': conversationId,
                            'userId': userId, **_validBeforeSeq(beforeSeq)}},
                {'$sort': {'seq': -1}},
                {'$limit': _validLimit(limit, CHAT_LIST_DEFAULT_LIMIT,
                                       CHAT_LIST_MAX_LIMIT)},
            ], CHAT_TURNS),
        }

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

Attachments: a message may carry up to CHAT_ATTACHMENTS_MAX files (JPEG,
PNG or PDF) the user uploaded to Storage under users/<uid>/, sent as their
download URLs. Each URL is checked (our bucket, the caller's own folder,
the type its name says) before the slot, the lock or any quota; the files
are counted per day (per customer, per store, all stores: ChatUsage) and
read through the Storage SDK (photoStorage.readOwnFiles, never by URL)
after the lock, and go to the model as base64 data URLs. A turn stores only
each file's path, type, name and size. The files of the last
HISTORY_ATTACHMENT_TURNS turns are read again and re-sent (within
HISTORY_ATTACHMENTS_MAX_TOTAL_BYTES); older ones are a note in their
turn's text. An admin's image notes carry its URL (no token), which the
admin assistant may pass to generateProductPhotos.

A failed turn (the model unavailable, the deadline passed, a file
refused) saves nothing, and its conversation's lock is released; its
quotas are given back only when the model never answered (so no tool
ran). Tool calls it already ran stay
done (an order placed, say): the user can ask again, and the order routes
guard their own state.

The clock: every read of now is _now(), which reads this module's
`datetime`, so a test freezes it by replacing ChatActions.datetime.
"""

import base64
import datetime
import json
import logging
import threading
import time
import unicodedata

import chatModel
import photoStorage
from actions.OrderActions import customerAtStore
from actions.ProductActions import _validLimit
from actions.callSlots import slot
from actions.StoreActions import _validText, requireOwnerStore, setUpStore
# QUOTA_ATTEMPTS is imported for the tests, which read it here.
from actions.usageCounters import (QUOTA_ATTEMPTS, appDay, envCap,
                                   giveBackQuotas, takeQuota, takeQuotas,
                                   usageExpiry, usageKey)
from mongoDb import db
from objects import (CHAT_ATTACHMENT_EXTENSIONS, CHAT_ATTACHMENT_MAX_BYTES,
                     CHAT_ATTACHMENT_NAME_MAX_LENGTH, CHAT_ATTACHMENT_TYPES,
                     CHAT_ATTACHMENTS_MAX, CHAT_ATTACHMENTS_MAX_TOTAL_BYTES,
                     CHAT_MESSAGE_MAX_LENGTH, CHAT_TITLE_MAX_LENGTH,
                     CHAT_TOOL_CALL_ID_MAX_LENGTH, HISTORY_ATTACHMENT_TURNS,
                     HISTORY_ATTACHMENTS_MAX_TOTAL_BYTES, ChatAttachment,
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
# The most files a day one customer attaches (across stores), one store's
# users (its admin included) attach, and all stores' together (an
# environment variable, else its default; 0 turns attachments off).
CUSTOMER_DAILY_ATTACHMENT_CAP = 20
STORE_DAILY_ATTACHMENT_CAP = 300
CHAT_ATTACHMENTS_DAILY_CAP_ALL_VARIABLE = 'CHAT_ATTACHMENTS_DAILY_CAP_ALL'
DEFAULT_CHAT_ATTACHMENTS_DAILY_CAP_ALL = 3000
# The owner of the all-stores counter (a store _id is a random token).
ALL_STORES = 'all'
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
# well before its lock runs out. Cloud Run's request timeout (300 s by
# default) is what caps a request: gunicorn's gthread --timeout only watches
# the worker, never a request.
TURN_DEADLINE_SECONDS = 90
CONVERSATION_LOCK_SECONDS = 110
CHAT_LIST_DEFAULT_LIMIT = 20
CHAT_LIST_MAX_LIMIT = 50
MESSAGE_LABEL = 'Your message'
EMPTY_MESSAGE = 'Your message is empty: type a message or attach a file'
ATTACHMENTS_COUNT = (f'attachments lists at most {CHAT_ATTACHMENTS_MAX} '
                     'files')
ATTACHMENTS_TWICE = 'attachments lists the same file twice'
ATTACHMENT_KIND = 'file'
ATTACHMENT_TYPE = 'must be a JPG, PNG or PDF file'
ATTACHMENTS_TOO_LARGE = ('The attachments are over 20 MB together: send '
                         'fewer or smaller files')
UNREADABLE_ATTACHMENT = ("One of the attachments couldn't be read: send a "
                         'JPEG, PNG or PDF')
ATTACHMENT_CAP_REACHED = ("You've reached today's attachment limit: send "
                          'your message without files')
STORE_ATTACHMENT_CAP_REACHED = ("This store's attachments are busy today: "
                                'send your message without files')
ATTACHMENTS_TURNED_OFF = ('Attachments are turned off right now: send your '
                          'message without files')
# A new conversation's title when its first message is files only and the
# first one has no usable name.
ATTACHMENT_TITLE = 'Attachment'
PDF_TYPE = 'application/pdf'
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
    """Give one message back on each ChatUsage counter of `keys`; a failure
    is logged, never raised (usageCounters.giveBackQuotas)."""
    giveBackQuotas(CHAT_USAGE, keys)


def _customerCounters(userId: str, storeId: str,
                      day: datetime.date) -> list[tuple[str, int, str]]:
    """The customer's two daily chat counters on `day` (key, cap, refusal):
    the customer's own and the store's."""
    return [
        (usageKey('customer', userId, day), CUSTOMER_DAILY_CHAT_CAP,
         CUSTOMER_CAP_REACHED),
        (usageKey('store', storeId, day), STORE_DAILY_CHAT_CAP,
         STORE_CAP_REACHED),
    ]


def _takeCustomerQuota(userId: str, storeId: str) -> tuple[str, str]:
    """Count the customer's message against both daily caps, refused when
    either is reached (and then counted against neither). Returns the two
    counters' keys."""
    day = appDay(_now())
    customerKey, storeKey = takeQuotas(
        CHAT_USAGE, _customerCounters(userId, storeId, day),
        usageExpiry(day))
    return customerKey, storeKey


def _takeAttachmentQuota(audience: str, userId: str, storeId: str,
                         count: int) -> list[str]:
    """Count the message's `count` files against the daily attachment caps:
    the customer's (customers only, across stores), the store's and all
    stores' (CHAT_ATTACHMENTS_DAILY_CAP_ALL; 0 is attachments turned off).
    Refused when any would pass (and then counted against none:
    usageCounters.takeQuotas). Returns the counters' keys."""
    capAll = envCap(CHAT_ATTACHMENTS_DAILY_CAP_ALL_VARIABLE,
                    DEFAULT_CHAT_ATTACHMENTS_DAILY_CAP_ALL,
                    'chatAttachmentsDailyCapAllInvalid')
    if capAll == 0:
        raise ValueError(ATTACHMENTS_TURNED_OFF)
    day = appDay(_now())
    counters = []
    if audience == 'customer':
        counters.append((usageKey('attachmentsCustomer', userId, day),
                         CUSTOMER_DAILY_ATTACHMENT_CAP,
                         ATTACHMENT_CAP_REACHED))
    counters += [
        (usageKey('attachmentsStore', storeId, day),
         STORE_DAILY_ATTACHMENT_CAP, STORE_ATTACHMENT_CAP_REACHED),
        (usageKey('attachmentsAll', ALL_STORES, day), capAll,
         STORE_ATTACHMENT_CAP_REACHED),
    ]
    return takeQuotas(CHAT_USAGE, counters, usageExpiry(day), amount=count)


# ── Attachments ─────────────────────────────

def _validMessage(message, hasAttachments: bool) -> str:
    """The message's text, stripped (_validText): required without
    attachments ("" is EMPTY_MESSAGE), and "" (None too) with them."""
    if hasAttachments:
        if message is None:
            return ''
        return _validText(message, MESSAGE_LABEL, CHAT_MESSAGE_MAX_LENGTH,
                          required=False) or ''
    if isinstance(message, str) and not message.strip():
        raise ValueError(EMPTY_MESSAGE)
    return _validText(message, MESSAGE_LABEL, CHAT_MESSAGE_MAX_LENGTH)


def _validAttachments(userId: str, urls) -> list[tuple[str, str]]:
    """The label and Storage path of each file the message attaches, from
    its download URLs (None or []: none): at most CHAT_ATTACHMENTS_MAX,
    each of our bucket (photoStorage.objectPathFromUrl) under the caller's
    own users/<uid>/ folder (ownImagePath: no HEIC/HEIF), named .jpg,
    .jpeg, .png or .pdf, no file twice. Pure: reads nothing. A refusal
    names the URL's place (attachments[0]), never the URL."""
    if urls is None or urls == []:
        return []
    if not isinstance(urls, list) or len(urls) > CHAT_ATTACHMENTS_MAX:
        raise ValueError(ATTACHMENTS_COUNT)
    files = []
    for i, url in enumerate(urls):
        label = f'attachments[{i}]'
        path = photoStorage.objectPathFromUrl(url, label, ATTACHMENT_KIND)
        path = photoStorage.ownImagePath(userId, path, label, ATTACHMENT_KIND)
        if photoStorage.fileExtension(path) not in CHAT_ATTACHMENT_EXTENSIONS:
            raise ValueError(f'{label} {ATTACHMENT_TYPE}')
        files.append((label, path))
    if len({path for _, path in files}) != len(files):
        raise ValueError(ATTACHMENTS_TWICE)
    return files


def attachmentName(path: str) -> str:
    """The file name a turn stores for the file at `path`: its last
    segment, every character but a letter, a digit, a space or ._-()
    replaced by _, at most CHAT_ATTACHMENT_NAME_MAX_LENGTH (its extension
    kept)."""
    name = unicodedata.normalize('NFC', path.rpartition('/')[2])
    name = ''.join(c if c.isalnum() or c in ' ._-()' else '_'
                   for c in name).strip()
    if not name:
        return ATTACHMENT_TITLE
    if len(name) > CHAT_ATTACHMENT_NAME_MAX_LENGTH:
        extension = photoStorage.fileExtension(name)
        name = (name[:CHAT_ATTACHMENT_NAME_MAX_LENGTH - len(extension)]
                + extension)
    return name


def _readFiles(userId: str, files: list[tuple[str, str]], maxTotalBytes: int,
               deadline: float, skipUnreadable: bool = False
               ) -> list[tuple[bytes, str] | None]:
    """photoStorage.readOwnFiles with the chat's types and limits."""
    return photoStorage.readOwnFiles(
        userId, files, CHAT_ATTACHMENT_TYPES, CHAT_ATTACHMENT_MAX_BYTES,
        maxTotalBytes, deadline, wrongType=ATTACHMENT_TYPE,
        totalTooLarge=ATTACHMENTS_TOO_LARGE,
        extensionTypes=CHAT_ATTACHMENT_EXTENSIONS,
        skipUnreadable=skipUnreadable)


def _readAttachments(userId: str, files: list[tuple[str, str]],
                     deadline: float) -> list[tuple[dict, bytes]]:
    """This message's files, read before `deadline`: each as the
    ChatAttachment a turn stores, with its bytes. A file refused (gone,
    of another type, over 10 MB, over 20 MB together) is a ValueError
    naming its place; a Storage failure a 503."""
    read = _readFiles(userId, files, CHAT_ATTACHMENTS_MAX_TOTAL_BYTES,
                      deadline)
    return [(ChatAttachment(path=path, contentType=contentType,
                            name=attachmentName(path),
                            size=len(data)).model_dump(), data)
            for (_, path), (data, contentType) in zip(files, read)]


def _filePart(attachment: dict, data: bytes) -> dict:
    """A file as the content part Meta takes: an image as an image_url, a
    PDF as a file, both base64 data URLs."""
    contentType = attachment['contentType']
    dataUrl = (f'data:{contentType};base64,'
               f'{base64.b64encode(data).decode("ascii")}')
    if contentType == PDF_TYPE:
        return {'type': 'file',
                'file': {'filename': attachment['name'],
                         'file_data': dataUrl}}
    return {'type': 'image_url', 'image_url': {'url': dataUrl}}


def _described(audience: str, attachment: dict) -> str:
    """A file as a note names it: its kind and name, and for an admin an
    image's URL (no token), which generateProductPhotos takes."""
    isPdf = attachment['contentType'] == PDF_TYPE
    described = f'{"PDF" if isPdf else "image"} {attachment["name"]}'
    if audience == 'admin' and not isPdf:
        described += f', url: {photoStorage.publicPathUrl(attachment["path"])}'
    return described


def _attachmentNote(audience: str, position: int, attachment: dict) -> str:
    return f'Attachment {position + 1}: {_described(audience, attachment)}'


def _earlierNote(audience: str, attachment: dict) -> str:
    return f'[Earlier attachment: {_described(audience, attachment)}]'


def _goneNote(attachment: dict) -> str:
    return f'[Earlier attachment no longer available: {attachment["name"]}]'


def _userMessage(text: str, notes: list[str], parts: list[dict]) -> dict:
    """A user message: its text with the notes after it, then the file
    parts. With no file part its content is plain text, as before
    attachments."""
    text = '\n\n'.join(part for part in (text, '\n'.join(notes)) if part)
    if not parts:
        return {'role': 'user', 'content': text}
    return {'role': 'user',
            'content': [{'type': 'text', 'text': text}, *parts]}


def _unreadableAttachment(error: Exception) -> Exception | None:
    """chatModel.complete's refusal while the request carries files: Meta's
    400 (a file it cannot decode, or past its limits) is the user's to
    fix."""
    import openai
    if isinstance(error, openai.BadRequestError):
        return ValueError(UNREADABLE_ATTACHMENT)
    return None


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


def refuseTurnBeforeItStarts(audience: str, userId: str, storeId: str,
                             conversationId) -> None:
    """Refuse, reading only, a message _slottedTurn would refuse before the
    model: a conversation that is not the caller's (or none), one still
    answering, or (a customer) a daily cap already reached, with the same
    messages. For a caller about to pay for something first (a voice
    message's transcription): _slottedTurn still takes the lock and the
    quota itself, atomically, so a racing message can still be refused
    there."""
    conversationId = _validConversationId(conversationId)
    now = _now()
    if conversationId is not None:
        conversation = db.read(
            {'_id': conversationId, 'userId': userId, 'storeId': storeId,
             'audience': audience}, CONVERSATIONS,
            projection={'busyUntil': 1}, findOne=True)
        if conversation is None:
            raise ValueError(CONVERSATION_NOT_FOUND)
        busyUntil = conversation.get('busyUntil')
        if busyUntil is not None and busyUntil > now:
            raise ValueError(STILL_ANSWERING)
    if audience == 'customer':
        for key, cap, message in _customerCounters(userId, storeId,
                                                   appDay(now)):
            counter = db.read({'_id': key}, CHAT_USAGE,
                              projection={'count': 1}, findOne=True)
            if counter is not None and counter['count'] >= cap:
                raise ValueError(message)


def ownConversation(conversationId, userId: str, projection: dict) -> dict:
    """The caller's own conversation `conversationId` (with `projection`):
    anything else, or no id, is CONVERSATION_NOT_FOUND."""
    conversationId = _validConversationId(conversationId)
    conversation = None if conversationId is None else db.read(
        {'_id': conversationId, 'userId': userId}, CONVERSATIONS,
        projection=projection, findOne=True)
    if conversation is None:
        raise ValueError(CONVERSATION_NOT_FOUND)
    return conversation


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


def _turnMessages(turn: dict, userMessage: dict | None = None) -> list[dict]:
    """A stored turn as messages: the user's (its text, unless
    `userMessage` is given), its steps (as read: none for an older turn),
    then the reply."""
    return [userMessage or {'role': 'user', 'content': turn['userText']},
            *[_stepMessage(step) for step in turn['steps']],
            {'role': 'assistant', 'content': turn['replyText']}]


def _historyFiles(turns: list[dict], seq: int) -> set[tuple[int, int]]:
    """Which files of `turns` (newest first) are sent again, as (turn
    index, file index): those of the turns after seq -
    HISTORY_ATTACHMENT_TURNS, newest first, until the next would take their
    stored sizes past HISTORY_ATTACHMENTS_MAX_TOTAL_BYTES."""
    chosen, total = set(), 0
    for t, turn in enumerate(turns):
        if turn['seq'] < seq - HISTORY_ATTACHMENT_TURNS:
            break
        for f, attachment in enumerate(turn.get('attachments') or []):
            total += attachment['size']
            if total > HISTORY_ATTACHMENTS_MAX_TOTAL_BYTES:
                return chosen
            chosen.add((t, f))
    return chosen


def _historyUserMessage(audience: str, turn: dict, t: int,
                        resend: dict[tuple[int, int], tuple | None]) -> dict:
    """A stored turn's user message: its text, a note per file (its
    Attachment note and its part when sent again, else an Earlier note, or
    a gone one when it could not be read again)."""
    notes, parts = [], []
    for f, attachment in enumerate(turn.get('attachments') or []):
        if (t, f) not in resend:
            notes.append(_earlierNote(audience, attachment))
        elif resend[(t, f)] is None:
            notes.append(_goneNote(attachment))
        else:
            notes.append(_attachmentNote(audience, f, attachment))
            parts.append(_filePart(attachment, resend[(t, f)][0]))
    return _userMessage(turn['userText'], notes, parts)


def _history(conversationId: str, seq: int, audience: str, userId: str,
             deadline: float) -> tuple[list[dict], int]:
    """The last HISTORY_TURNS turns before the turn `seq`, oldest first, as
    messages: the last HISTORY_TOOL_STEP_TURNS with their steps (an older
    turn's steps are not even read), then as many older ones as keep the
    whole under HISTORY_MAX_CHARS (text only: a file's bytes are never
    counted). The files of the last HISTORY_ATTACHMENT_TURNS are read again
    (one readOwnFiles, before `deadline`) and sent as parts
    (_historyFiles); a file gone since is a note. Also returns how many
    were gone."""
    turns = list(db.aggregate([
        {'$match': {'conversationId': conversationId}},
        {'$sort': {'seq': -1}},
        {'$limit': HISTORY_TURNS},
        {'$project': {
            'seq': 1,
            'userText': 1,
            'replyText': 1,
            'attachments': 1,
            'steps': {'$cond': [
                {'$gte': ['$seq', seq - HISTORY_TOOL_STEP_TURNS]},
                '$steps', []]},
        }},
    ], CHAT_TURNS))
    chosen = _historyFiles(turns, seq)
    # Measured as if every chosen file were sent again: its note only.
    sentAgain = {key: (b'', '') for key in chosen}
    kept, size = [], 0
    for t, turn in enumerate(turns):  # newest first
        textOnly = _historyUserMessage(audience, turn, t, sentAgain)
        if isinstance(textOnly['content'], list):
            textOnly = {'role': 'user',
                        'content': textOnly['content'][0]['text']}
        size += len(json.dumps(_turnMessages(turn, textOnly)))
        if size >= HISTORY_MAX_CHARS:
            break
        kept.append(t)
    keys = sorted(key for key in chosen if key[0] in kept)
    read = _readFiles(
        userId, [(f'attachments[{f}]', turns[t]['attachments'][f]['path'])
                 for t, f in keys],
        HISTORY_ATTACHMENTS_MAX_TOTAL_BYTES, deadline, skipUnreadable=True)
    resend = dict(zip(keys, read))
    gone = sum(1 for value in read if value is None)
    messages = [_turnMessages(turns[t],
                              _historyUserMessage(audience, turns[t], t,
                                                  resend))
                for t in kept]
    return ([message for turnMessages in reversed(messages)
             for message in turnMessages], gone)


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
              messages: list[dict], progress: dict,
              deadline: float | None = None,
              refusal=None) -> dict:
    """Call the model until it replies in text, running its tool calls in
    between (at most MAX_TOOL_CALLS_PER_MESSAGE; past it, one last call
    without tools). Returns the reply, the steps, the tools called (as the
    log names them) and the usage. Sets progress['answered'] once the model
    answered (before any tool runs). Raises unavailable() past
    TURN_DEADLINE_SECONDS (or `deadline`). `refusal` is
    chatModel.complete's."""
    from chatTools import TOOLS
    if deadline is None:
        deadline = time.monotonic() + TURN_DEADLINE_SECONDS
    steps, toolNames, usage = [], [], _Usage()
    toolsAllowed = True
    while True:
        completion = chatModel.complete(
            messages, TOOLS[audience] if toolsAllowed else None, deadline,
            refusal=refusal)
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
              busyUntil: datetime.datetime | None,
              attachments: list[dict] = ()) -> None:
    """Store the turn and move its conversation on, in one transaction (on
    a replica set). `conversation` is the new conversation to insert, or
    None for an existing one (its lock, `busyUntil`, is cleared). The
    conversation is written first: without a transaction (a local mongod),
    a lost lock then stores no turn, and a turn is never stored without its
    conversation. `attachments` are the message's files (ChatAttachment
    dicts: never their bytes)."""
    now = _now()
    turn = ChatTurn(conversationId=conversationId,
                    userId=userId,
                    storeId=storeId,
                    seq=seq,
                    userText=userText,
                    attachments=[ChatAttachment.model_validate(attachment)
                                 for attachment in attachments],
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
              conversationId, attachments=None) -> dict:
    """One message of `userId` at `storeId` (with the files of the download
    URLs `attachments`, checked first: _validAttachments) and the
    assistant's reply, saved as a turn of the conversation `conversationId`
    (None: a new one). Refused (503) while CHAT_TURNS_AT_ONCE turns run."""
    files = _validAttachments(userId, attachments)
    with slot(_turnSlots, chatModel.unavailable):
        return _slottedTurn(audience, userId, storeId, message,
                            conversationId, attachments=files)


def _newTitle(userText: str, files: list[tuple[str, str]]) -> str:
    """A new conversation's title: its first message, else (files only)
    the first file's name."""
    title = userText or attachmentName(files[0][1])
    return title[:CHAT_TITLE_MAX_LENGTH]


def _slottedTurn(audience: str, userId: str, storeId: str, message,
                 conversationId, attachments=()) -> dict:
    """The turn of _chatTurn, in its slot. `attachments` are the
    message's files, checked (_validAttachments: their labels and paths);
    the voice route sends none."""
    files = list(attachments)
    userText = _validMessage(message, bool(files))
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
            title=_newTitle(userText, files),
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
    deadline = started + TURN_DEADLINE_SECONDS
    quotaKeys, attachmentKeys, sent, gone = (), (), [], 0
    try:
        progress = {'answered': False}
        try:
            quotaKeys = (_takeCustomerQuota(userId, storeId)
                         if audience == 'customer' else ())
            if files:
                attachmentKeys = _takeAttachmentQuota(audience, userId,
                                                      storeId, len(files))
            sent = _readAttachments(userId, files, deadline)
            if newConversation is None:
                history, gone = _history(conversationId, seq, audience,
                                         userId, deadline)
            userMessage = _userMessage(
                userText,
                [_attachmentNote(audience, i, attachment)
                 for i, (attachment, _) in enumerate(sent)],
                [_filePart(attachment, data) for attachment, data in sent])
            messages = [{'role': 'developer',
                         'content': chatModel.CHAT_PROMPTS[audience]},
                        *history,
                        userMessage]
            carriesFiles = any(isinstance(m.get('content'), list)
                               for m in messages if m['role'] == 'user')
            answer = _runModel(
                audience, userId, storeId, messages, progress,
                deadline=deadline,
                refusal=_unreadableAttachment if carriesFiles else None)
        except (ServiceUnavailable, ValueError):
            # The model never answered, so nothing ran: the message and its
            # files are not counted (a Storage failure, a file refused).
            if not progress['answered']:
                _giveBackQuota(*quotaKeys)
                giveBackQuotas(CHAT_USAGE, attachmentKeys, len(files))
            raise
        _saveTurn(newConversation, conversationId, userId, storeId, seq,
                  userText, answer, busyUntil,
                  [attachment for attachment, _ in sent])
        busyUntil = None  # cleared by the save
    finally:
        if busyUntil is not None:
            _releaseLock(conversationId, busyUntil)
    if gone:
        logging.info({'event': 'chatHistoryAttachmentsGone',
                      'conversationId': conversationId, 'count': gone})
    usage = answer['usage']
    # Never the message, the tool arguments, a name the model made up, a
    # file's path, name, URL or content, or the key.
    # A message with files adds their count and bytes.
    logging.info({
        'event': 'chatTurn',
        'conversationId': conversationId,
        'audience': audience,
        'tools': answer['toolNames'],
        **({'attachments': len(sent),
            'attachmentBytes': sum(attachment['size']
                                   for attachment, _ in sent)}
           if sent else {}),
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
                             conversationId: str | None = None,
                             attachments: list[str] | None = None) -> dict:
        """Send the admin's message to the store assistant, at the store
        the admin owns, and return its reply. No conversationId starts a
        new conversation. `attachments` are the Storage download URLs of
        up to 4 JPEGs, PNGs or PDFs the admin uploaded under users/<uid>/
        (the message may then be empty)."""
        store = requireOwnerStore(userId, {'_id': 1})
        return _chatTurn('admin', userId, store['_id'], message,
                         conversationId, attachments)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Reply sent',
                  responseModel=ChatReplyResponse)
    def sendStoreChatMessage(self,
                             userId: str,
                             storeId: str,
                             message: str,
                             conversationId: str | None = None,
                             attachments: list[str] | None = None) -> dict:
        """Send the customer's message to the shopping assistant of the
        store being visited, and return its reply. No conversationId starts
        a new conversation. `attachments` are the Storage download URLs of
        up to 4 JPEGs, PNGs or PDFs the customer uploaded under
        users/<uid>/ (the message may then be empty). Capped per customer
        and per store each day, messages and files alike."""
        customerAtStore(userId, storeId)
        setUpStore(storeId, {'_id': 1})
        return _chatTurn('customer', userId, storeId, message,
                         conversationId, attachments)

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
        user texts, their files (path, type, name; no `attachments` key on
        a turn without any) and replies, never its tool steps. `beforeSeq` pages
        back: only the turns before it."""
        conversationId = ownConversation(conversationId, userId,
                                         {'_id': 1})['_id']
        return {
            'turns': db.aggregateResponses(ChatTurnRow, [
                {'$match': {'conversationId': conversationId,
                            'userId': userId, **_validBeforeSeq(beforeSeq)}},
                {'$sort': {'seq': -1}},
                {'$limit': _validLimit(limit, CHAT_LIST_DEFAULT_LIMIT,
                                       CHAT_LIST_MAX_LIMIT)},
                # A turn without files sends no attachments key, as one
                # stored before attachments.
                {'$set': {'attachments': {'$cond': [
                    {'$gt': [{'$size': {'$ifNull': ['$attachments', []]}},
                             0]},
                    '$attachments', '$$REMOVE']}}},
            ], CHAT_TURNS),
        }

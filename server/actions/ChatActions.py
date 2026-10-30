"""ChatActions: the chat with the store's assistant (Meta's Muse model).

A message goes to the model with its toolset's tools (chatTools.py,
generated from the @mcp_tool routes and chatForms.py's form tools), and the
model's tool calls run here, in-process, as the signed-in user at the
conversation's store (chatModel.runTool, chatForms.runFormTool). The loop
ends at the model's text reply, the turn's cards and forms are built from
what its tools returned (chatCards.py), and the turn is saved: a
Conversations document per chat, a ChatTurns document per message
(objects/chat.py). Conversations are kept forever.

A user's turn is a message, or the answer to a form or a card shown in the
same conversation (`form`, chatForms.answerTurn): checked against the
component it answers, once, under the conversation's lock. The model reads
its details (userContent) and acts on them.

An admin whose store is not set up yet (the empty store sign-up creates)
chats with the setup toolset only (chatForms.SETUP_TOOLSET).

Who and where come only from the JWT's userId and the conversation's
storeId: an admin chats at the store they own (requireAnyOwnerStore), a
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
read through the Storage SDK (photoStorage.ownFiles, downloadFiles: never
by URL, their contents checked) after the lock, and go to the model as
base64 data URLs. A turn stores only each file's path, type, name and
size. One request sends at most CHAT_ATTACHMENTS_MAX_TOTAL_BYTES of files
(this message's first, refused past it).

A message carries at most one PDF (ONE_PDF_PER_MESSAGE), and its own
files are not weighed further. A PDF of about 48 pages or more sent with
images can exceed Muse's 50 images per request
(chatModel.MAX_IMAGES_PER_REQUEST): Meta then refuses it, the user sees
UNREADABLE_ATTACHMENT and the quotas are kept. This is accepted.

History is bounded to what the message leaves, in bytes and in images
(a PDF weighs PDF_IMAGE_WEIGHT; never below 0): within it, the
files of the last HISTORY_ATTACHMENT_TURNS turns are read again and
re-sent, newest first; the others are a note in their turn's text. When
Meta refuses the first call of a request whose history carried files,
the turn is retried once with those files as notes (same deadline, same
reserved bytes, no further quota; that refusal is logged as a warning,
the expected path), so one file it cannot read never breaks a
conversation for good. The bytes a process holds are
capped (CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX, reserved from the stored
sizes before any download): past it this message is a 503, and a history
file a note. An admin's image notes carry its URL (no token), which the
admin assistant may pass to generateProductPhotos.

A failed turn (the model unavailable, the deadline passed, a file
refused) saves nothing, and its conversation's lock is released. Its
quotas are given back when it failed before the request went to the model,
or when the model was unavailable (a 503) before it answered; never once
it answered (a tool may have run), nor for Meta's refusal of this
message's files (they reached it). Meta's refusal of a retry without
history files when this message carries none is ours: a 503, the quotas
given back. Tool calls it already ran stay
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

import chatCards
import chatForms
import chatModel
import chatThinking
import photoStorage
from actions.OrderActions import customerAtStore
from actions.ProductActions import _validLimit
from actions.callSlots import slot
from actions.StoreActions import (_validText, isSetUp, requireAnyOwnerStore,
                                  setUpStore)
# QUOTA_ATTEMPTS is imported for the tests, which read it here.
from actions.usageCounters import (QUOTA_ATTEMPTS, appDay, envCap,
                                   giveBackQuotas, takeDailyQuotas,
                                   takeQuota, takeQuotas, usageExpiry,
                                   usageKey)
from mongoDb import db
from objects import (CHAT_ATTACHMENT_EXTENSIONS, CHAT_ATTACHMENT_MAX_BYTES,
                     CHAT_ATTACHMENT_NAME_MAX_LENGTH, CHAT_ATTACHMENT_TYPES,
                     CHAT_ATTACHMENTS_MAX, CHAT_ATTACHMENTS_MAX_TOTAL_BYTES,
                     CHAT_MESSAGE_MAX_LENGTH, CHAT_TITLE_MAX_LENGTH,
                     CHAT_TOOL_CALL_ID_MAX_LENGTH, HISTORY_ATTACHMENT_TURNS,
                     ChatAttachment, ChatFormAnswer, ChatThinkingResponse,
                     ChatReplyResponse, ChatStep, ChatTokenUsage,
                     ChatToolCall, ChatTurn, ChatTurnRow, Conversation,
                     ConversationListResponse, ConversationRow,
                     ConversationTurnsResponse)
from route_config import route_config
from services.__request import ServiceUnavailable
from utils import generateRandomString, parseIsoInstant

CONVERSATIONS = 'Conversations'
CHAT_TURNS = chatForms.CHAT_TURNS
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
# The attachment bytes all of a process's turns hold at once, reserved
# from the stored sizes before any download. It admits three turns with
# their whole 20 MB budget: a fourth at once is a 503 (its own files), or
# its history files become notes. It counts raw bytes: a turn's real peak
# is about 4 times them (the base64 data URLs, then the SDK's request
# body), about 240 MB at the cap, sized for the 512Mi Cloud Run service.
# Lower it (40 MB) only if a load test shows memory pressure.
CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX = 60 * 1024 * 1024
_bytesInFlight = 0
_bytesInFlightLock = threading.Lock()
# What routeErrors' 503 names when the process holds too many bytes:
# "Attachment reading is unavailable right now. Please try again."
ATTACHMENTS_SERVICE = 'attachment reading'
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
# Meta reads up to 50 page images of a PDF, its limit per request: a second
# PDF could pass it.
ONE_PDF_PER_MESSAGE = 'Attach at most one PDF per message'
FORM_WITH_ATTACHMENTS = 'Send a form answer without attachments'
ATTACHMENT_KIND = 'file'
ATTACHMENT_TYPE = 'must be a JPG, PNG or PDF file'
ATTACHMENTS_TOO_LARGE = (
    f'The attachments are over '
    f'{photoStorage.megabytes(CHAT_ATTACHMENTS_MAX_TOTAL_BYTES)} together: '
    'send fewer or smaller files')
NOT_A_REAL_ATTACHMENT = "isn't a real JPEG, PNG or PDF file"
UNREADABLE_ATTACHMENT = ("One of the attachments couldn't be read, or they "
                         'have too many pages together: send fewer or '
                         'shorter JPEG, PNG or PDF files')
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


def _takeCustomerQuota(userId: str, storeId: str,
                       day: datetime.date) -> tuple[str, str]:
    """Count the customer's message against both daily caps of `day`,
    refused when either is reached (and then counted against neither).
    Returns the two counters' keys."""
    customerKey, storeKey = takeQuotas(
        CHAT_USAGE, _customerCounters(userId, storeId, day),
        usageExpiry(day))
    return customerKey, storeKey


def _takeAttachmentQuota(audience: str, userId: str, storeId: str,
                         count: int, day: datetime.date) -> list[str]:
    """Count the message's `count` files against the daily attachment caps
    of `day`: the customer's (customers only, across stores), the store's
    and all stores' (CHAT_ATTACHMENTS_DAILY_CAP_ALL; 0 is attachments
    turned off). Refused when any would pass (and then counted against
    none: usageCounters.takeDailyQuotas). Returns the counters' keys."""
    counters = []
    if audience == 'customer':
        counters.append(('attachmentsCustomer', userId,
                         CUSTOMER_DAILY_ATTACHMENT_CAP,
                         ATTACHMENT_CAP_REACHED))
    counters.append(('attachmentsStore', storeId, STORE_DAILY_ATTACHMENT_CAP,
                     STORE_ATTACHMENT_CAP_REACHED))
    return takeDailyQuotas(CHAT_USAGE, counters,
                           ('attachmentsAll', STORE_ATTACHMENT_CAP_REACHED),
                           envCap(CHAT_ATTACHMENTS_DAILY_CAP_ALL_VARIABLE,
                                  DEFAULT_CHAT_ATTACHMENTS_DAILY_CAP_ALL,
                                  'chatAttachmentsDailyCapAllInvalid'),
                           ATTACHMENTS_TURNED_OFF, day, amount=count)


class _BytesInFlight:
    """The attachment bytes one turn holds, counted against the process's
    CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX: reserve() before a download,
    release() (all of them) when the turn ends."""

    def __init__(self):
        self.held = 0

    def reserve(self, size: int) -> bool:
        """Hold `size` more bytes; False (nothing held) when the process
        would pass its cap."""
        global _bytesInFlight
        with _bytesInFlightLock:
            if _bytesInFlight + size > CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX:
                return False
            _bytesInFlight += size
        self.held += size
        return True

    def release(self) -> None:
        global _bytesInFlight
        with _bytesInFlightLock:
            _bytesInFlight -= self.held
        self.held = 0


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
    .jpeg, .png or .pdf, no file twice, at most one PDF
    (ONE_PDF_PER_MESSAGE). Pure: reads nothing. A refusal
    names the URL's place (attachments[0]), never the URL."""
    if urls is None or urls == []:
        return []
    if not isinstance(urls, list) or len(urls) > CHAT_ATTACHMENTS_MAX:
        raise ValueError(ATTACHMENTS_COUNT)
    files = photoStorage.ownPathsOfUrls(userId, urls, 'attachments',
                                        ATTACHMENTS_TWICE, ATTACHMENT_KIND)
    for label, path in files:
        if photoStorage.fileExtension(path) not in CHAT_ATTACHMENT_EXTENSIONS:
            raise ValueError(f'{label} {ATTACHMENT_TYPE}')
    if sum(1 for _, path in files
           if CHAT_ATTACHMENT_EXTENSIONS[photoStorage.fileExtension(path)]
           == PDF_TYPE) > 1:
        raise ValueError(ONE_PDF_PER_MESSAGE)
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


def _imageWeight(contentType: str) -> int:
    """How many of Meta's MAX_IMAGES_PER_REQUEST a file may take: a PDF
    its first PDF_IMAGE_WEIGHT page images, an image one."""
    return (chatModel.PDF_IMAGE_WEIGHT if contentType == PDF_TYPE
            else chatModel.IMAGE_WEIGHT)


def _ownFiles(userId: str, files: list[tuple[str, str]], deadline: float,
              skipUnreadable: bool = False) -> list:
    """photoStorage.ownFiles with the chat's types and limits."""
    return photoStorage.ownFiles(
        userId, files, CHAT_ATTACHMENT_TYPES, CHAT_ATTACHMENT_MAX_BYTES,
        deadline, wrongType=ATTACHMENT_TYPE,
        extensionTypes=CHAT_ATTACHMENT_EXTENSIONS, kind=ATTACHMENT_KIND,
        skipUnreadable=skipUnreadable)


def _downloadFiles(files: list, deadline: float,
                   skipUnreadable: bool = False) -> list:
    """photoStorage.downloadFiles with the chat's limits."""
    return photoStorage.downloadFiles(
        files, CHAT_ATTACHMENT_MAX_BYTES, deadline,
        notReal=NOT_A_REAL_ATTACHMENT, skipUnreadable=skipUnreadable)


def _busy() -> ServiceUnavailable:
    """The 503 of a message whose files the process cannot hold now."""
    return ServiceUnavailable(ATTACHMENTS_SERVICE, 'chat')


def _newUserMessage(audience: str, userId: str, userText: str,
                    files: list[tuple[str, str]], inFlight: _BytesInFlight,
                    deadline: float) -> tuple[dict, list[dict]]:
    """This message as Meta takes it (its text, a note per file, a part per
    file), and its files as the ChatAttachments a turn stores. Their
    metadata is checked first (gone, of another type, over 10 MB: a
    ValueError naming its place), then their sizes together
    (ATTACHMENTS_TOO_LARGE) and the process's bytes (inFlight: else a 503),
    then they are downloaded and their contents checked. The bytes are
    dropped once the parts are built. A Storage failure is a 503."""
    if not files:
        return _userMessage(userText, [], []), []
    owned = _ownFiles(userId, files, deadline)
    total = sum(file.size for file in owned)
    if total > CHAT_ATTACHMENTS_MAX_TOTAL_BYTES:
        raise ValueError(ATTACHMENTS_TOO_LARGE)
    if not inFlight.reserve(total):
        raise _busy()
    notes, parts, attachments = [], [], []
    for i, (file, data) in enumerate(zip(owned, _downloadFiles(owned,
                                                               deadline))):
        attachment = ChatAttachment(path=file.path,
                                    contentType=file.contentType,
                                    name=attachmentName(file.path),
                                    size=len(data)).model_dump()
        notes.append(_attachmentNote(audience, i, attachment))
        parts.append(_filePart(attachment, data))
        attachments.append(attachment)
    return _userMessage(userText, notes, parts), attachments


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


def _skippedNote(attachment: dict) -> str:
    return (f'[Earlier attachment could not be sent again: '
            f'{attachment["name"]}]')


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
    """chatModel.complete's refusal on the first model call of a request
    carrying this message's files: Meta's 400 (a file it cannot decode, or
    too many pages) is the user's to fix. A later call's 400 is ours (a
    503)."""
    import openai
    if isinstance(error, openai.BadRequestError):
        return ValueError(UNREADABLE_ATTACHMENT)
    return None


class _HistoryFilesRefused(Exception):
    """Meta's 400 on the first model call of a request whose history
    carried files: _slottedTurn retries once with them as notes."""


def _historyFilesRefused(error: Exception) -> Exception | None:
    import openai
    if isinstance(error, openai.BadRequestError):
        return _HistoryFilesRefused()
    return None


def _ourRequestRefused(error: Exception) -> Exception | None:
    """Meta's 400 on a request carrying no file of this message, retried
    without the history's: ours, a 503 (logged as an ERROR)."""
    import openai
    if isinstance(error, openai.BadRequestError):
        return chatModel.unavailable()
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
    """A stored turn as messages: the user's turn as the model read it
    (`userMessage` when given, else a form answer's userContent, else its
    text), its steps (as read: none for an older turn), then the reply."""
    return [userMessage or {'role': 'user',
                            'content': turn.get('userContent')
                            or turn['userText']},
            *[_stepMessage(step) for step in turn['steps']],
            {'role': 'assistant', 'content': turn['replyText']}]


def _historyFiles(turns: list[dict], seq: int, budget: int,
                  weight: int) -> list[tuple[int, int]]:
    """Which files of `turns` (newest first) are sent again, as (turn
    index, file index), newest first: those of the turns after seq -
    HISTORY_ATTACHMENT_TURNS, until the next would take their stored sizes
    past `budget` bytes or their image weight (_imageWeight) past
    `weight`."""
    chosen, total, weighed = [], 0, 0
    for t, turn in enumerate(turns):
        if turn['seq'] < seq - HISTORY_ATTACHMENT_TURNS:
            break
        for f, attachment in enumerate(turn.get('attachments') or []):
            total += attachment['size']
            weighed += _imageWeight(attachment['contentType'])
            if total > budget or weighed > weight:
                return chosen
            chosen.append((t, f))
    return chosen


def _historyUserMessage(audience: str, turn: dict, t: int, status: dict,
                        withParts: bool = True) -> dict:
    """A stored turn's user message: its text and a note per file, by its
    status (turn index, file index: its bytes, GONE or REFUSED; none, an
    Earlier note). A file sent again gets its Attachment note and (with
    `withParts`) its part."""
    notes, parts = [], []
    for f, attachment in enumerate(turn.get('attachments') or []):
        value = status.get((t, f))
        if value is None:
            notes.append(_earlierNote(audience, attachment))
        elif value == photoStorage.GONE:
            notes.append(_goneNote(attachment))
        elif value == photoStorage.REFUSED:
            notes.append(_skippedNote(attachment))
        else:
            notes.append(_attachmentNote(audience, f, attachment))
            if withParts:
                parts.append(_filePart(attachment, value))
    return _userMessage(turn.get('userContent') or turn['userText'],
                        notes, parts)


def _readHistoryFiles(userId: str, turns: list[dict],
                      keys: list[tuple[int, int]], budget: int, weight: int,
                      inFlight: _BytesInFlight, deadline: float) -> dict:
    """The status of each history file of `keys` (newest first): its
    bytes, GONE (deleted since) or REFUSED (of another type, too large, past
    `budget` or `weight` by its actual size, past the process's bytes, or
    contents not its type's). One metadata read, then one download, of
    them all."""
    owned = _ownFiles(userId, [
        (f'attachments[{f}]', turns[t]['attachments'][f]['path'])
        for t, f in keys], deadline, skipUnreadable=True)
    status, toRead, total, weighed = {}, [], 0, 0
    for key, file in zip(keys, owned):
        if isinstance(file, str):
            status[key] = file
            continue
        total += file.size
        weighed += _imageWeight(file.contentType)
        if (total > budget or weighed > weight
                or not inFlight.reserve(file.size)):
            total -= file.size
            weighed -= _imageWeight(file.contentType)
            status[key] = photoStorage.REFUSED
            continue
        toRead.append((key, file))
    read = _downloadFiles([file for _, file in toRead], deadline,
                          skipUnreadable=True)
    for (key, _), value in zip(toRead, read):
        status[key] = value
    return status


def _history(conversationId: str, seq: int, audience: str, userId: str,
             deadline: float, budget: int, weight: int,
             inFlight: _BytesInFlight) -> dict:
    """The last HISTORY_TURNS turns before the turn `seq`, oldest first, as
    messages: the last HISTORY_TOOL_STEP_TURNS with their steps (an older
    turn's steps are not even read), then as many older ones as keep the
    whole under HISTORY_MAX_CHARS (text only: a file's bytes are never
    counted). The files of the last HISTORY_ATTACHMENT_TURNS are read again
    and sent as parts, within `budget` bytes and `weight` images
    (_historyFiles, _readHistoryFiles, before `deadline`); a file gone
    since, or that could not be sent again, is a note. Returns the
    messages, the same without any file part (each file sent again an
    Earlier note: the retry of _slottedTurn), how many files were sent
    again, how many gone and how many skipped."""
    turns = list(db.aggregate([
        {'$match': {'conversationId': conversationId}},
        {'$sort': {'seq': -1}},
        {'$limit': HISTORY_TURNS},
        {'$project': {
            'seq': 1,
            'userText': 1,
            'userContent': 1,
            'replyText': 1,
            'attachments': 1,
            'steps': {'$cond': [
                {'$gte': ['$seq', seq - HISTORY_TOOL_STEP_TURNS]},
                '$steps', []]},
        }},
    ], CHAT_TURNS))
    chosen = _historyFiles(turns, seq, budget, weight)
    # Measured as if every chosen file were sent again: its note only.
    asSent = {key: b'' for key in chosen}
    kept, size = [], 0
    for t, turn in enumerate(turns):  # newest first
        textOnly = _historyUserMessage(audience, turn, t, asSent,
                                       withParts=False)
        size += len(json.dumps(_turnMessages(turn, textOnly)))
        if size >= HISTORY_MAX_CHARS:
            break
        kept.append(t)
    status = _readHistoryFiles(userId, turns,
                               [key for key in chosen if key[0] in kept],
                               budget, weight, inFlight, deadline)
    gone = sum(1 for value in status.values() if value == photoStorage.GONE)
    skipped = sum(1 for value in status.values()
                  if value == photoStorage.REFUSED)
    resent = {key for key, value in status.items()
              if isinstance(value, bytes)}
    notesOnly = {key: value for key, value in status.items()
                 if key not in resent}

    def messages(fileStatus: dict) -> list[dict]:
        return [message for t in reversed(kept)
                for message in _turnMessages(
                    turns[t], _historyUserMessage(audience, turns[t], t,
                                                  fileStatus))]
    return {'messages': messages(status),
            'withoutFiles': messages(notesOnly) if resent else None,
            'resent': len(resent), 'gone': gone, 'skipped': skipped}


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


def _runModel(toolset: str, userId: str, storeId: str,
              messages: list[dict], progress: dict,
              deadline: float | None = None,
              refusal=None, refusalLevel: int = logging.ERROR,
              turnKey: str | None = None) -> dict:
    """Call the model with the tools of `toolset` (chatTools.TOOLS) until
    it replies in text, running its tool calls in between (at most
    MAX_TOOL_CALLS_PER_MESSAGE; past it, one last call without tools): a
    route tool through chatModel.runTool, a form tool through
    chatForms.runFormTool. Returns the reply, the steps, the forms opened
    (by tool call id), the tools called (as the log names them) and the
    usage. Sets progress['sent'] just before the
    first model call and progress['answered'] once the model answered
    (before any tool runs). Raises unavailable() past
    TURN_DEADLINE_SECONDS (or `deadline`). `refusal` is
    chatModel.complete's, on the first call only, logged at
    `refusalLevel`. Before each route tool runs, one of its thinking texts
    is saved as what the turn `turnKey` is doing (chatThinking.note)."""
    from chatTools import TOOLS
    if deadline is None:
        deadline = time.monotonic() + TURN_DEADLINE_SECONDS
    steps, toolNames, usage = [], [], _Usage()
    forms = {}
    formTools = chatForms.formToolNames(toolset)
    toolsAllowed = True
    while True:
        progress['sent'] = True
        completion = chatModel.complete(
            messages, TOOLS[toolset] if toolsAllowed else None, deadline,
            refusal=None if progress['answered'] else refusal,
            refusalLevel=refusalLevel)
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
                elif call['name'] in formTools:
                    toolNames.append(call['name'])
                    chatModel.remainingSeconds(deadline)
                    content, form = chatForms.runFormTool(
                        toolset, call['name'], call['arguments'], userId,
                        storeId)
                    if form is not None:
                        forms[call['id']] = form
                else:
                    toolNames.append(chatModel.toolLogName(toolset,
                                                           call['name']))
                    chatModel.remainingSeconds(deadline)
                    chatThinking.note(userId, storeId, turnKey,
                                      call['name'])
                    content = chatModel.runTool(toolset, call['name'],
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
        return {'reply': reply, 'steps': steps, 'forms': forms,
                'toolNames': toolNames, 'usage': usage.model()}


def _saveTurn(conversation: dict | None, conversationId: str, userId: str,
              storeId: str, seq: int, userTurn: dict, answer: dict,
              components: list[dict],
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
                    userText=userTurn['userText'],
                    userContent=userTurn['userContent'],
                    attachments=[ChatAttachment.model_validate(attachment)
                                 for attachment in attachments],
                    steps=[ChatStep.model_validate(step)
                           for step in answer['steps']],
                    replyText=answer['reply'],
                    components=components,
                    formAnswer=userTurn['formAnswer'],
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


def _chatTurn(audience: str, toolset: str, userId: str, storeId: str,
              message, conversationId, form=None, attachments=None,
              turnKey=None) -> dict:
    """One message (or form answer) of `userId` at `storeId` (with the
    files of the download URLs `attachments`, checked first:
    _validAttachments) and the assistant's reply, with the tools of
    `toolset`, saved as a turn of the conversation `conversationId` (None:
    a new one). `turnKey` names the turn to getChatThinking.
    Refused (503) while CHAT_TURNS_AT_ONCE turns run."""
    chatThinking.validTurnKey(turnKey)
    files = _validAttachments(userId, attachments)
    if files and form is not None:
        raise ValueError(FORM_WITH_ATTACHMENTS)
    with slot(_turnSlots, chatModel.unavailable):
        return _slottedTurn(audience, toolset, userId, storeId, message,
                            conversationId, form, attachments=files,
                            turnKey=turnKey)


def _userTurn(message, formAnswer, conversationId: str | None,
              userId: str, hasAttachments: bool = False) -> dict:
    """The user's side of a turn: its text, what the model reads (null:
    the text) and the form answer. A message's text may be empty with
    attachments (_validMessage). A form answer is checked against the form
    it answers (chatForms.answerTurn), so it needs its conversation's lock:
    called with it held."""
    if formAnswer is None:
        return {'userText': _validMessage(message, hasAttachments),
                'userContent': None, 'formAnswer': None}
    userText, userContent = chatForms.answerTurn(conversationId, userId,
                                                 formAnswer)
    return {'userText': userText, 'userContent': userContent,
            'formAnswer': formAnswer.model_dump()}


def _newTitle(userText: str, files: list[tuple[str, str]]) -> str:
    """A new conversation's title: its first message, else (files only)
    the first file's name."""
    title = userText or attachmentName(files[0][1])
    return title[:CHAT_TITLE_MAX_LENGTH]


def _slottedTurn(audience: str, toolset: str, userId: str, storeId: str,
                 message, conversationId, form, attachments=(),
                 turnKey=None) -> dict:
    """The turn of _chatTurn, in its slot. `attachments` are the
    message's files, checked (_validAttachments: their labels and paths;
    _chatTurn refuses them with a form answer); the voice route sends
    none. What the turn `turnKey` is doing is cleared when it ends."""
    turnKey = chatThinking.validTurnKey(turnKey)
    files = list(attachments)
    formAnswer = chatForms.validAnswer(form)
    if formAnswer is None:
        userTurn = _userTurn(message, None, None, userId, bool(files))
    elif message is not None and message != '':
        raise ValueError(chatForms.MESSAGE_AND_FORM)
    conversationId = _validConversationId(conversationId)
    if formAnswer is not None and conversationId is None:
        raise ValueError(chatForms.FORM_NEEDS_CONVERSATION)
    now = _now()
    day = appDay(now)
    newConversation, busyUntil, seq = None, None, 0
    history = {'messages': [], 'withoutFiles': None, 'resent': 0,
               'gone': 0, 'skipped': 0}
    if conversationId is None:
        # A new conversation is inserted only with its first turn: nobody
        # knows its id before, so it needs no lock.
        conversationId = generateRandomString()
        newConversation = Conversation(
            _id=conversationId,
            userId=userId,
            audience=audience,
            storeId=storeId,
            title=_newTitle(userTurn['userText'], files),
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
    quotaKeys, attachmentKeys, sent = (), (), []
    inFlight = _BytesInFlight()
    try:
        progress = {'sent': False, 'answered': False}
        try:
            if formAnswer is not None:
                userTurn = _userTurn(None, formAnswer, conversationId,
                                     userId)
            quotaKeys = (_takeCustomerQuota(userId, storeId, day)
                         if audience == 'customer' else ())
            if files:
                attachmentKeys = _takeAttachmentQuota(
                    audience, userId, storeId, len(files), day)
            userMessage, sent = _newUserMessage(
                audience, userId,
                userTurn['userContent'] or userTurn['userText'], files,
                inFlight, deadline)
            if newConversation is None:
                # What this message leaves, never below 0 (a PDF with
                # images may weigh past the 50 alone).
                history = _history(
                    conversationId, seq, audience, userId, deadline,
                    max(0, CHAT_ATTACHMENTS_MAX_TOTAL_BYTES -
                        sum(attachment['size'] for attachment in sent)),
                    max(0, chatModel.MAX_IMAGES_PER_REQUEST -
                        sum(_imageWeight(attachment['contentType'])
                            for attachment in sent)),
                    inFlight)
            ownRefusal = _unreadableAttachment if sent else None

            def run(historyMessages: list[dict], refusal,
                    refusalLevel: int = logging.ERROR) -> dict:
                return _runModel(
                    toolset, userId, storeId,
                    [{'role': 'developer',
                      'content': chatModel.CHAT_PROMPTS[toolset]},
                     *historyMessages, userMessage],
                    progress, deadline=deadline, refusal=refusal,
                    refusalLevel=refusalLevel, turnKey=turnKey)
            if history['withoutFiles'] is None:
                answer = run(history['messages'], ownRefusal)
            else:
                try:
                    # The expected path: a warning, then the retry.
                    answer = run(history['messages'], _historyFilesRefused,
                                 logging.WARNING)
                except _HistoryFilesRefused:
                    # Once, with the history's files as notes: its own
                    # files refused again are the user's, else it is ours.
                    logging.info({'event': 'chatHistoryAttachmentsRefused',
                                  'conversationId': conversationId,
                                  'count': history['resent']})
                    history['messages'] = history['withoutFiles']
                    answer = run(history['messages'],
                                 ownRefusal or _ourRequestRefused)
        except Exception as error:
            # Given back when nothing reached the model (a file refused, a
            # Storage or database failure), or when the model was
            # unavailable before it answered (so no tool ran); kept once it
            # answered, and when Meta refused the files it was sent.
            if not progress['sent'] or (
                    not progress['answered']
                    and isinstance(error, ServiceUnavailable)):
                _giveBackQuota(*quotaKeys)
                giveBackQuotas(CHAT_USAGE, attachmentKeys, len(files))
            raise
        components = chatCards.build(answer['steps'], answer['forms'],
                                     chatCards.CardContext(storeId))
        _saveTurn(newConversation, conversationId, userId, storeId, seq,
                  userTurn, answer, components, busyUntil, sent)
        busyUntil = None  # cleared by the save
    finally:
        inFlight.release()
        chatThinking.clear(userId, turnKey)
        if busyUntil is not None:
            _releaseLock(conversationId, busyUntil)
    if history['gone']:
        logging.info({'event': 'chatHistoryAttachmentsGone',
                      'conversationId': conversationId,
                      'count': history['gone']})
    if history['skipped']:
        logging.info({'event': 'chatHistoryAttachmentsSkipped',
                      'conversationId': conversationId,
                      'count': history['skipped']})
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
                                   for attachment in sent)}
           if sent else {}),
        'promptTokens': usage.promptTokens,
        'completionTokens': usage.completionTokens,
        'cachedTokens': usage.cachedTokens,
        'durationMs': round((time.monotonic() - started) * 1000),
    })
    return {'conversationId': conversationId,
            'userText': userTurn['userText'], 'reply': answer['reply'],
            'components': components, 'turnSeq': seq}


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


def _turnRow(row: dict) -> dict:
    """A ChatTurns row as getConversationTurns sends it: a turn saved
    before turns had components shows none, one saved with a component kind
    since retired shows the rest (chatForms.COMPONENT_KINDS), and one that
    answered no form has a null answeredFormId."""
    row = {'components': [], 'answeredFormId': None, **row}
    row['components'] = [c for c in row['components']
                         if c.get('kind') in chatForms.COMPONENT_KINDS]
    return row


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
                             message: str | None = None,
                             conversationId: str | None = None,
                             form: ChatFormAnswer | None = None,
                             attachments: list[str] | None = None,
                             turnKey: str | None = None) -> dict:
        """Send the admin's message, or a form's answer (`form`, in its
        conversation), to the store assistant at the store the admin owns,
        and return its reply with the cards and forms to show under it. No
        conversationId starts a new conversation. Before the store is set
        up the assistant only sets it up (chatForms.SETUP_TOOLSET).
        `attachments` are the Storage download URLs of up to 4 JPEGs, PNGs
        or PDFs (one PDF at most) the admin uploaded under
        users/<uid>/chat/ (the message may then be empty; never with a
        form). `turnKey`, a random id of the client's, lets getChatThinking
        tell what the assistant is doing while it answers."""
        store = requireAnyOwnerStore(userId, {'_id': 1, 'slug': 1})
        toolset = 'admin' if isSetUp(store) else chatForms.SETUP_TOOLSET
        return _chatTurn('admin', toolset, userId, store['_id'], message,
                         conversationId, form, attachments, turnKey)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Reply sent',
                  responseModel=ChatReplyResponse)
    def sendStoreChatMessage(self,
                             userId: str,
                             storeId: str,
                             message: str | None = None,
                             conversationId: str | None = None,
                             form: ChatFormAnswer | None = None,
                             attachments: list[str] | None = None,
                             turnKey: str | None = None) -> dict:
        """Send the customer's message, or a form's answer (`form`, in its
        conversation), to the shopping assistant of the store being
        visited, and return its reply with the cards and forms to show
        under it. No conversationId starts a new conversation.
        `attachments` are the Storage download URLs of up to 4 JPEGs, PNGs
        or PDFs (one PDF at most) the customer uploaded under
        users/<uid>/chat/ (the message may then be empty; never with a
        form). Capped per customer and per store each day, messages and
        files alike. `turnKey`, a random id of the client's, lets
        getChatThinking tell what the assistant is doing while it
        answers."""
        customerAtStore(userId, storeId)
        setUpStore(storeId, {'_id': 1})
        return _chatTurn('customer', 'customer', userId, storeId, message,
                         conversationId, form, attachments, turnKey)

    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Thinking fetched',
                  responseModel=ChatThinkingResponse)
    def getChatThinking(self, userId: str, turnKey: str) -> dict:
        """What the assistant is doing in the caller's own chat turn
        `turnKey` (the turnKey sent with the message), while it answers:
        the running tool's thinking text, or null before its first tool and
        once the turn is over."""
        thinking = chatThinking.current(userId, turnKey)
        return {'thinkingText': None if thinking is None
                else thinking['thinkingText']}

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
            store = requireAnyOwnerStore(userId, {'_id': 1})
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
        a turn without any) and replies, never its tool steps. `beforeSeq`
        pages back: only the turns before it."""
        conversationId = ownConversation(conversationId, userId,
                                         {'_id': 1})['_id']
        return {
            'turns': db.aggregateResponses(ChatTurnRow, [
                {'$match': {'conversationId': conversationId,
                            'userId': userId, **_validBeforeSeq(beforeSeq)}},
                {'$sort': {'seq': -1}},
                {'$limit': _validLimit(limit, CHAT_LIST_DEFAULT_LIMIT,
                                       CHAT_LIST_MAX_LIMIT)},
                {'$addFields': {'answeredFormId': '$formAnswer.formId'}},
                # A turn without files sends no attachments key, as one
                # stored before attachments.
                {'$set': {'attachments': {'$cond': [
                    {'$gt': [{'$size': {'$ifNull': ['$attachments', []]}},
                             0]},
                    '$attachments', '$$REMOVE']}}},
            ], CHAT_TURNS, readWhole=('components', ), prepare=_turnRow),
        }

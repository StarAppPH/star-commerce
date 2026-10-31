"""voiceModel.py — the store's voice: ElevenLabs' text to speech
(hand-written, no database).

- speak(): a text (spokenText) to MP3 bytes in the store's
  ElevenLabs voice, else ELEVENLABS_DEFAULT_VOICE_ID, with
  ELEVENLABS_API_KEY. A store voice ElevenLabs refuses (deleted on their
  side) is retried once with the default voice.
- listVoices() / voiceExists(): the voices a store may pick: ElevenLabs'
  default (premade) voices and those saved to the app's account, never a
  person's clone (only the premade and generated categories are offered).
- speechToken(): a single-use token the browser streams a reply with.

Every call is a plain HTTPS request (no SDK wraps the API) through one
requests.Session, to a fixed URL (only a voice id that passed
VOICE_ID_PATTERN goes into a path), with redirects off. Each attempt is
given min(its attempt seconds, the time left before the call's deadline),
the body read included, but requests applies it per socket operation: the
body read can take up to about twice that, and the headers are bounded only
by requests' per-read timeout, so a trickling server can hold an attempt
longer, up to Cloud Run's 300 s request timeout (_send);
_call() retries once, through chatModel's
callWithRetry (callModel's loop):
only a connection error, a timeout, a 429 or a 5xx, with RETRY_MIN_SECONDS
left after a RETRY_BACKOFF_SECONDS pause, never any other 4xx. A missing
key, a passed deadline or a failed call is unavailable(), a 503 ("Voice is
unavailable right now").

Nothing here logs audio, a reply, a voice's text or a key:
errors are logged by class and status only.

The clock: deadlines and body reads read this module's `time`, the retry
loop (attempts, the backoff sleep) chatModel's, so a test replaces both;
the HTTP layer is httpSession(), which a test replaces.
"""

import json
import logging
import os
import re
import threading
import time
import unicodedata
from contextlib import closing
from typing import Callable

import requests

import chatModel
from objects import (VOICE_DESCRIPTION_MAX_LENGTH, VOICE_ID_PATTERN,
                     VOICE_LABEL_KEY_MAX_LENGTH, VOICE_LABEL_VALUE_MAX_LENGTH,
                     VOICE_LABELS_MAX, VOICE_NAME_MAX_LENGTH,
                     VOICE_PAGE_TOKEN_MAX_LENGTH,
                     VOICE_PREVIEW_URL_MAX_LENGTH)
from services.__request import ServiceUnavailable

# ElevenLabs' text to speech and voices.
TTS_URL = 'https://api.elevenlabs.io/v1/text-to-speech/{voiceId}'
VOICES_URL = 'https://api.elevenlabs.io/v2/voices'
# A single-use token the browser streams one reply's speech with, straight
# from ElevenLabs (wss://api.elevenlabs.io/v1/text-to-speech/<voiceId>/
# stream-input): our key never leaves the server.
SPEECH_TOKEN_URL = ('https://api.elevenlabs.io/v1/single-use-token/'
                    'tts_websocket')
SPEECH_TOKEN_MAX_LENGTH = 2048
TTS_MODEL = 'eleven_flash_v2_5'
TTS_OUTPUT_FORMAT = 'mp3_44100_64'
TTS_CONTENT_TYPE = 'audio/mpeg'
ELEVENLABS_KEY_VARIABLE = 'ELEVENLABS_API_KEY'
# The voice of a store that picked none, and the fallback of a store voice
# ElevenLabs refuses.
DEFAULT_VOICE_VARIABLE = 'ELEVENLABS_DEFAULT_VOICE_ID'
# A store voice refused with one of these is retried with the default
# voice (a 401 is the key, a 429 is retried as itself).
VOICE_FALLBACK_STATUSES = (400, 403, 404, 422)
# The voices offered: two listings (voice_type takes one value), paged in
# turn, and only voices of these categories (a cloned, professional or
# famous voice is a person's).
VOICE_TYPES = ('default', 'saved')
OFFERED_CATEGORIES = ('premade', 'generated')
VOICES_PAGE_SIZE = 20
# What routeErrors' 503 names: "Voice is unavailable right now".
VOICE_SERVICE = 'voice'

# The most of a reply that is spoken (cut at a sentence end): the chat
# keeps the whole text.
SPOKEN_REPLY_MAX_CHARS = 2500

# Each call's own deadline, and the most one attempt waits.
TTS_DEADLINE_SECONDS = TTS_ATTEMPT_SECONDS = 15
VOICES_DEADLINE_SECONDS = 15
SPEECH_TOKEN_DEADLINE_SECONDS = SPEECH_TOKEN_ATTEMPT_SECONDS = 5
SPEECH_TOKEN_RESPONSE_MAX_BYTES = 16 * 1024
VOICES_ATTEMPT_SECONDS = 10
# The largest response body read: past it, the call failed.
TTS_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
VOICES_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

PAGE_TOKEN_INVALID = ('pageToken is not a page of listVoices: send '
                      'nextPageToken back as it came')

_session = None
_sessionLock = threading.Lock()


class _Refused(Exception):
    """The API answered a status that is never retried (a 4xx other than
    429, or a 3xx: redirects are off)."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(status)


class _Busy(Exception):
    """The API answered a 429 or a 5xx: retried once."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(status)


class _BadBody(Exception):
    """A 2xx whose body is too large, or not what the API documents."""


def unavailable() -> ServiceUnavailable:
    """The 503 of every voice failure: "Voice is unavailable right now"
    (routeErrors)."""
    return ServiceUnavailable(VOICE_SERVICE, 'voice')


def httpSession() -> requests.Session:
    """The one Session of every voice call, built once (gunicorn runs
    threads, hence the lock). trust_env is off: a .netrc must never replace
    the key headers, and no proxy variable applies."""
    global _session
    with _sessionLock:
        if _session is None:
            session = requests.Session()
            session.trust_env = False
            _session = session
        return _session


def _key(variable: str) -> str:
    """The API key in `variable`, or unavailable() (a 503) without it, or
    with one that cannot go into a header as is: not ASCII, or holding a
    space or a control character (logged, never quoted)."""
    key = (os.getenv(variable) or '').strip()
    if not key:
        raise unavailable()
    if not key.isascii() or not key.isprintable() or ' ' in key:
        logging.error({'event': 'voiceKeyInvalid', 'variable': variable})
        raise unavailable()
    return key


def validVoiceId(voiceId) -> bool:
    """Whether `voiceId` is text that can be an ElevenLabs voice id
    (VOICE_ID_PATTERN), so it may go into a URL path."""
    return (isinstance(voiceId, str)
            and re.fullmatch(VOICE_ID_PATTERN, voiceId) is not None)


def defaultVoiceId() -> str:
    """ELEVENLABS_DEFAULT_VOICE_ID: unset or no voice id, unavailable() (a
    bad one logged, never quoted)."""
    voiceId = (os.getenv(DEFAULT_VOICE_VARIABLE) or '').strip()
    if not validVoiceId(voiceId):
        if voiceId:
            logging.warning({'event': 'voiceDefaultInvalid'})
        raise unavailable()
    return voiceId


# ── HTTP ────────────────────────────────────

def _readBody(response, attemptEnds: float, maxBytes: int) -> bytes:
    """The response body, at most `maxBytes` (_BadBody past it), read
    until `attemptEnds`: a chunk that arrives once the time is up is a
    timeout (retried as one), but a body whose last chunk lands just after
    it is kept whole. The time is checked only as each chunk arrives, and
    each socket read waits up to requests' per-read timeout: a chunk just
    before `attemptEnds` followed by a stall can run up to one more
    timeout past it."""
    chunks, size, late = [], 0, False
    for chunk in response.iter_content(READ_CHUNK_BYTES):
        if late:
            raise requests.Timeout()
        size += len(chunk)
        if size > maxBytes:
            raise _BadBody()
        chunks.append(chunk)
        late = time.monotonic() > attemptEnds
    return b''.join(chunks)


def _send(method: str, url: str, timeout: float, maxBytes: int,
          **kwargs) -> tuple[dict, bytes]:
    """One request: its headers and body on a 2xx; _Busy on a 429 or a
    5xx; _Refused on any other status. requests' `timeout` applies to each
    socket operation (the connect, each read), not to the whole. The body
    read (_readBody) can take up to about twice `timeout` (it overruns
    `attemptEnds` by at most one more read); the headers are bounded only
    by requests' per-read timeout, so a trickling server can hold an
    attempt longer, up to Cloud Run's 300 s request timeout."""
    attemptEnds = time.monotonic() + timeout
    response = httpSession().request(method, url, timeout=timeout,
                                     allow_redirects=False, stream=True,
                                     **kwargs)
    with closing(response):
        status = response.status_code
        if status == 429 or status >= 500:
            raise _Busy(status)
        if not 200 <= status < 300:
            raise _Refused(status)
        return response.headers, _readBody(response, attemptEnds, maxBytes)


def _handle(error: BaseException, event: str) -> bool:
    """Log a failed attempt by its class and status only (chatModel's
    errorLog: a requests error's text holds the URL): a refused status is
    ours to fix, so an ERROR. A _Refused (and a ServiceUnavailable) is
    raised as itself, for the caller to map; otherwise returns whether it
    may be retried: a connection error, a timeout, a 429 or a 5xx."""
    if isinstance(error, ServiceUnavailable):
        raise error
    logging.log(logging.ERROR if isinstance(error, _Refused)
                else logging.WARNING,
                chatModel.errorLog(event, error,
                                   getattr(error, 'status', None)))
    if isinstance(error, _Refused):
        raise error
    return isinstance(error, (requests.ConnectionError, requests.Timeout,
                              _Busy))


def _call(request: Callable[[float], tuple[dict, bytes]], deadline: float,
          attemptSeconds: float, logEvent: str) -> tuple[dict, bytes]:
    """`request(timeout)` (one _send), done before `deadline`
    (time.monotonic()), retried as chatModel.callWithRetry does (its clock
    is chatModel's `time`): once, only a connection error, a timeout, a 429
    or a 5xx. A _Refused is raised as itself; any other failure, and a
    passed deadline, as unavailable(). Every failure is logged
    (logEvent)."""
    return chatModel.callWithRetry(
        request, deadline, attemptSeconds, unavailable,
        (requests.RequestException, _Refused, _Busy, _BadBody),
        lambda error: _handle(error, logEvent))


def _json(body: bytes, logEvent: str) -> dict:
    """A 2xx body as the JSON object the API documents, else unavailable()
    (logged, never quoted)."""
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        logging.warning({'event': logEvent, 'errorClass': 'BadBody'})
        raise unavailable()
    return data


# ── Text to speech ──────────────────────────

# A fenced code block, dropped whole (an unclosed one to the end).
_FENCE = re.compile(r'^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)',
                    re.M | re.S)
_RULE = re.compile(r'^[ \t]*(?:[-*_][ \t]*){3,}$', re.M)
_TABLE_RULE = re.compile(r'^[ \t]*\|?[ \t]*:?-{3,}[-:| \t]*$', re.M)
_HEADING = re.compile(r'^[ \t]*#{1,6}[ \t]+', re.M)
_QUOTE = re.compile(r'^[ \t]*>[ \t]?', re.M)
_BULLET = re.compile(r'^[ \t]*[-*+•][ \t]+', re.M)
_IMAGE = re.compile(r'!\[([^\]]*)\]\([^)]*\)')
_LINK = re.compile(r'\[([^\]]+)\]\([^)]*\)')
_URL = re.compile(r'(?:\bhttps?://|\bwww\.)\S+', re.I)
_INLINE_CODE = re.compile(r'`+([^`]*)`+')
_BOLD = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1', re.S)
_ITALIC = re.compile(r'(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])'
                     r'|(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])')
_PIPE = re.compile(r'[ \t]*\|[ \t]*')
_SENTENCE_END = re.compile(r'[.!?…](?=\s|$)')
_ENDS_A_CLAUSE = ('.', '!', '?', '…', ':', ';', ',')


def spokenText(reply: str) -> str:
    """`reply` as it is spoken: its markdown removed (fenced code blocks
    dropped whole; rules, headings, quotes, bullets, bold, italics, inline
    code marks; a link becomes its text, a bare URL the word "link", a
    table's pipes pauses), each line ended as a sentence, whitespace
    collapsed, and cut within SPOKEN_REPLY_MAX_CHARS: at the last sentence
    end past half of it, else at the last space."""
    text = reply or ''
    for pattern in (_FENCE, _RULE, _TABLE_RULE, _HEADING, _QUOTE, _BULLET):
        text = pattern.sub('', text)
    text = _IMAGE.sub(r'\1', text)
    text = _LINK.sub(r'\1', text)
    text = _URL.sub('link', text)
    text = _INLINE_CODE.sub(r'\1', text)
    text = _BOLD.sub(r'\2', text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    lines = []
    for line in text.splitlines():
        line = _PIPE.sub(', ', line).strip(' \t,')
        line = ' '.join(line.split())
        if line:
            lines.append(line if line.endswith(_ENDS_A_CLAUSE)
                         else f'{line}.')
    text = ' '.join(lines)
    if len(text) <= SPOKEN_REPLY_MAX_CHARS:
        return text
    ends = [match.end() for match in _SENTENCE_END.finditer(text)
            if SPOKEN_REPLY_MAX_CHARS // 2 < match.end()
            <= SPOKEN_REPLY_MAX_CHARS]
    if ends:
        return text[:ends[-1]]
    window = text[:SPOKEN_REPLY_MAX_CHARS]
    space = window.rfind(' ')
    return (window[:space] if space > 0 else window).rstrip()


def _speakWith(key: str, voiceId: str, text: str, deadline: float) -> bytes:
    """`text` spoken in the voice `voiceId` (a valid id), as MP3 bytes.
    _Refused when ElevenLabs refuses it; an answer that is no MP3, or an
    empty one, is unavailable()."""

    def request(timeout: float):
        return _send('POST', TTS_URL.format(voiceId=voiceId), timeout,
                     TTS_RESPONSE_MAX_BYTES,
                     params={'output_format': TTS_OUTPUT_FORMAT},
                     headers={'xi-api-key': key, 'Accept': TTS_CONTENT_TYPE},
                     json={'text': text, 'model_id': TTS_MODEL})

    headers, body = _call(request, deadline, TTS_ATTEMPT_SECONDS,
                          'voiceSpeakError')
    contentType = (headers.get('Content-Type') or '').split(';')[0]
    if contentType.strip().lower() != TTS_CONTENT_TYPE or not body:
        logging.warning({'event': 'voiceSpeakError', 'errorClass': 'BadBody'})
        raise unavailable()
    return body


def speak(text: str, voiceId: str | None, deadline: float) -> bytes:
    """`text` (spokenText) spoken as MP3 bytes, done before `deadline`
    (time.monotonic()), in the voice `voiceId` (the store's; None, or no
    voice id, is the default voice). A store voice ElevenLabs refuses
    (VOICE_FALLBACK_STATUSES: deleted on their side) is tried once more in
    the default voice (logged: voiceFallback). Any other failure is
    unavailable()."""
    key = _key(ELEVENLABS_KEY_VARIABLE)
    voice = voiceId if validVoiceId(voiceId) else defaultVoiceId()
    try:
        return _speakWith(key, voice, text, deadline)
    except _Refused as refused:
        if refused.status not in VOICE_FALLBACK_STATUSES:
            raise unavailable() from None
        default = defaultVoiceId()
        if voice == default:
            raise unavailable() from None
        logging.warning({'event': 'voiceFallback', 'status': refused.status})
    try:
        return _speakWith(key, default, text, deadline)
    except _Refused:
        raise unavailable() from None


def speechToken() -> str:
    """A single-use ElevenLabs token for one text-to-speech stream (it
    expires after 15 minutes), made with our key. Any failure is
    unavailable(); the token is never logged."""
    key = _key(ELEVENLABS_KEY_VARIABLE)

    def request(timeout: float):
        return _send('POST', SPEECH_TOKEN_URL, timeout,
                     SPEECH_TOKEN_RESPONSE_MAX_BYTES,
                     headers={'xi-api-key': key})

    try:
        _, body = _call(request,
                        time.monotonic() + SPEECH_TOKEN_DEADLINE_SECONDS,
                        SPEECH_TOKEN_ATTEMPT_SECONDS, 'voiceTokenError')
    except _Refused:
        raise unavailable() from None
    token = _json(body, 'voiceTokenError').get('token')
    if (not isinstance(token, str) or not token
            or len(token) > SPEECH_TOKEN_MAX_LENGTH or not token.isascii()
            or not token.isprintable() or ' ' in token):
        logging.warning({'event': 'voiceTokenError', 'errorClass': 'BadBody'})
        raise unavailable()
    return token


# ── Voices ──────────────────────────────────

def _cleanText(value, maxLength: int) -> str | None:
    """Third-party text as sent on: control and format characters dropped,
    whitespace collapsed, cut to `maxLength`; None when it is no text or
    nothing is left."""
    if not isinstance(value, str):
        return None
    kept = ''.join(c for c in value
                   if unicodedata.category(c) not in ('Cc', 'Cf'))
    text = ' '.join(kept.split())[:maxLength].rstrip()
    return text or None


def _previewUrl(value) -> str | None:
    """An https sample URL, at most VOICE_PREVIEW_URL_MAX_LENGTH, with no
    whitespace or control character; else None."""
    if (not isinstance(value, str) or not value.startswith('https://')
            or len(value) > VOICE_PREVIEW_URL_MAX_LENGTH
            or any(c.isspace() or unicodedata.category(c) in ('Cc', 'Cf')
                   for c in value)):
        return None
    return value


def _voiceRow(voice) -> dict | None:
    """ElevenLabs' voice as a VoiceRow, or None when it is not offered (no
    valid id or name, or a category of OFFERED_CATEGORIES it is not)."""
    if not isinstance(voice, dict):
        return None
    voiceId, name = voice.get('voice_id'), _cleanText(voice.get('name'),
                                                     VOICE_NAME_MAX_LENGTH)
    if (not validVoiceId(voiceId) or name is None
            or voice.get('category') not in OFFERED_CATEGORIES):
        return None
    labels = {}
    rawLabels = voice.get('labels')
    for label, value in (rawLabels.items()
                         if isinstance(rawLabels, dict) else ()):
        label = _cleanText(label, VOICE_LABEL_KEY_MAX_LENGTH)
        value = _cleanText(value, VOICE_LABEL_VALUE_MAX_LENGTH)
        if label is not None and value is not None:
            labels[label] = value
        if len(labels) == VOICE_LABELS_MAX:
            break
    return {'voiceId': voiceId,
            'name': name,
            'description': _cleanText(voice.get('description'),
                                      VOICE_DESCRIPTION_MAX_LENGTH),
            'labels': labels,
            'previewUrl': _previewUrl(voice.get('preview_url'))}


def _voicesPage(params: dict, deadline: float) -> dict:
    """One GET /v2/voices with `params`, done before `deadline`, as its JSON
    object."""
    key = _key(ELEVENLABS_KEY_VARIABLE)

    def request(timeout: float):
        return _send('GET', VOICES_URL, timeout, VOICES_RESPONSE_MAX_BYTES,
                     params={**params, 'include_total_count': 'false'},
                     headers={'xi-api-key': key,
                              'Accept': 'application/json'})

    try:
        _, body = _call(request, deadline, VOICES_ATTEMPT_SECONDS,
                        'voiceListError')
    except _Refused:
        raise unavailable() from None
    return _json(body, 'voiceListError')


def _offeredRows(data: dict) -> list[dict]:
    """The voices of a /v2/voices page that a store may pick, as VoiceRow
    dicts (_voiceRow)."""
    rawVoices = data.get('voices')
    return [row for row in map(_voiceRow, rawVoices
                               if isinstance(rawVoices, list) else [])
            if row is not None]


def _pageOf(pageToken: str | None) -> tuple[int, str | None]:
    """The listing (VOICE_TYPES index) and ElevenLabs' page token of our
    pageToken "<index>:<their token>" (None: the first page)."""
    if pageToken is None:
        return 0, None
    match = re.fullmatch(r'([0-9]):([!-~]*)', pageToken)
    if match is None or int(match.group(1)) >= len(VOICE_TYPES):
        raise ValueError(PAGE_TOKEN_INVALID)
    return int(match.group(1)), match.group(2) or None


def listVoices(search: str | None,
               pageToken: str | None) -> tuple[list[dict], str | None]:
    """A page of the voices a store may pick (VoiceRow dicts), matching
    `search` when sent, and the next page's token (None on the last). The
    default voices are paged first, then the saved ones; a page may hold
    fewer than VOICES_PAGE_SIZE (voices not offered are dropped)."""
    index, theirToken = _pageOf(pageToken)
    params = {'voice_type': VOICE_TYPES[index], 'page_size': VOICES_PAGE_SIZE}
    if search is not None:
        params['search'] = search
    if theirToken is not None:
        params['next_page_token'] = theirToken
    data = _voicesPage(params, time.monotonic() + VOICES_DEADLINE_SECONDS)
    voices = _offeredRows(data)
    if data.get('has_more') is True:
        nextToken = data.get('next_page_token')
        if (isinstance(nextToken, str)
                and re.fullmatch(r'[!-~]+', nextToken) is not None
                and len(nextToken) + 2 <= VOICE_PAGE_TOKEN_MAX_LENGTH):
            return voices, f'{index}:{nextToken}'
        # The rest of this listing cannot be paged to: go on to the next.
        logging.warning({'event': 'voiceListTokenDropped',
                         'voiceType': VOICE_TYPES[index]})
    if index + 1 < len(VOICE_TYPES):
        return voices, f'{index + 1}:'
    return voices, None


def voiceExists(voiceId) -> bool:
    """Whether `voiceId` is a voice a store may pick (as listVoices offers
    them), looked up by id (voice_ids=) in each listing, within one
    deadline. No voice id is False, with no call."""
    if not validVoiceId(voiceId):
        return False
    deadline = time.monotonic() + VOICES_DEADLINE_SECONDS
    for voiceType in VOICE_TYPES:
        data = _voicesPage({'voice_type': voiceType, 'voice_ids': voiceId,
                            'page_size': VOICES_PAGE_SIZE}, deadline)
        if any(row['voiceId'] == voiceId for row in _offeredRows(data)):
            return True
    return False

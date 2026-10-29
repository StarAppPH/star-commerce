"""voiceModel.py — voice chat's speech: Meta's Muse Voice Transcribe in,
ElevenLabs' text to speech out (hand-written, no database).

- transcribe(): a WAV recording (validWav) to its text, through
  POST /v1/asr/transcribe with MODEL_API_KEY (chat's key).
- speak(): a reply's text (spokenText) to MP3 bytes in the store's
  ElevenLabs voice, else ELEVENLABS_DEFAULT_VOICE_ID, with
  ELEVENLABS_API_KEY. A store voice ElevenLabs refuses (deleted on their
  side) is retried once with the default voice.
- listVoices() / voiceExists(): the voices a store may pick: ElevenLabs'
  default (premade) voices and those saved to the app's account, never a
  person's clone (only the premade and generated categories are offered).

Every call is a plain HTTPS request (no SDK wraps either API) through one
requests.Session, to a fixed URL (only a voice id that passed
VOICE_ID_PATTERN goes into a path), with redirects off. Each attempt waits
at most min(its attempt seconds, the time left before the call's deadline),
the body read included; _call() retries once, as chatModel.callModel does:
only a connection error, a timeout, a 429 or a 5xx, with RETRY_MIN_SECONDS
left after a RETRY_BACKOFF_SECONDS pause, never any other 4xx. A missing
key, a passed deadline or a failed call is unavailable(), a 503 ("Voice is
unavailable right now").

Nothing here logs audio, a transcript, a reply, a voice's text or a key:
errors are logged by class and status only.

The clock: every deadline reads this module's `time`, so a test replaces
voiceModel.time; the HTTP layer is httpSession(), which a test replaces.
"""

import base64
import binascii
import json
import logging
import math
import os
import re
import struct
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

# Meta's speech to text (MODEL_API_KEY, as chat).
STT_URL = 'https://api.meta.ai/v1/asr/transcribe'
STT_MODEL = 'muse-voice-transcribe-1.0'
STT_MODE = 'PUSH_TO_TALK'
STT_AUDIO_ENCODING = 'WAV'
META_KEY_VARIABLE = chatModel.API_KEY_VARIABLE
# ElevenLabs' text to speech and voices.
TTS_URL = 'https://api.elevenlabs.io/v1/text-to-speech/{voiceId}'
VOICES_URL = 'https://api.elevenlabs.io/v2/voices'
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

# The recording a voice route takes: RIFF/WAVE, mono, 16-bit PCM, 16 or 24
# kHz, 0.3 to 60 seconds (Meta takes up to 10 minutes).
WAV_SAMPLE_RATES = (16000, 24000)
WAV_PCM_FORMAT = 1
WAV_BYTES_PER_SAMPLE = 2
VOICE_AUDIO_MIN_SECONDS = 0.3
VOICE_AUDIO_MAX_SECONDS = 60
# The header chunks a recorder may write besides the samples.
WAV_HEADER_ALLOWANCE = 4096
VOICE_AUDIO_MAX_BYTES = (VOICE_AUDIO_MAX_SECONDS * max(WAV_SAMPLE_RATES) *
                         WAV_BYTES_PER_SAMPLE + WAV_HEADER_ALLOWANCE)
VOICE_AUDIO_MAX_BASE64 = 4 * math.ceil(VOICE_AUDIO_MAX_BYTES / 3)
# The most of a reply that is spoken (cut at a sentence end): the chat
# keeps the whole text.
SPOKEN_REPLY_MAX_CHARS = 2500

# Each call's own deadline, and the most one attempt waits.
STT_DEADLINE_SECONDS = STT_ATTEMPT_SECONDS = 15
TTS_DEADLINE_SECONDS = TTS_ATTEMPT_SECONDS = 15
VOICES_DEADLINE_SECONDS = 15
VOICES_ATTEMPT_SECONDS = 10
# The largest response body read: past it, the call failed.
STT_RESPONSE_MAX_BYTES = 256 * 1024
TTS_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
VOICES_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

AUDIO_NOT_BASE64 = 'audio must be a WAV recording, base64 encoded'
AUDIO_NOT_WAV = 'audio is not a WAV recording: record or convert it to WAV'
AUDIO_FORMAT = ('audio must be mono, 16-bit PCM, at 16 or 24 kHz: convert '
                'the recording')
AUDIO_TOO_SHORT = 'audio is too short: hold to talk a little longer'
AUDIO_TOO_LONG = (f'audio is too long: keep a message under '
                  f'{VOICE_AUDIO_MAX_SECONDS} seconds')
NOTHING_HEARD = "I couldn't hear anything: try again closer to the mic"
RECORDING_UNREADABLE = ("The recording couldn't be read: send a mono 16-bit "
                        'WAV at 16 or 24 kHz')
PAGE_TOKEN_INVALID = ('pageToken is not a page of listVoices: send '
                      'nextPageToken back as it came')

_session = None
_sessionLock = threading.Lock()


class NothingHeard(ValueError):
    """Meta transcribed the recording to nothing (a 400: NOTHING_HEARD).
    Meta billed it."""

    def __init__(self):
        super().__init__(NOTHING_HEARD)


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
    """The API key in `variable`, or unavailable() (a 503) without it."""
    key = (os.getenv(variable) or '').strip()
    if not key:
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
    """The response body, read before `attemptEnds` (a slow trickle is a
    timeout, retried as one) and at most `maxBytes` (_BadBody past it)."""
    chunks, size = [], 0
    for chunk in response.iter_content(READ_CHUNK_BYTES):
        size += len(chunk)
        if size > maxBytes:
            raise _BadBody()
        if time.monotonic() > attemptEnds:
            raise requests.Timeout()
        chunks.append(chunk)
    return b''.join(chunks)


def _send(method: str, url: str, timeout: float, maxBytes: int,
          **kwargs) -> tuple[dict, bytes]:
    """One request, waiting at most `timeout` seconds in all: its headers
    and body on a 2xx; _Busy on a 429 or a 5xx; _Refused on any other
    status."""
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


def _logError(error: Exception, event: str) -> None:
    """Log a failed attempt by its class and status only (a requests error's
    text holds the URL, a body may quote the request): a refused status is
    ours to fix, so an ERROR."""
    status = getattr(error, 'status', None)
    logging.log(logging.ERROR if isinstance(error, _Refused)
                else logging.WARNING, {
                    'event': event,
                    'errorClass': type(error).__name__,
                    'status': status,
                })


def _call(request: Callable[[float], tuple[dict, bytes]], deadline: float,
          attemptSeconds: float, logEvent: str) -> tuple[dict, bytes]:
    """`request(timeout)` (one _send), done before `deadline`
    (time.monotonic()): each attempt waits at most min(attemptSeconds, the
    time left). A connection error, a timeout, a 429 or a 5xx is retried
    once, with at least RETRY_MIN_SECONDS left after a
    RETRY_BACKOFF_SECONDS pause (chatModel's); anything else never is. A
    _Refused is raised as itself, for the caller to map; any other failure,
    and a passed deadline, as unavailable(). Every failure is logged
    (logEvent)."""
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise unavailable()
        try:
            return request(min(attemptSeconds, remaining))
        except ServiceUnavailable:
            raise
        except (requests.RequestException, _Refused, _Busy,
                _BadBody) as error:
            _logError(error, logEvent)
            if isinstance(error, _Refused):
                raise
            retryable = isinstance(error, (requests.ConnectionError,
                                           requests.Timeout, _Busy))
            if (attempt == 0 and retryable
                    and deadline - time.monotonic() >=
                    chatModel.RETRY_BACKOFF_SECONDS +
                    chatModel.RETRY_MIN_SECONDS):
                time.sleep(chatModel.RETRY_BACKOFF_SECONDS)
                continue
            raise unavailable() from None
    raise unavailable()


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


# ── Speech to text ──────────────────────────

def validWav(audio) -> tuple[bytes, float]:
    """The recording a voice route was sent, decoded, and its length in
    seconds: base64 (strict) of a RIFF/WAVE file of mono 16-bit PCM at 16
    or 24 kHz, VOICE_AUDIO_MIN_SECONDS to VOICE_AUDIO_MAX_SECONDS long, at
    most VOICE_AUDIO_MAX_BYTES (the base64 length is checked before
    decoding). Its chunks are walked, bounds checked, to `fmt ` and `data`.
    Every refusal names `audio` and the fix, never quoting it."""
    if not isinstance(audio, str) or not audio:
        raise ValueError(AUDIO_NOT_BASE64)
    if len(audio) > VOICE_AUDIO_MAX_BASE64:
        raise ValueError(AUDIO_TOO_LONG)
    try:
        wav = base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(AUDIO_NOT_BASE64) from None
    if len(wav) > VOICE_AUDIO_MAX_BYTES:
        raise ValueError(AUDIO_TOO_LONG)
    if len(wav) < 12 or wav[:4] != b'RIFF' or wav[8:12] != b'WAVE':
        raise ValueError(AUDIO_NOT_WAV)
    fmt, offset = None, 12
    while offset + 8 <= len(wav):
        chunkId = wav[offset:offset + 4]
        (size,) = struct.unpack_from('<I', wav, offset + 4)
        start = offset + 8
        end = start + size
        if end > len(wav) or (chunkId == b'data' and fmt is None):
            raise ValueError(AUDIO_NOT_WAV)
        if chunkId == b'data':
            dataSize = size
            break
        if chunkId == b'fmt ':
            if size < 16:
                raise ValueError(AUDIO_NOT_WAV)
            fmt = struct.unpack_from('<HHIIHH', wav, start)
        offset = end + (size & 1)  # a chunk is padded to an even size
    else:
        raise ValueError(AUDIO_NOT_WAV)
    audioFormat, channels, rate, byteRate, blockAlign, bits = fmt
    if (audioFormat != WAV_PCM_FORMAT or channels != 1
            or rate not in WAV_SAMPLE_RATES or bits != 16
            or blockAlign != WAV_BYTES_PER_SAMPLE
            or byteRate != rate * WAV_BYTES_PER_SAMPLE):
        raise ValueError(AUDIO_FORMAT)
    seconds = dataSize / (rate * WAV_BYTES_PER_SAMPLE)
    if seconds < VOICE_AUDIO_MIN_SECONDS:
        raise ValueError(AUDIO_TOO_SHORT)
    if seconds > VOICE_AUDIO_MAX_SECONDS:
        raise ValueError(AUDIO_TOO_LONG)
    return wav, seconds


def transcribe(wav: bytes, deadline: float) -> str:
    """What the recording `wav` (validWav) says, stripped, done before
    `deadline` (time.monotonic()). Nothing heard is NothingHeard (a 400);
    a recording Meta refuses (its 400) is RECORDING_UNREADABLE (a 400); any
    other failure is unavailable()."""
    key = _key(META_KEY_VARIABLE)
    settings = json.dumps({'model': STT_MODEL, 'mode': STT_MODE,
                           'audioEncoding': STT_AUDIO_ENCODING})

    def request(timeout: float):
        return _send('POST', STT_URL, timeout, STT_RESPONSE_MAX_BYTES,
                     headers={'Authorization': f'Bearer {key}',
                              'Accept': 'application/json'},
                     files={'request': (None, settings, 'application/json'),
                            'audio': ('audio.wav', wav, 'audio/wav')})

    try:
        _, body = _call(request, deadline, STT_ATTEMPT_SECONDS,
                        'voiceTranscribeError')
    except _Refused as refused:
        if refused.status == 400:
            raise ValueError(RECORDING_UNREADABLE) from None
        raise unavailable() from None
    transcript = _json(body, 'voiceTranscribeError').get('transcript')
    if not isinstance(transcript, str):
        logging.warning({'event': 'voiceTranscribeError',
                         'errorClass': 'BadBody'})
        raise unavailable()
    transcript = transcript.strip()
    if not transcript:
        raise NothingHeard()
    return transcript


# ── Text to speech ──────────────────────────

_FENCE = re.compile(r'^[ \t]*(?:```|~~~).*$', re.M)
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
    """`reply` as it is spoken: its markdown removed (code fences, rules,
    headings, quotes, bullets, bold, italics, inline code; a link becomes
    its text, a bare URL the word "link", a table's pipes pauses), each
    line ended as a sentence, whitespace collapsed, and cut at the last
    sentence end within SPOKEN_REPLY_MAX_CHARS (else the last space)."""
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
            if match.end() <= SPOKEN_REPLY_MAX_CHARS]
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


def _voicesPage(params: dict) -> dict:
    """One GET /v2/voices with `params`, as its JSON object."""
    key = _key(ELEVENLABS_KEY_VARIABLE)

    def request(timeout: float):
        return _send('GET', VOICES_URL, timeout, VOICES_RESPONSE_MAX_BYTES,
                     params={**params, 'include_total_count': 'false'},
                     headers={'xi-api-key': key,
                              'Accept': 'application/json'})

    try:
        _, body = _call(request,
                        time.monotonic() + VOICES_DEADLINE_SECONDS,
                        VOICES_ATTEMPT_SECONDS, 'voiceListError')
    except _Refused:
        raise unavailable() from None
    return _json(body, 'voiceListError')


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
    data = _voicesPage(params)
    rawVoices = data.get('voices')
    voices = [row for row in map(_voiceRow, rawVoices
                                 if isinstance(rawVoices, list) else [])
              if row is not None]
    nextToken = data.get('next_page_token')
    if (data.get('has_more') is True and isinstance(nextToken, str)
            and nextToken
            and re.fullmatch(r'[!-~]+', nextToken) is not None
            and len(nextToken) + 2 <= VOICE_PAGE_TOKEN_MAX_LENGTH):
        return voices, f'{index}:{nextToken}'
    if index + 1 < len(VOICE_TYPES):
        return voices, f'{index + 1}:'
    return voices, None


def voiceExists(voiceId) -> bool:
    """Whether `voiceId` is a voice a store may pick (as listVoices offers
    them), looked up by id (voice_ids=) in each listing. No voice id is
    False, with no call."""
    if not validVoiceId(voiceId):
        return False
    for voiceType in VOICE_TYPES:
        data = _voicesPage({'voice_type': voiceType, 'voice_ids': voiceId,
                            'page_size': VOICES_PAGE_SIZE})
        rawVoices = data.get('voices')
        for voice in rawVoices if isinstance(rawVoices, list) else []:
            row = _voiceRow(voice)
            if row is not None and row['voiceId'] == voiceId:
                return True
    return False

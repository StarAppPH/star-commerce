"""samModel.py — finding an item in a photo with Meta's SAM 3.1 (Segment
Anything), through the Model API's Responses endpoint (hand-written, no
database).

segment() sends one photo and one short noun phrase ("white paper cup")
to POST /v1/responses (model sam-3.1, store false) through chatModel's
client (MODEL_API_KEY), and returns every match's box. SAM takes one
concept per request: a phrase listing several things finds nothing.

SAM answers in output_text, one record per match, e.g.
    <0f>0<|box;x1=71;y1=111;x2=169;y2=238;w=640;h=400|><|mask;...|>,1<|box;...|>
<0f> is the frame (always 0 for a photo), then each object's id, its box
(inclusive pixel corners, in an image of w x h pixels) and its mask, the
objects separated by commas. A box may carry c=<confidence>. Only the
boxes are read: the masks' encoding is undocumented. No match is an empty
output_text; text that has no box record in it is a 503 (logged by
length only), never a guess.

It retries as chatModel.callModel does. Any SDK error, a missing key or a
passed deadline is unavailable(), a 503 ("Product finding is unavailable
right now"); errors are logged by class and status only, never the photo
or the phrase.
"""

import logging
import re
from dataclasses import dataclass

import chatModel
from services.__request import ServiceUnavailable

SAM_MODEL = 'sam-3.1'
# The most one attempt waits, cut to the time the call has left.
SAM_TIMEOUT_SECONDS = 30
# What routeErrors' 503 names: "Product finding is unavailable right now".
SAM_SERVICE = 'product finding'
_RECORD = re.compile(r'<\|box;([^|]*)\|>')
_BOX_KEYS = ('x1', 'y1', 'x2', 'y2', 'w', 'h')


@dataclass(frozen=True)
class Box:
    """A match's box in pixels of the image SAM saw (width x height):
    left and top inclusive, right and bottom exclusive."""
    left: int
    top: int
    right: int
    bottom: int
    width: int
    height: int
    confidence: float | None = None

    @property
    def area(self) -> int:
        return (self.right - self.left) * (self.bottom - self.top)


def unavailable() -> ServiceUnavailable:
    """The 503 of every failed SAM call."""
    return ServiceUnavailable(SAM_SERVICE, 'photos')


def _client():
    try:
        return chatModel.client()
    except ServiceUnavailable:
        raise unavailable() from None


def _box(fields: str) -> Box | None:
    """One box record's fields (key=value;...), or None when a corner or
    the image size is missing, not a number, or makes an empty box."""
    values = {}
    for pair in fields.split(';'):
        key, _, value = pair.partition('=')
        values[key.strip()] = value.strip()
    try:
        x1, y1, x2, y2, width, height = (int(values[key])
                                         for key in _BOX_KEYS)
        confidence = float(values['c']) if 'c' in values else None
    except (KeyError, ValueError):
        return None
    # The wire's corners are inclusive: right and bottom become exclusive.
    left, top = max(0, x1), max(0, y1)
    right, bottom = min(width, x2 + 1), min(height, y2 + 1)
    if width <= 0 or height <= 0 or right <= left or bottom <= top:
        return None
    return Box(left, top, right, bottom, width, height, confidence)


def parseBoxes(text: str) -> list[Box]:
    """The boxes of SAM's output_text, in its order: [] for no match (an
    empty text). Text with no box record is unavailable()."""
    if not text or not text.strip():
        return []
    records = _RECORD.findall(text)
    boxes = [box for box in map(_box, records) if box is not None]
    if not boxes:
        logging.warning({'event': 'samUnreadable', 'length': len(text),
                         'records': len(records)})
        raise unavailable()
    return boxes


def _outputText(response) -> str:
    """The response's text: the SDK's output_text, else its output_text
    parts joined."""
    text = getattr(response, 'output_text', None)
    if text:
        return text
    parts = []
    for item in getattr(response, 'output', None) or []:
        for block in getattr(item, 'content', None) or []:
            if getattr(block, 'type', '') == 'output_text':
                parts.append(getattr(block, 'text', '') or '')
    return ''.join(parts)


def segment(imageDataUrl: str, phrase: str, deadline: float) -> list[Box]:
    """Every match of `phrase` in the photo (a data URL), done before
    `deadline` (time.monotonic()), each attempt within
    SAM_TIMEOUT_SECONDS."""

    def request(timeout: float):
        return _client().responses.create(
            model=SAM_MODEL,
            store=False,
            input=[{'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': phrase},
                {'type': 'input_image', 'image_url': imageDataUrl}]}],
            timeout=timeout)

    response = chatModel.callModel(request, deadline, SAM_TIMEOUT_SECONDS,
                                   failure=unavailable,
                                   logEvent='samModelError')
    return parseBoxes(_outputText(response))

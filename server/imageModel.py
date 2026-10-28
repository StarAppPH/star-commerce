"""imageModel.py — product photos from Meta's Muse Image model
(hand-written).

editImages() sends the admin's reference photos and a prompt to
POST /v1/images/edits through chatModel's client (MODEL_API_KEY, the SDK
retrying nothing) and returns the images as bytes. It retries as
chatModel.callModel does: once, only a connection error, a timeout, a 429
or a 5xx, within the call's deadline, never after a Retry-After sleep.

Meta refusing the request on content safety (a 400
content_policy_violation) is a ValueError the admin can act on
(PHOTO_REQUEST_DECLINED); any other SDK error, a missing key or a passed
deadline is unavailable(), a 503. Neither quotes the prompt, and errors are
logged by class and status only. `moderation` is never sent: Meta refuses
it. Only the images returned are billed.
"""

import base64

import chatModel
from services.__request import ServiceUnavailable

IMAGE_MODEL = 'muse-image-1.0'
IMAGE_OUTPUT_FORMAT = 'jpeg'
IMAGE_SIZE = '1024x1024'
# The most one attempt waits, cut to the time the call has left.
IMAGE_TIMEOUT_SECONDS = 75
CONTENT_POLICY_VIOLATION = 'content_policy_violation'
PHOTO_REQUEST_DECLINED = ('Meta declined this photo request. Try different '
                          'photos or wording.')
# What routeErrors' 503 names: "Photo generation is unavailable right now".
PHOTO_SERVICE = 'photo generation'


def unavailable() -> ServiceUnavailable:
    """The 503 of every failed photo call: "Photo generation is unavailable
    right now" (routeErrors)."""
    return ServiceUnavailable(PHOTO_SERVICE, 'photos')


def _declined(error: Exception) -> ValueError | None:
    """PHOTO_REQUEST_DECLINED when Meta refused the request on content
    safety (a 400 whose error code or type is content_policy_violation)."""
    import openai
    if (isinstance(error, openai.BadRequestError)
            and CONTENT_POLICY_VIOLATION in (error.code, error.type)):
        return ValueError(PHOTO_REQUEST_DECLINED)
    return None


def _client():
    """chatModel's client, or unavailable() (photos' 503) without
    MODEL_API_KEY."""
    try:
        return chatModel.client()
    except ServiceUnavailable:
        raise unavailable() from None


def editImages(images: list[tuple[str, bytes, str]], prompt: str, n: int,
               deadline: float) -> list[bytes]:
    """`n` images Meta makes from the reference `images` (each a file
    name, its bytes and content type) and `prompt`, done before `deadline`
    (time.monotonic()), each attempt within IMAGE_TIMEOUT_SECONDS. Returns
    the images it sent back (at most `n`, maybe fewer), decoded."""

    def request(timeout: float):
        return _client().images.edit(model=IMAGE_MODEL,
                                     image=images,
                                     prompt=prompt,
                                     n=n,
                                     size=IMAGE_SIZE,
                                     output_format=IMAGE_OUTPUT_FORMAT,
                                     response_format='b64_json',
                                     timeout=timeout)

    response = chatModel.callModel(request, deadline, IMAGE_TIMEOUT_SECONDS,
                                   failure=unavailable, refusal=_declined,
                                   logEvent='imageModelError')
    decoded = []
    for image in response.data or []:
        if image.b64_json:
            decoded.append(base64.b64decode(image.b64_json))
    return decoded[:n]

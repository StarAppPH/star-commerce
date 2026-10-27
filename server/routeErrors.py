"""What a generated route (app.py) or push handler (appPubSub.py) answers and
logs when it raised.

AppCreator emits, in every handler:

    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {<the handler's log entry>, 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        ...

`status` is what the branch answers an error that is the CALLER's to fix.
routeFailure decides whether `error` is one:
  - PermissionError: 403 with its message (role and ownership checks).
  - pydantic ValidationError (a ValueError): 400 naming each failing field
    and why, never the value it was given (str(e) quotes the input, which can
    be a stored customer's data).
  - any other ValueError: 400 with its message. A route raises one on purpose
    to tell the user what to fix, so its text is user-facing by contract.
  - OutdatedDataError (mongoDb's optimistic lock): 400 with its fixed message.
  - a ServiceError a sibling answered with 400 and an envelope message: 400
    with the sibling's message (the sibling's own ValueError text).
  - any other error of a push handler whose Pub/Sub envelope never parsed:
    400 "Malformed Pub/Sub push" (Pub/Sub retries any non-2xx, then
    dead-letters).
Anything else is a server fault, whatever `status` says:
  - any other ServiceError, or ServiceUnavailable: 503, a generic message
    naming the service, and an error id;
  - everything else: 500, a generic message and an error id. Its text can
    quote data and code internals, so it never leaves the server.
A 5xx is a real HTTP 5xx, so Cloud Run's error-rate metrics see a server
fault. The generated TS client hands it to the page as status 0 with the
message (see AppCreator._ts_method_code).

The handler logs each failure ONCE: its own log entry (function, event,
request keys or messageId) plus failure.traceback, the stack (file, line,
function and source line of each frame) and the exception type with the
error id, WITHOUT the exception's message, its chained causes or any local
variable. A caller's error is a WARNING, a server fault an ERROR.
"""
import functools
import logging
import traceback
import uuid
from typing import Any, NamedTuple

from flask import jsonify
from pydantic import BaseModel, ValidationError

from mongoDb import OutdatedDataError
from objects import ApiResponse
from services.__request import ServiceError, ServiceUnavailable

STATUS_BAD_REQUEST = 400
STATUS_FORBIDDEN = 403
STATUS_SERVER_ERROR = 500
STATUS_SERVICE_UNAVAILABLE = 503

MALFORMED_PUSH_MESSAGE = 'Malformed Pub/Sub push'
_MAX_FIELD_ERRORS = 3


class RouteFailure(NamedTuple):
    status: int
    message: str
    level: int  # logging.WARNING for the caller's error, logging.ERROR for ours
    traceback: str
    data: Any = None
    push: bool = False

    def response(self):
        """The failure envelope, as a Flask view returns it. A push handler's
        carries the (message-free) traceback: DeadLetterDashboard reads it on
        replay."""
        envelope = ApiResponse(message=self.message,
                               status=self.status,
                               data=self.data,
                               **({'traceback': self.traceback}
                                  if self.push else {}))
        return jsonify(envelope.model_dump(exclude_unset=True)), self.status


def safeTraceback(error: BaseException, errorId: str) -> str:
    """The stack of `error` and its type, never its message."""
    frames = ''.join(traceback.format_tb(error.__traceback__))
    errorType = type(error)
    return ('Traceback (most recent call last):\n'
            f'{frames}{errorType.__module__}.{errorType.__qualname__}'
            f' (errorId {errorId})')


@functools.cache
def _declaredFieldNames() -> frozenset:
    """Every field name and alias the objects models declare."""
    import objects
    names = set()
    for value in vars(objects).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            for name, field in value.model_fields.items():
                names.add(name)
                if field.alias:
                    names.add(field.alias)
    return frozenset(names)


def _locPart(part) -> str:
    """A list index or a declared field name as it is; anything else (a dict
    key, which is the caller's data, or a union tag) as '*'."""
    if isinstance(part, int) or part in _declaredFieldNames():
        return str(part)
    return '*'


def validationMessage(error: ValidationError) -> str:
    """'Invalid <field>: <why>' for each failing field, without the input."""
    details = error.errors(include_input=False,
                           include_url=False,
                           include_context=False)
    parts = [
        f"{'.'.join(_locPart(part) for part in detail['loc']) or 'value'}: "
        f"{detail['msg']}" for detail in details[:_MAX_FIELD_ERRORS]
    ]
    if len(details) > _MAX_FIELD_ERRORS:
        parts.append(f'and {len(details) - _MAX_FIELD_ERRORS} more')
    return f"Invalid {'; '.join(parts)}"


def _callersMessage(error: Exception) -> str | None:
    """The message to answer `error` with if it is the caller's to fix, else
    None."""
    if isinstance(error, ValidationError):
        return validationMessage(error)
    if isinstance(error, (PermissionError, ValueError, OutdatedDataError)):
        return str(error)
    if (isinstance(error, ServiceError) and error.status == STATUS_BAD_REQUEST
            and error.message):
        return error.message
    return None


def routeFailure(error: Exception,
                 status: int,
                 push: bool = False,
                 envelopeParsed: bool = True,
                 data=None) -> RouteFailure:
    """How a handler answers and logs `error` (see the module docstring).
    `status` is what it answers an error that is the caller's to fix; `data`
    is what that envelope carries for the page (None but for a refusal that
    hands the form its detail)."""
    errorId = uuid.uuid4().hex[:12]
    tb = safeTraceback(error, errorId)
    message = _callersMessage(error)
    if message is not None:
        return RouteFailure(status, message, logging.WARNING, tb, data, push)
    if push and not envelopeParsed:
        return RouteFailure(STATUS_BAD_REQUEST, MALFORMED_PUSH_MESSAGE,
                            logging.WARNING, tb, None, push)
    if isinstance(error, (ServiceError, ServiceUnavailable)):
        service = error.service[:1].upper() + error.service[1:]
        return RouteFailure(
            STATUS_SERVICE_UNAVAILABLE,
            f'{service} is unavailable right now. Please try again. '
            f'(error id {errorId})', logging.ERROR, tb, None, push)
    return RouteFailure(
        STATUS_SERVER_ERROR,
        'Something went wrong on our side. Please try again. '
        f'(error id {errorId})', logging.ERROR, tb, None, push)

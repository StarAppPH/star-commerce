"""toolArguments.py — the chat model's tool-call arguments before and after
their args model checks them (hand-written; chatModel's tools and
chatForms' form tools share it).

- decodedArguments(): Meta's Muse sometimes sends an object or a list
  argument as a JSON-encoded string ({"delivery": "{\\"address\\": ...}"}).
  A top-level argument that is a str, whose field takes an object or a
  list and never a str, is decoded when it parses to that shape; anything
  else is left as sent, so the args model refuses it as before. Only the
  top level: a field inside an object or list is never decoded.
- refusedFields(): the fields a ValidationError refused (items.0.quantity),
  each a shape error's reason after it ("delivery (expected an object)"),
  never a value.
"""

import json
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

_OBJECT = 'object'
_LIST = 'list'
_LIST_TYPES = (list, tuple, set, frozenset)

# Pydantic error types of an object or a list sent as something else.
SHAPE_REASONS = {
    'model_type': 'expected an object',
    'model_attributes_type': 'expected an object',
    'dict_type': 'expected an object',
    'list_type': 'expected a list',
    'tuple_type': 'expected a list',
    'set_type': 'expected a list',
    'frozen_set_type': 'expected a list',
}


def _shapes(annotation) -> tuple[set[str], bool]:
    """(the shapes, object and list, that `annotation` takes, whether it
    takes a str). Any counts as taking a str: it is never decoded."""
    if hasattr(annotation, '__value__'):  # a `type X = ...` alias
        return _shapes(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Annotated:
        return _shapes(get_args(annotation)[0])
    if origin is Union or origin is types.UnionType:
        shapes, takesStr = set(), False
        for member in get_args(annotation):
            memberShapes, memberStr = _shapes(member)
            shapes |= memberShapes
            takesStr = takesStr or memberStr
        return shapes, takesStr
    if origin is Literal:
        return set(), any(isinstance(value, str)
                          for value in get_args(annotation))
    if annotation is Any:
        return set(), True
    if annotation is dict or origin is dict:
        return {_OBJECT}, False
    if annotation in _LIST_TYPES or origin in _LIST_TYPES:
        return {_LIST}, False
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return {_OBJECT}, False
        if issubclass(annotation, str):
            return set(), True
    return set(), False


def decodedArguments(argsModel: type[BaseModel], arguments: dict) -> dict:
    """`arguments` with each top-level JSON-string object or list decoded
    where `argsModel`'s field takes that shape and no str."""
    fields = {}
    for name, info in argsModel.model_fields.items():
        fields[name] = info
        if info.alias:
            fields[info.alias] = info
    decoded = dict(arguments)
    for key, value in arguments.items():
        info = fields.get(key)
        if info is None or not isinstance(value, str):
            continue
        shapes, takesStr = _shapes(info.annotation)
        if takesStr or not shapes:
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if ((isinstance(parsed, dict) and _OBJECT in shapes)
                or (isinstance(parsed, list) and _LIST in shapes)):
            decoded[key] = parsed
    return decoded


def refusedFields(error: ValidationError, whole: str) -> str:
    """The fields `error` refused, `whole` for the input itself, each with
    its shape error's reason; never a value."""
    fields: dict[str, str | None] = {}
    for detail in error.errors(include_input=False):
        field = '.'.join(str(part) for part in detail['loc']) or whole
        reason = SHAPE_REASONS.get(detail['type'])
        if field not in fields or (fields[field] is None and reason):
            fields[field] = reason
    return ', '.join(f'{field} ({reason})' if reason else field
                     for field, reason in fields.items())

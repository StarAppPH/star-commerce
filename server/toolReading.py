"""Reading the routes, the tools and their answers' models, for the tests.

Shared by test_route_tool_twins.py, test_unused_tool_fields.py and
test_ai_copy.py; no production code imports it.
  - stampedMembers / routes / tools: the @route_config and @mcp_tool members;
  - reachable / routeModels: the models an answer reaches;
  - leaves / pathText: every scalar leaf of a model, at any depth;
  - sample / fieldAt / valueAt / withValue / changedValues: a full sample of
    a model (every optional field set, one element in every list, distinct
    type-valid leaves) and one-leaf changes of it.
The walk follows a nested model alone, nullable or in a list. A field that
holds a dict of models, two or more models, or a model in any other shape
(a list of lists, a tuple) is refused with a clear error (TypeError, naming
the model and field): the sample would cover only one of them, so a field
of another would never be checked. No tool model has one today; teach the
walk that shape before adding one.
"""
import datetime
import enum
import typing
from functools import cache
from typing import NamedTuple

from pydantic import BaseModel, TypeAdapter, ValidationError

from objects.base import _is_union
from responseModelCheck import models_in

SAMPLE_START = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


# ---- Routes and tools ----

def stampedMembers(root: type, stamp: str) -> dict:
    """Name -> function of every member of `root` (and its bases, in MRO
    order) carrying `stamp`: httpMethod for a route, mcpAudiences for a
    tool."""
    found = {}
    for cls in root.__mro__:
        for name, func in vars(cls).items():
            if callable(func) and hasattr(func, stamp):
                found.setdefault(name, func)
    return found


def routes() -> dict:
    """Name -> route function of every ApiRequests @route_config."""
    from ApiRequests import ApiRequests
    return stampedMembers(ApiRequests, 'httpMethod')


def tools() -> dict:
    """Name -> tool method (with its responseModel) of every @mcp_tool."""
    from ToolRequests import ToolRequests
    return stampedMembers(ToolRequests, 'mcpAudiences')


def reachable(model: type, seen: set | None = None) -> set:
    """`model` and every model nested in it, at any depth."""
    seen = set() if seen is None else seen
    if model in seen:
        return seen
    seen.add(model)
    for field in model.model_fields.values():
        for nested in models_in(field.annotation):
            reachable(nested, seen)
    return seen


@cache
def routeModels() -> frozenset:
    """Every model a route's responseModel reaches: the route gate's."""
    found: set = set()
    for route in routes().values():
        model = getattr(route, 'responseModel', None)
        if isinstance(model, type) and issubclass(model, BaseModel):
            reachable(model, found)
    return frozenset(found)


# ---- Leaves ----

def key(field, name: str) -> str:
    """A field's serialized key."""
    return field.alias or name


def _peelAnnotated(annotation):
    if typing.get_origin(annotation) is typing.Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def nonNone(annotation) -> tuple:
    """`annotation`'s members other than None, and whether None is one."""
    annotation = _peelAnnotated(annotation)
    if _is_union(annotation):
        args = typing.get_args(annotation)
        return (tuple(_peelAnnotated(a) for a in args
                      if a is not type(None)),
                type(None) in args)
    return (annotation, ), False


def nestedModel(model: type, name: str, field):
    """(the model `field` holds, whether in a list), or None for a leaf.
    Raises TypeError for a field holding models in any other shape (see the
    module docstring)."""
    found = list(dict.fromkeys(models_in(field.annotation)))
    if not found:
        return None
    members, _ = nonNone(field.annotation)
    shape = f'{model.__name__}.{name}'
    if len(found) > 1 or len(members) != 1:
        raise TypeError(
            f'{shape} holds {len(found)} models ({", ".join(m.__name__ for m in found)}): '
            'the tool-field gate samples one model per field; teach '
            'toolReading to sample each one before adding such a field')
    member = members[0]
    if member is found[0]:
        return found[0], False
    if (typing.get_origin(member) is list
            and _peelAnnotated(typing.get_args(member)[0]) is found[0]):
        return found[0], True
    raise TypeError(
        f'{shape} holds {found[0].__name__} as {member!r} (a dict of models, '
        'or a nested container): the tool-field gate walks a model alone, '
        'nullable or in a list only; teach toolReading that shape before '
        'adding such a field')


class Leaf(NamedTuple):
    model: type          # the model at that path
    key: str             # its serialized key
    path: tuple          # keys, 0 for a list's item: ('products', 0, 'name')
    containers: tuple    # (model, key) of each nested model's key above it


def pathText(path: tuple) -> str:
    text = ''
    for step in path:
        text += '[]' if step == 0 else ('.' if text else '') + step
    return text


def leaves(model: type, path: tuple = (), containers: tuple = ()) -> list:
    """Every scalar leaf of `model`, at any depth. A never-sent guard
    (typed None) carries nothing and is skipped."""
    found = []
    for name, field in model.model_fields.items():
        if field.annotation is type(None):
            continue
        fieldKey = key(field, name)
        nested = nestedModel(model, name, field)
        if nested is None:
            found.append(Leaf(model, fieldKey, path + (fieldKey, ),
                              containers))
            continue
        inner, isList = nested
        found += leaves(inner,
                        path + ((fieldKey, 0) if isList else (fieldKey, )),
                        containers + ((model, fieldKey), ))
    return found


# ---- The full sample ----

class _Counter:
    def __init__(self):
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _adapter(field) -> TypeAdapter:
    return TypeAdapter(field.rebuild_annotation())


def _valid(adapter: TypeAdapter, value) -> bool:
    try:
        adapter.validate_python(value)
        return True
    except ValidationError:
        return False


def _scalarCandidates(annotation, n: int, text: str) -> list:
    """Type-valid-looking values of a scalar type, most distinct first."""
    annotation = _peelAnnotated(annotation)
    if typing.get_origin(annotation) is typing.Literal:
        return list(typing.get_args(annotation))
    if annotation is bool:
        return [True]
    if annotation is int:
        return [n, n % 100 + 1, 1, 0]
    if annotation is float:
        return [n + 0.5, n % 80 + 0.5, 0.5, 0.25]
    if annotation is datetime.datetime:
        return [(SAMPLE_START + datetime.timedelta(minutes=n)).isoformat()]
    if annotation is datetime.date:
        return [(SAMPLE_START + datetime.timedelta(days=n)).date().isoformat()]
    if annotation is str:
        return [f's{n}:{text}', f's{n}', f'+63917{n:07d}',
                f'#a1b2{n % 100:02d}', f'v{n}', f'slug-{n}', 'x']
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return list(annotation)
    raise TypeError(f'no sample for {annotation!r}')


def _candidates(annotation, n: int, text: str) -> list:
    """Candidate values of a leaf field's annotation (None left out)."""
    members, _ = nonNone(annotation)
    member = members[0]
    origin = typing.get_origin(member)
    if origin is list:
        return [[c] for c in _candidates(typing.get_args(member)[0], n, text)]
    if origin is dict:
        return [{'k': c}
                for c in _candidates(typing.get_args(member)[1], n, text)]
    return _scalarCandidates(member, n, text)


def leafValue(field, n: int, text: str):
    adapter = _adapter(field)
    for candidate in _candidates(field.annotation, n, text):
        if _valid(adapter, candidate):
            return candidate
    raise AssertionError(f'{text}: no type-valid sample value')


def sample(model: type, counter: _Counter | None = None,
           path: tuple = ()) -> dict:
    """A full sample of `model`: every field set (optional ones too), one
    element in every list, distinct type-valid leaves."""
    counter = counter or _Counter()
    built = {}
    for name, field in model.model_fields.items():
        if field.annotation is type(None):
            continue
        fieldKey = key(field, name)
        nested = nestedModel(model, name, field)
        if nested:
            inner, isList = nested
            row = sample(inner, counter,
                         path + ((fieldKey, 0) if isList else (fieldKey, )))
            built[fieldKey] = [row] if isList else row
        else:
            built[fieldKey] = leafValue(field, counter.next(),
                                        pathText(path + (fieldKey, )))
    return built


def fieldAt(model: type, path: tuple):
    """The pydantic field of the leaf at `path` in `model`."""
    current = model
    field = None
    for step in path:
        if step == 0:
            continue
        name = next(n for n, f in current.model_fields.items()
                    if key(f, n) == step)
        field = current.model_fields[name]
        nested = nestedModel(current, name, field)
        if nested:
            current = nested[0]
    return field


def valueAt(data, path: tuple):
    for step in path:
        data = data[step]
    return data


def withValue(data, path: tuple, value):
    """A copy of `data` with the leaf at `path` set to `value`."""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(data, list):
        return [withValue(data[0], rest, value), *data[1:]]
    return {**data, head: withValue(data[head], rest, value)}


def _changedScalar(annotation, value) -> list:
    annotation = _peelAnnotated(annotation)
    if typing.get_origin(annotation) is typing.Literal:
        return [member for member in typing.get_args(annotation)
                if member != value]
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, (int, float)):
        return [value + 1, value - 1, value / 2]
    if annotation is datetime.datetime:
        return [(datetime.datetime.fromisoformat(value)
                 + datetime.timedelta(days=1)).isoformat()]
    if annotation is datetime.date:
        return [(datetime.date.fromisoformat(value)
                 + datetime.timedelta(days=1)).isoformat()]
    if isinstance(value, str):
        last = value[-1]
        swap = {'9': '8'}.get(last, chr(ord(last) + 1)) if last.isalnum() \
            else 'y'
        return [value + 'x', value[:-1] + swap]
    if isinstance(value, enum.Enum):
        return [member for member in type(value) if member != value]
    raise TypeError(f'no change for {annotation!r}')


def changedValues(field, value) -> list:
    """The type-valid changes of one leaf: its type's first valid change
    (every other member for a Literal), and None when it may be null."""
    members, optional = nonNone(field.annotation)
    member = members[0]
    origin = typing.get_origin(member)
    if origin is list:
        inner = typing.get_args(member)[0]
        options = [[c] for c in _changedScalar(inner, value[0])]
    elif origin is dict:
        inner = typing.get_args(member)[1]
        options = [{'k': c} for c in _changedScalar(inner, value['k'])]
    else:
        inner = member
        options = _changedScalar(member, value)
    adapter = _adapter(field)
    valid = [option for option in options if _valid(adapter, option)]
    isLiteral = typing.get_origin(_peelAnnotated(inner)) is typing.Literal
    changes = valid if isLiteral else valid[:1]
    if optional:
        changes.append(None)
    return changes

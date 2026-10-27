"""The response-model rule: what a route sends is a ResponseModel, all the way
down, built from typed leaves only.

Every ApiRequests route's responseModel and every Pub/Sub publisher's payload
model (its route's responseModel, or its PUBSUB_PAYLOAD_MODELS entry) is an
objects.ResponseModel, and so is every model nested in it, at any depth. Every
pydantic base of one is a ResponseModel too, so none is or inherits a database
model (MongoModel, AppModel, StoredDocumentKeys, a *View). A RootModel (a bare
cross-repo route) is looked through: its contents follow the rule.

Every field is built from allowed leaves only (ALLOWED_LEAVES: str, int,
float, bool, None, datetime, date, a Literal, an Enum, a ResponseModel)
through Optional, unions, list[...] and dict[str, ...]. Anything else (Any, a
bare dict or list, object, a TypedDict, a dataclass, a tuple, a set) could
carry a whole document past the undeclared-key check, so it is refused. A
ResponseModel declares no @computed_field: it sends exactly the fields it
declares, so a derived value is a field the route fills. The one field typed
None it may declare is a never-sent guard, `x: SkipJsonSchema[None] = None`:
a key the route must never send a value for (objects.ResponseModel).
Nothing else hides from the TS type: no SkipJsonSchema anywhere in a field
(but on a SECRET_RESPONSE_KEYS key, which every egress strips), no excluded
field, no callable json_schema_extra. A field is read under one key, its
alias else its name: no other serialization or validation alias, so the
check, the projections and the TS type always agree on it. A field that can be
one of two or more models (a union of them, at any depth) declares a str
discriminator, Field(discriminator='<tag field>'), so the member a value is
cut as is the one its tag names, never one guessed by validation.

PubSubRequests push handlers are exempt: their return value only acks to
Pub/Sub.

route_config refuses a responseModel that breaks the rule when the route is
decorated (assertResponseModel), AppCreator refuses to generate
(validateRoutes for the routes, assertPublisherModels for the publishers),
and test_route_response_models.py's test_responseModelsAreResponseModels
asserts it. A shared file (create-star-app's fleet/sharedFiles.json): edit the
template, never one repo's copy.
"""
import datetime
import enum
import functools
import inspect
import types
import typing
from pathlib import Path
from typing import Any, List, get_args, get_origin

from pydantic import BaseModel, RootModel
from pydantic.json_schema import SkipJsonSchema

# The leaf types a response field may be built from (with a Literal, an Enum
# and a ResponseModel).
ALLOWED_LEAVES = (str, int, float, bool, type(None), datetime.datetime,
                  datetime.date)


def models_in(annotation: Any) -> list:
    """The pydantic models an annotation holds, in order, through Optional,
    unions, Annotated, lists, tuples and dict values (a model itself is not
    looked into)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in get_args(annotation) for model in models_in(arg)]


@functools.lru_cache(maxsize=None)
def _where(cls: type) -> str:
    """`cls`'s file:line, for a message that names it. Call it only to word
    a problem (it is cached): inspect.getsourcelines re-parses the whole
    module, and calling it for every field of every route's model took most
    of an app import's 25-65 s (route_config runs this check at decoration,
    at every worker start)."""
    try:
        return (f'{inspect.getsourcefile(cls)}:'
                f'{inspect.getsourcelines(cls)[1]}')
    except (OSError, TypeError):
        return getattr(cls, '__module__', '?')


def _unallowedLeaves(annotation) -> list:
    """The parts of `annotation` that are no allowed leaf, as text; a model
    counts as a leaf here (visit checks it)."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is typing.Annotated:
        return _unallowedLeaves(args[0])
    if origin is typing.Literal:
        return []
    from objects import _is_union
    if _is_union(annotation):
        return [leaf for arg in args for leaf in _unallowedLeaves(arg)]
    if origin is list:
        return _unallowedLeaves(args[0]) if args else ['list']
    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            return [repr(annotation)]
        return _unallowedLeaves(args[1])
    if annotation in ALLOWED_LEAVES:
        return []
    if isinstance(annotation, type) and issubclass(annotation,
                                                    (enum.Enum, BaseModel)):
        return []
    return [getattr(annotation, '__name__', None) or repr(annotation)]


def _skipsSchema(field) -> bool:
    """Whether a field's own metadata hides it from the JSON schema."""
    return any(isinstance(meta, SkipJsonSchema) for meta in field.metadata)


def _hiddenInside(annotation) -> bool:
    """Whether a SkipJsonSchema sits anywhere inside an annotation (a union
    member, a list item): a value sent but invisible to the TS type."""
    if get_origin(annotation) is typing.Annotated:
        if any(isinstance(meta, SkipJsonSchema)
               for meta in annotation.__metadata__):
            return True
    return any(_hiddenInside(arg) for arg in get_args(annotation))


def _unionProblems(annotation, discriminator=None) -> list:
    """What is wrong with the unions inside `annotation`, read as the cut
    reads them (objects._union_of: Annotated peeled with its tag, nested
    unions flattened), one line each:
    - members carrying two or more different models that no str
      discriminator tags. Only a union of the models themselves can be
      tagged: `discriminator` is the field's own str tag (objects._tag_of),
      which tags only the union the field is; a union nested in a list, a
      dict or an Optional is tagged by an Annotated Field(discriminator=...)
      or Discriminator(...). A union carrying models in other members
      (list[A] | list[B], A | dict[str, B]) can carry no tag;
    - a model unioned with a dict: both accept a dict, so a value that is not
      the model would go out whole as the dict."""
    from objects import (_carries_several_models, _model_members,
                         _tag_literals, _union_of)
    members, tag = _union_of(annotation, discriminator)
    found = []
    if len(members) > 1:
        carrying = [member for member in members if models_in(member)]
        models = _model_members(members)
        tagged = tag and len(models) == len(carrying)
        untaggable = [
            model.__name__ for model in models
            if tagged and not _tag_literals(model, tag)
        ]
        if untaggable:
            found.append(
                f'a union tagged {tag!r} whose member '
                f'{", ".join(untaggable)} declares no Literal {tag!r} field: '
                "a value of it would be cut to every member's keys")
        if _carries_several_models(members) and not tagged:
            names = ' | '.join(
                dict.fromkeys(model.__name__ for member in carrying
                              for model in models_in(member)))
            found.append(
                f'an untagged union of models ({names}): a field that can '
                'be one of two or more ResponseModels declares a str '
                'discriminator on a union of the models themselves, '
                "Field(discriminator='<tag field>') (list[A | B], never "
                'list[A] | list[B]), so its value is cut as the member its '
                'tag names')
        dicts = [member for member in members if get_origin(member) is dict]
        if models and dicts:
            found.append(
                'a union of a model and a dict ('
                + ' | '.join(model.__name__ for model in models)
                + ' | dict): both accept a dict, so a value that is not the '
                'model would go out whole as the dict')
    for member in members:
        for arg in get_args(member):
            found += _unionProblems(arg)
    return found


def _secretKeys() -> frozenset:
    """AppConfig.SECRET_RESPONSE_KEYS: keys every egress strips (ApiResponse,
    pubSubDecorator), so a response field holding one is hidden from the TS
    type on purpose."""
    from AppConfig import SECRET_RESPONSE_KEYS
    return frozenset(SECRET_RESPONSE_KEYS)


def responseModelProblems(model: type) -> List[str]:
    """Why `model` breaks the rule, one line per offending class or field,
    with its path in the tree and its file:line; [] when it holds."""
    from objects import ResponseModel, _tag_of
    problems: List[str] = []
    seen: set = set()

    def visit(cls: type, path: str) -> None:
        if cls in seen:
            return
        seen.add(cls)
        if issubclass(cls, RootModel):
            root = cls.model_fields['root']
            annotation = root.annotation
            for leaf in _unallowedLeaves(annotation):
                problems.append(f'{path}: {cls.__name__} ({_where(cls)}) '
                                f'holds {leaf}, which is no allowed response '
                                'leaf')
            for union in _unionProblems(
                    annotation, _tag_of(root.discriminator, *root.metadata)):
                problems.append(
                    f'{path}: {cls.__name__} ({_where(cls)}) holds {union}')
            for nested in models_in(annotation):
                visit(nested, path)
            return
        if not issubclass(cls, ResponseModel):
            problems.append(f'{path}: {cls.__name__} ({_where(cls)}) is not '
                            'a ResponseModel')
        else:
            for base in cls.__mro__[1:]:
                if (issubclass(base, BaseModel)
                        and base not in (BaseModel, ResponseModel)
                        and not issubclass(base, ResponseModel)):
                    problems.append(
                        f'{path}: {cls.__name__} ({_where(cls)}) inherits '
                        f'{base.__name__} ({_where(base)}), which is not a '
                        'ResponseModel')
        for name in cls.model_computed_fields:
            problems.append(
                f'{path}.{name}: {cls.__name__} ({_where(cls)}) declares it '
                'a @computed_field: a response sends only declared fields, '
                'so make it a field the route fills')
        if callable(cls.model_config.get('json_schema_extra')):
            problems.append(
                f'{path}: {cls.__name__} ({_where(cls)}) rewrites its JSON '
                'schema (a callable json_schema_extra): the TS type must '
                'show every field it sends')
        for name, field in cls.model_fields.items():
            key = field.alias or name
            fieldPath = f'{path}.{key}'

            def where() -> str:  # lazy: _where only for a problem
                return f'{fieldPath}: {cls.__name__} ({_where(cls)})'

            isGuard = (field.annotation is type(None)
                       and field.default is None and _skipsSchema(field))
            if field.annotation is type(None) and not isGuard:
                problems.append(
                    f'{where()} types it None: the only None-typed field is a '
                    'never-sent guard, declared `SkipJsonSchema[None] = '
                    'None`')
            if not isGuard and key not in _secretKeys() and (
                    _skipsSchema(field)
                    or _hiddenInside(field.annotation)):
                problems.append(
                    f'{where()} hides it from the TS type (SkipJsonSchema): a '
                    'response field the client cannot see; only a never-sent '
                    'guard (`SkipJsonSchema[None] = None`) or a '
                    'SECRET_RESPONSE_KEYS key may')
            if field.exclude or callable(field.json_schema_extra):
                problems.append(
                    f'{where()} is excluded from, or rewrites, its JSON '
                    'schema: the TS type must show every field it sends')
            if (field.serialization_alias not in (None, key)
                    or field.validation_alias not in (None, key)):
                problems.append(
                    f'{where()} has a serialization or validation alias other '
                    f'than {key!r}: a response is read under one key, its '
                    'alias (else its name), which the TS type shows')
            for union in _unionProblems(
                    field.annotation,
                    _tag_of(field.discriminator, *field.metadata)):
                problems.append(f'{where()} types it with {union}')
            for leaf in _unallowedLeaves(field.annotation):
                problems.append(
                    f'{fieldPath}: {cls.__name__} ({_where(cls)}) types it '
                    f'with {leaf}, which is no allowed response leaf (a '
                    'scalar, datetime, date, Literal, Enum or ResponseModel, '
                    'through Optional, unions, list and dict[str, ...])')
            for nested in models_in(field.annotation):
                visit(nested, fieldPath)

    visit(model, model.__name__)
    return problems


def assertResponseModel(funcName: str, model) -> None:
    """route_config's decoration-time check: `model` is a ResponseModel (or
    the RootModel of a bare cross-repo route) and its whole tree holds.
    Raises ValueError naming the route and the model."""
    from objects import ResponseModel
    if not (isinstance(model, type)
            and issubclass(model, (ResponseModel, RootModel))):
        raise ValueError(
            f'responseModel of {funcName} must be a ResponseModel subclass '
            f'(objects.py), got {model!r}')
    problems = responseModelProblems(model)
    if problems:
        raise ValueError(
            f'responseModel {model.__name__} of {funcName} must be a '
            'ResponseModel all the way down (objects.ResponseModel, never a '
            'database model):\n  - ' + '\n  - '.join(problems))


def publisherModelProblems(projectRoot: Path) -> List[str]:
    """responseModelProblems of every Pub/Sub publisher's payload model, as
    pubSubModels.collectPublishers reads them, each line prefixed with the
    publisher."""
    from pubSubModels import collectPublishers
    return [
        f'{spec.anchor}: {problem}'
        for spec in collectPublishers(projectRoot)
        for problem in responseModelProblems(spec.model)
    ]


def assertPublisherModels(projectRoot: Path) -> None:
    """AppCreator's refusal: SystemExit listing every publisher whose payload
    model breaks the rule."""
    problems = publisherModelProblems(projectRoot)
    if problems:
        raise SystemExit(
            'Pub/Sub payload models must be ResponseModels all the way down '
            '(objects.ResponseModel, never a database model):\n  - ' +
            '\n  - '.join(problems))

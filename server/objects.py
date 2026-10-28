"""
objects.py — Define your Pydantic models here.

HOW IT WORKS:
  - Models defined here are used for data validation, serialization, and TypeScript schema generation.
  - Run `ENVIRONMENT=localdev python AppCreator.py` to generate TypeScript interfaces from these models into client/app/schemas/.
  - Every ApiRequests route declares one of these models as its responseModel
    (@route_config(responseModel=...)): the generated client method returns
    Promise<ApiResponse<Model>>, and in localdev route_config checks every
    response against it (see "RESPONSE MODELS" below and ARCHITECTURE.md).
  - Models should contain pure data + validation logic. No database calls (those belong in ApiRequests or action files).

MODEL HIERARCHY:
  - MongoModel: Base for documents stored in MongoDB (has _id, _version, and empty-string coercion).
  - AppModel: Base for embedded/non-persisted models (has empty-string coercion only).
  - ResponseModel: Base for what a route SENDS (its responseModel, a Pub/Sub
    publisher's payload model) and for every model nested in one. Never a
    database model: see "RESPONSE MODELS" below.
  - BaseModel: Use only for simple data carriers with no str | None fields (e.g., ApiResponse).
  - StoredDocumentKeys: mixin for a document as READ from MongoDB
    (`class FooView(StoredDocumentKeys, Foo)`): _id and _version required.

WRITING A MODEL:
  1. Create a class that extends MongoModel (for DB documents) or AppModel (for embedded models).
  2. Define fields with type annotations and Field(...) for metadata.
  3. MongoModel auto-generates _id and provides _version for optimistic locking.

FIELD CONVENTIONS:
  - MongoModel handles _id and _version automatically — you don't need to define them.
  - Use `Field(default_factory=...)` for dynamic defaults (e.g., timestamps, random IDs).
  - Use `str | None` for nullable fields — empty strings are auto-coerced to None.

RESPONSE MODELS (what a route declares as its responseModel):
  - A responseModel is a ResponseModel, and so is every model nested in it,
    all the way down: never a MongoModel, AppModel, StoredDocumentKeys or
    *View. A ResponseModel inherits from other ResponseModels only. The same
    holds for a Pub/Sub publisher's payload model. Its fields are built from
    allowed leaves only (a scalar, datetime, date, Literal, Enum or
    ResponseModel, through Optional, unions, list and dict[str, ...]: never
    Any, a bare dict or list, a TypedDict or a dataclass), and it declares
    no @computed_field. route_config refuses a model that breaks this when
    the route is decorated, AppCreator refuses to generate, naming the model
    with its file:line, and test_route_response_models.py asserts it too
    (responseModelCheck.py).
  - It declares EXACTLY the fields the route sends, each under its
    serialized key (its alias, else its name): in localdev (so in every
    test) the response check rejects any other key. So a route returns only
    those fields: it reads them with
    `db.readResponse(FooResponse, query, collection)` (or readResponses),
    which projects what the model declares (projection_of, never written by
    hand) and cuts what it read (projectedCopy), or builds the dict
    explicitly. A value the route reads for its own use is dropped before it
    returns (`projectedCopy(FooResponse, document)` keeps what the model
    declares). An aggregation answers with
    `db.aggregateResponses(FooResponse, pipeline, collection)`.
  - A field that can be one of two or more ResponseModels declares a str
    discriminator, `Field(discriminator='kind')`, each member a
    `kind: Literal[...]` of its own.
  - A route returns a dict with explicit keys: a list or a primitive is
    wrapped in a model with one field (`{'todos': [...]}`).
  - Fields the route always sets are REQUIRED. Omittable but never null:
    `x: str = Field(None)`. Omittable or null: `x: str | None = None`.
  - A defaulted field is optional (`x?:`) in the generated TS type. A model
    a route ALWAYS sends with every field may opt in to
    `model_config = ConfigDict(json_schema_serialization_defaults_required=True)`
    so its defaulted fields are required in TS.
  - No bare dict/list/Any fields, no dict[str, X] keyed by customer data,
    and no values in validator messages (they reach the client and logs).

VALIDATION:
  - Use @field_validator for single field validation.
  - Use @model_validator for cross-field validation.
  - A ResponseModel declares no @computed_field (refused): the generated TS
    type would declare it as always present, but a route returns a raw
    dict, which never carries it. Compute it in the route (a real field),
    or in the client.
  - No validator may EVER mutate a value it receives: return a new dict or
    list (copy-on-write) when it changes anything. That covers every
    before-validator (model_validator and field_validator alike) and an
    after-validator of an Any or bare dict/list field: all of them are
    handed the caller's own objects. In localdev route_config validates
    every route's return value against its responseModel without copying
    it, so a mutating validator would rewrite the response the caller
    receives (and the check raises when it detects that).

EXAMPLE:
  class Product(MongoModel):
      name: str = Field(..., description='Product name')
      price: float = Field(..., ge=0, description='Price in USD')
      category: str | None = Field(None, description='Product category')
      createdAt: datetime.datetime = Field(default_factory=datetime.datetime.now)

      @field_validator('name')
      @classmethod
      def name_not_empty(cls, v: str) -> str:
          if not v.strip():
              raise ValueError('Product name cannot be empty')
          return v


  class ProductRow(ResponseModel):
      \"\"\"One row of listProducts: what it sends, read with
      projection_of(ProductRow).\"\"\"
      id: str = Field(..., alias='_id')
      name: str


  class ProductListResponse(ResponseModel):
      products: list[ProductRow]
"""

import functools
import types
import typing

from pydantic import BaseModel, Field, RootModel, TypeAdapter, ValidationError, ValidationInfo, model_validator, computed_field, field_validator, field_serializer, ConfigDict
from pydantic_core import InitErrorDetails
import datetime
from typing import Any, Optional, Literal, List
from utils import generateRandomString
from roles import Role, RoleSetter, AllRoles, UserRoles
from AppConfig import SECRET_RESPONSE_KEYS


# ──────────────────────────────────────────────
# Empty String Coercion
# ──────────────────────────────────────────────

def _is_union(annotation) -> bool:
    """Whether `annotation` is a union (typing.Union, Optional or X | Y)."""
    return (typing.get_origin(annotation) is typing.Union
            or isinstance(annotation, types.UnionType))


_OPTIONAL_STR_FIELDS_CACHE: dict[type, list[list[str]]] = {}


def _coerce_empty_strings_to_none(cls, data):
    """Convert '' to None for str | None fields. Used as model_validator(mode='before').

    Copy-on-write: returns `data` itself when nothing needed coercing, and
    otherwise a shallow copy with the coerced keys. The input is never
    mutated, which is what lets route_config check a response without
    copying it first."""
    if not isinstance(data, dict):
        return data

    if cls not in _OPTIONAL_STR_FIELDS_CACHE:
        fields_to_check = []
        for field_name, field_info in cls.model_fields.items():
            ann = field_info.annotation
            origin = typing.get_origin(ann)
            args = typing.get_args(ann)
            is_optional_str = (
                _is_union(ann)
                and str in args
                and type(None) in args
                and len(args) == 2
            )
            if is_optional_str:
                keys = [field_name]
                if field_info.alias and field_info.alias != field_name:
                    keys.append(field_info.alias)
                fields_to_check.append(keys)
        _OPTIONAL_STR_FIELDS_CACHE[cls] = fields_to_check

    coerced = None
    for keys in _OPTIONAL_STR_FIELDS_CACHE[cls]:
        for key in keys:
            if key in data and data[key] == '':
                if coerced is None:
                    coerced = dict(data)
                coerced[key] = None

    if coerced is None:
        return data
    return coerced


# ──────────────────────────────────────────────
# Response check context
# ──────────────────────────────────────────────
# In localdev route_config validates every route's return value against its
# responseModel with context={RESPONSE_CHECK_CONTEXT_KEY: True}. A validator
# that repairs a legacy stored shape when a model is built from a DB read must
# reject that shape under this context instead, with a message naming the field
# and never its value: the generated TS type describes the CURRENT shape, so a
# legacy shape reaching the client is a mismatch to report, not one to hide.
RESPONSE_CHECK_CONTEXT_KEY = 'responseCheck'


def validate_route_response(model: type[BaseModel], value) -> BaseModel:
    """Validate a route's return value against its responseModel, under the
    response-check context. The one way to do it: route_config's check and
    the tests' assertMatchesModel both call this, so a test can never pass
    a response the check itself would fail. `value` is not copied (see the
    no-mutation rule at the top of this module)."""
    return model.model_validate(value,
                                context={RESPONSE_CHECK_CONTEXT_KEY: True})


# ──────────────────────────────────────────────
# Base Models
# ──────────────────────────────────────────────


class MongoModel(BaseModel):
    """Base for models stored in MongoDB with _id, _version, and empty-string coercion."""
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        extra='ignore',
    )

    id: str = Field(default_factory=generateRandomString,
                    alias='_id',
                    serialization_alias='_id')
    version: int | None = Field(None,
                                alias='_version',
                                serialization_alias='_version')

    @model_validator(mode='before')
    @classmethod
    def _coerce_empty_strings(cls, data):
        return _coerce_empty_strings_to_none(cls, data)

    @field_validator('id', mode='before')
    @classmethod
    def _set_id_if_none(cls, v):
        if v is None:
            return generateRandomString()
        return v

    @property
    def _id(self):
        return self.id

    @_id.setter
    def _id(self, value):
        self.id = value

    @property
    def _version(self):
        return self.version

    @_version.setter
    def _version(self, value):
        self.version = value


class AppModel(BaseModel):
    """Base for non-persisted/embedded models with empty-string-to-None coercion."""

    @model_validator(mode='before')
    @classmethod
    def _coerce_empty_strings(cls, data):
        return _coerce_empty_strings_to_none(cls, data)


# ──────────────────────────────────────────────
# Stored documents and projections
# ──────────────────────────────────────────────


class StoredDocumentKeys(BaseModel):
    """Mixin for a model describing a document as READ from MongoDB: _id and
    _version, which every stored document has, are required.

    MongoModel defaults both (a fresh id, no version) so a new document can
    be built; a response model inheriting those defaults would fabricate an
    id for a document that came back without one. Its _set_id_if_none,
    which replaces a null _id with a fresh one, is overridden here too, so a
    null _id fails as well. List it FIRST among the bases
    (`class FooView(StoredDocumentKeys, Foo)`) so its fields and that
    override win."""
    id: str = Field(...,
                    alias='_id',
                    serialization_alias='_id',
                    description='Document _id')
    version: int = Field(...,
                         alias='_version',
                         serialization_alias='_version',
                         description='Optimistic-lock version')

    @field_validator('id', mode='before')
    @classmethod
    def _set_id_if_none(cls, v):
        # Same name as MongoModel's validator, so it replaces that one: a
        # stored document's _id is read, never minted.
        return v

    @model_validator(mode='after')
    def set_id(self):
        # A stored document's _id is read, never rebuilt: replaces a
        # same-named after-validator with which a domain model may derive
        # its _id from its other fields, so the check sees the stored one.
        return self


# ──────────────────────────────────────────────
# Response models and their projections
# ──────────────────────────────────────────────


@functools.cache
def _sent_keys(model: type[BaseModel]) -> frozenset:
    """The keys a model's response carries: each field's serialized key (its
    alias, else its name)."""
    return frozenset(field.alias or name
                     for name, field in model.model_fields.items())


class ResponseModel(BaseModel):
    """Base for what a route sends: every route's responseModel, every Pub/Sub
    publisher's payload model, and every model nested in either, all the way
    down. It is never a database model (MongoModel, AppModel,
    StoredDocumentKeys, a *View), inherits from ResponseModels only, types
    its fields with allowed leaves only and declares no @computed_field
    (responseModelCheck.py: route_config, AppCreator and
    test_route_response_models.py enforce it).

    It declares exactly the keys the route sends. Under the response check
    (localdev only: validate_route_response's context) a key that is not a
    field's serialized key (its alias, else its name) is rejected as
    pydantic's extra='forbid' would (the key in the error's loc, never its
    value), at any depth and next to the other mismatches: a route that
    starts sending a field fails its tests until the model declares it.
    Production never validates a response, and a ResponseModel validated
    outside the check ignores an extra key.

    One exception to "declares exactly what it sends": a declared never-sent
    guard, `x: SkipJsonSchema[None] = None`, a key that must be absent or
    null (a field a route strips from a document before it goes out). It
    stays out of the TS type, projection_of never reads it, projectedCopy
    drops it at every depth, and the check fails a value in it, so a route
    that builds its dict without projectedCopy and leaves a value in it is
    caught in its tests. responseModelCheck.py allows a None-typed field in
    this form only."""
    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode='wrap')
    @classmethod
    def _response_declares_every_key(cls, data, handler,
                                     info: ValidationInfo):
        if not (isinstance(data, dict) and info.context
                and info.context.get(RESPONSE_CHECK_CONTEXT_KEY)):
            return handler(data)
        undeclared = [
            InitErrorDetails(type='extra_forbidden', loc=(key, ),
                             input=data[key]) for key in data
            if key not in _sent_keys(cls)
        ]
        if not undeclared:
            return handler(data)
        try:
            handler(data)
        except ValidationError as e:
            # Reported together with the other mismatches, nested ones
            # included. An error pydantic cannot rebuild (a custom type) is
            # raised alone: the undeclared keys then show once it is fixed.
            try:
                combined = ValidationError.from_exception_data(
                    cls.__name__, e.errors() + undeclared)
            except Exception:
                raise e from None
            raise combined from None
        raise ValidationError.from_exception_data(cls.__name__, undeclared)

    @model_validator(mode='before')
    @classmethod
    def _coerce_empty_strings(cls, data):
        return _coerce_empty_strings_to_none(cls, data)


# ──────────────────────────────────────────────
# Projections derived from a ResponseModel
# ──────────────────────────────────────────────
# A route reads what it sends with db.readResponse / db.readResponses, or
# db.aggregateResponses for a pipeline (mongoDb.py): they read with the
# projection the model derives and cut every document with projectedCopy.
# projection_of(Model) reads what Model declares; projectedCopy(Model, doc)
# cuts a document down to it in Python. A nested ResponseModel is read field
# by field (dotted keys) when Mongo can express it: the model itself, a union
# of models, or a list of either, none of it nullable. A nested ResponseModel
# that may be null (or a list whose items may be), at any depth, must be read
# whole, because a dotted projection drops a null or legacy-scalar value
# instead of returning it, so it comes back uncut: projection_of refuses it.
# Only the read-and-cut helpers read it whole (_projection_of_for_cut, private
# to them), because they cut every document they read; route code never
# does. Any other shape holding a ResponseModel (a dict of them, a list of
# lists, a union with a scalar) cannot be projected: projection_of raises,
# and the route reads that field itself and cuts it with projectedCopy.


def _union_of(annotation, tag=None) -> tuple:
    """`annotation` read as a union: (its members, its str tag). Annotated
    is peeled, its str tag kept (_tag_of), a RootModel read as its root (its
    root field's tag kept), and a nested union's members join the outer
    one's, so the standard optional form
    `Annotated[A | B, Field(discriminator='kind')] | None` is the union
    (A, B, None) tagged 'kind'. An inner union's tag is carried outward only
    through None: beside any other member (`Annotated[A | B, tag] | C`) it
    tags nothing, and the outer union is untagged. Anything else is one
    member. The check (responseModelCheck) and the cut read unions through
    this one walk."""
    while True:
        if typing.get_origin(annotation) is typing.Annotated:
            tag = _tag_of(*annotation.__metadata__) or tag
            annotation = typing.get_args(annotation)[0]
        elif isinstance(annotation, type) and issubclass(annotation, RootModel):
            root = annotation.model_fields['root']
            tag = _tag_of(root.discriminator, *root.metadata) or tag
            annotation = root.annotation
        else:
            break
    if not _is_union(annotation):
        return (annotation, ), tag
    parts = [_union_of(arg) for arg in typing.get_args(annotation)]
    if tag is None:
        others = [part for part in parts if part[0] != (type(None), )]
        if len(others) == 1:
            tag = others[0][1]
    return tuple(member for inner, _ in parts for member in inner), tag


def _members(annotation) -> tuple:
    """A union's members (_union_of), else the annotation alone."""
    return _union_of(annotation)[0]


def _model_members(members: tuple) -> list:
    """The members of a union (_union_of) that are models: what the check
    and the cut both call a union's models."""
    return [
        member for member in members
        if isinstance(member, type) and issubclass(member, BaseModel)
    ]


def _holds_a_response_model(annotation) -> bool:
    from responseModelCheck import models_in
    return any(
        issubclass(model, ResponseModel)
        or issubclass(model, RootModel) and _holds_a_response_model(
            model.model_fields['root'].annotation)
        for model in models_in(annotation))


def _may_be_null(annotation) -> bool:
    """Whether a field of `annotation`, or an item of its list, may be
    null."""
    members = _members(annotation)
    if type(None) in members:
        return True
    return any(type(None) in _members(typing.get_args(member)[0])
               for member in members
               if typing.get_origin(member) is list)


def _dotted_models(annotation) -> list:
    """The ResponseModels a non-null field is read field by field through
    (the model, a union of models, a list of either), or None when its shape
    cannot be expressed as dotted keys."""
    members = _members(annotation)
    if len(members) == 1 and typing.get_origin(members[0]) is list:
        members = _members(typing.get_args(members[0])[0])
    if all(
            isinstance(member, type) and issubclass(member, ResponseModel)
            for member in members):
        return list(members)
    return None


def projection_of(model: type[BaseModel]) -> dict:
    """The inclusion projection that reads exactly what `model` declares, by
    serialized key: the model is the one source of the shape, so a field
    added to it is read and one dropped from it stops being read. See the
    rules above; `_id`, which Mongo returns unless told not to, is excluded
    when the model does not declare it. Raises ValueError for a nested
    ResponseModel it cannot express, for one with no fields (it would read
    nothing), and for one that may be null, at any depth: read that model
    with db.readResponse(s) or db.aggregateResponses, which cut what they
    read."""
    return _projection(model, '', cutAfter=False)


def _guard_inside(annotation) -> str | None:
    """The dotted path of a never-sent guard (ResponseModel) declared by a
    model anywhere inside `annotation`, else None. A sub-document read whole
    has no projection to leave a nested guard out: projectedCopy drops it,
    and the read refuses it as well, so a guard's stored value is never
    even read."""
    from responseModelCheck import models_in
    seen = set()

    def walk(annotation, path: str) -> str | None:
        for model in models_in(annotation):
            if model in seen:
                continue
            seen.add(model)
            for name, field in model.model_fields.items():
                key = f'{path}.{field.alias or name}'
                if field.annotation is type(None):
                    return key
                found = walk(field.annotation, key)
                if found:
                    return found
        return None

    return walk(annotation, '')


def _refuse_a_guard_read_whole(model: type[BaseModel], key: str,
                               annotation) -> None:
    """ValueError when `key` of `model`, read whole, holds a never-sent guard
    at any depth (_guard_inside)."""
    guard = _guard_inside(annotation)
    if guard:
        raise ValueError(
            f'{model.__name__}: {key} is read whole and {key}{guard} is a '
            'never-sent guard inside it: its stored value would go out')


def _projection_of_for_cut(model: type[BaseModel],
                           readWhole: tuple = ()) -> dict:
    """projection_of, except that a nested ResponseModel that may be null is
    read whole, and so is each top-level key of `readWhole` (a declared
    field a legacy-shape repair needs as stored, in place of its dotted
    keys, or an undeclared key the route reads for its own use). Private to
    the read-and-cut helpers (db.readResponse, db.readResponses,
    db.aggregateResponses, db.projectionStage), which cut every document
    they read with projectedCopy; route code never calls it, so nothing
    read whole goes out uncut. A never-sent guard read whole is refused, at
    any depth: its stored value would survive the cut."""
    fields = {
        field.alias or name: field
        for name, field in model.model_fields.items()
    }
    projection = _projection(model, '', cutAfter=True)
    for key in readWhole:
        if not isinstance(key, str) or not key or '.' in key:
            raise ValueError(f'readWhole takes top-level keys, got {key!r}')
        if key in fields and fields[key].annotation is type(None):
            raise ValueError(
                f'readWhole: {key} is a never-sent guard of {model.__name__}; '
                'its stored value would go out')
        if key in fields:
            _refuse_a_guard_read_whole(model, key, fields[key].annotation)
        projection = {
            path: include
            for path, include in projection.items()
            if path != key and not path.startswith(key + '.')
        }
        projection[key] = 1
    return projection


def _projection(model: type[BaseModel], prefix: str, cutAfter: bool) -> dict:
    """projection_of's walk: `prefix` is the dotted path of a nested model,
    and cutAfter reads a nested model that may be null whole. A RootModel
    (a bare list or value) has no fields to project: it is refused."""
    if issubclass(model, RootModel):
        raise ValueError(
            f'projection_of({model.__name__}): a RootModel has no fields to '
            'project; read it and cut it with projectedCopy')
    projection = {}
    for name, field in model.model_fields.items():
        if field.annotation is type(None):
            # A never-sent guard (see ResponseModel): never read.
            continue
        key = prefix + (field.alias or name)
        if not _holds_a_response_model(field.annotation):
            projection[key] = 1
            continue
        if _may_be_null(field.annotation):
            if not cutAfter:
                raise ValueError(
                    f'projection_of({model.__name__}): {key} may be null, so '
                    'it is read whole and must be cut: read with '
                    'db.readResponse(s) or db.aggregateResponses, which cut '
                    'every document they read')
            _refuse_a_guard_read_whole(model, key, field.annotation)
            projection[key] = 1
            continue
        nested = _dotted_models(field.annotation)
        if nested is None:
            raise ValueError(
                f'projection_of({model.__name__}): {key} holds a '
                'ResponseModel in a shape a projection cannot express; read '
                'it yourself and cut it with projectedCopy')
        for nestedModel in nested:
            if not nestedModel.model_fields:
                raise ValueError(
                    f'projection_of({model.__name__}): {key} is '
                    f'{nestedModel.__name__}, which declares no field')
            projection.update(
                _projection(nestedModel, key + '.', cutAfter))
    if not prefix and '_id' not in projection:
        projection['_id'] = 0
    return projection


def projectedCopy(model: type[BaseModel], document):
    """`document` cut down to what `model` declares, at every depth: for a
    document the route did not read with projection_of(model) (one it built,
    or read whole). Nested models are cut through lists, dict values,
    Annotated and unions; a union of models is cut as the member its
    discriminator tag names (_member_for), and a value whose tag names no
    member is cut down to the keys any member declares (never sent whole:
    the response check reports it). A never-sent guard (ResponseModel) is
    dropped at every depth, in every shape walked, whatever it stores. A
    null or a legacy scalar is kept as it is. A RootModel cuts its value as
    its root (a list, a model, a union, with the root's tag). A new dict;
    the input is not mutated. Anything else that is not a dict is returned
    as is."""
    if issubclass(model, RootModel):
        return _cut(model, document)
    if not isinstance(document, dict):
        return document
    copy = {}
    for name, field in model.model_fields.items():
        key = field.alias or name
        if field.annotation is type(None):
            # A never-sent guard: never copied.
            continue
        if key in document:
            copy[key] = _cut(field.annotation, document[key],
                             _tag_of(field.discriminator, *field.metadata))
    return copy


def _str_tag(discriminator):
    """A discriminator as the name of its tag field: a str, a
    Discriminator('kind') or a Field(discriminator=...) naming one; None for
    anything else (a callable Discriminator names no field)."""
    while discriminator is not None and not isinstance(discriminator, str):
        discriminator = getattr(discriminator, 'discriminator', None)
    return discriminator


def _tag_of(*declared):
    """The first str tag among a union's declared discriminators (a field's
    own and its metadata, or an Annotated's metadata), else None."""
    return next((tag for tag in map(_str_tag, declared) if tag), None)


def _cut(annotation, value, discriminator=None):
    """`value` cut down to what `annotation` declares (see projectedCopy).
    `discriminator` tags the union `annotation` is (a field's own); a union
    nested in a list or a dict, or in an Optional, carries its tag in
    Annotated (_union_of)."""
    members, discriminator = _union_of(annotation, discriminator)
    if len(members) > 1:
        return _cut_union(members, value, discriminator)
    annotation = members[0]
    origin = typing.get_origin(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return projectedCopy(annotation, value)
    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            return value
        args = typing.get_args(annotation)
        item = args[0] if args else typing.Any
        return [_cut(item, element) for element in value]
    if origin is dict:
        args = typing.get_args(annotation)
        if not isinstance(value, dict) or len(args) != 2:
            return value
        return {k: _cut(args[1], v) for k, v in value.items()}
    return value


def _cut_union(members: tuple, value, discriminator=None):
    """`value` cut as the union member it is (_member_for). A scalar or a
    null has nothing to cut. A container no member is (a tag naming none, an
    untagged union of models, a value no member accepts) is cut down to what
    the members declare: a dict to every member model's keys, a list to
    nothing; it is never sent whole."""
    if not isinstance(value, (dict, list, tuple)):
        return value
    member = _member_for(members, value, discriminator)
    if member is not None:
        return _cut(member, value)
    if not _holds_a_response_model(typing.Union[members]):
        return value
    if isinstance(value, dict):
        kept = {}
        for model in _model_members(members):
            kept.update(projectedCopy(model, value))
        return kept
    return []


@functools.cache
def _adapter(member) -> TypeAdapter:
    return TypeAdapter(member)


def _carries_several_models(members: tuple) -> bool:
    """Whether two or more of a union's members carry models (as the member,
    a list or dict of them, ...) and they carry two or more different ones:
    a union only a str tag on its models' level can decide (list[A | B]
    with a discriminator), never validation."""
    from responseModelCheck import models_in
    carrying = [models for models in map(models_in, members) if models]
    return (len(carrying) > 1
            and len({model for models in carrying for model in models}) > 1)


def _literal_values(annotation) -> tuple:
    """The values a tag field's Literal admits, through Annotated and
    Optional."""
    if typing.get_origin(annotation) is typing.Literal:
        return typing.get_args(annotation)
    return tuple(value for arg in typing.get_args(annotation)
                 for value in _literal_values(arg))


def _tag_literals(model: type[BaseModel], tagField: str) -> tuple:
    """The values `model`'s tag field admits (its Literal's), () when it
    declares no such Literal field."""
    field = model.model_fields.get(tagField)
    return () if field is None else _literal_values(field.annotation)


def _tag_value(model: type[BaseModel], tagField: str, value: dict):
    """(the tag `value` carries for `model`, the values model's tag field
    admits): the field is named as the discriminator names it, its value
    read under its serialized key (alias, else name), as _sent_keys reads
    every field. ((), ()) when the model has no such field."""
    field = model.model_fields.get(tagField)
    if field is None:
        return (), ()
    return value.get(field.alias or tagField), _tag_literals(model, tagField)


def _member_for(members: tuple, value, discriminator=None):
    """The union member a dict or list `value` is, or None when there is
    none to name. Of members carrying two or more models, only a str
    discriminator tag names one (responseModelCheck refuses any other such
    union): the model whose tag field lists the value's tag, never one chosen
    by validation. Otherwise a list's one list member; else the one model,
    or a dict or list member, the value validates as."""
    models = _model_members(members)
    if isinstance(discriminator, str) and isinstance(value, dict):
        for member in models:
            tag, admitted = _tag_value(member, discriminator, value)
            if tag in admitted:
                return member
        return None
    if _carries_several_models(members):
        # An untagged union of several models: no member is guessed.
        return None
    containers = [
        member for member in members if typing.get_origin(member) in (
            (dict, ) if isinstance(value, dict) else (list, tuple))
    ]
    if isinstance(value, (list, tuple)) and len(containers) == 1:
        # The one list member: its items are cut one by one.
        return containers[0]
    if isinstance(value, dict) and models:
        try:
            models[0].model_validate(value)
            return models[0]
        except ValidationError:
            # Never a dict member beside a model (responseModelCheck refuses
            # the union): it would send the whole value. The value is cut to
            # the model's keys.
            return None
    for member in containers:
        try:
            _adapter(member).validate_python(value)
        except ValidationError:
            continue
        return member
    return None


def stripSecretKeys(value):
    """Recursively drop SECRET_RESPONSE_KEYS from dicts/lists. Pure; returns
    new containers, never mutates the input."""
    if isinstance(value, dict):
        return {
            k: stripSecretKeys(v)
            for k, v in value.items() if k not in SECRET_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [stripSecretKeys(v) for v in value]
    return value


class ApiResponse(BaseModel):
    message: str
    status: int
    data: Any = None
    current_user: str | None = None
    access_token: str | None = None
    jwtToken: str | None = None
    traceback: str | None = None

    @field_serializer('data')
    def _strip_secret_keys(self, value: Any) -> Any:
        """Every generated HTTP route returns via
        ApiResponse(...).model_dump(), so this is the single choke point that
        keeps SECRET_RESPONSE_KEYS (secret capability fields) out of ALL
        client-facing responses — including raw aggregation docs and any
        future endpoint. DB writes, pubsub publishing, and email building
        happen before this boundary and keep the secrets."""
        return stripSecretKeys(value)


class NoContentResponse(ResponseModel):
    """A route whose result is its status and message alone: no client code
    reads data from it (test_unused_response_fields.py), so it sends none
    and returns {}."""


# ──────────────────────────────────────────────
# Users and sign-in
# ──────────────────────────────────────────────


class User(MongoModel):
    id: str | None = Field(...,
                           alias='_id',
                           serialization_alias='_id',
                           description="Unique identifier for the user")
    name: str | None = Field(..., description="Username of the user")
    email: str = Field(..., description="Email address of the user")
    roles: UserRoles = Field(..., description="Roles assigned to the user")
    userType: str | None = Field(None, description='User type')
    createdAt: datetime.datetime = Field(...,
                                         description="Account creation date")
    needToBeLinked: bool = Field(
        False,
        description=
        "User has been created by pubSub function and needs to be linked by authentication system"
    )
    version: int = Field(1, alias='_version', serialization_alias='_version')

    @model_validator(mode='after')
    def validateRoles(self):
        if self.needToBeLinked == False:
            if self.id is None:
                raise ValueError("User id cannot be None for linked users")
            if self.name is None:
                raise ValueError("User name cannot be empty for linked users")
        return self

    def setRole(self, userType: str) -> dict:
        roleSetter = RoleSetter(userRoles=self.roles)
        if userType == 'superAdmin':
            roleSetter.setSuperAdminRoles()
            self.userType = 'superAdmin'
        else:
            roleSetter.setRole(userType)
            self.userType = userType
        return self

    def setSpecificRole(self, roleId: str, value: bool) -> dict:

        if self.userType == None:
            raise ValueError('User type not set, cannot set role')

        roleSetter = RoleSetter(userRoles=self.roles)
        roleSetter.setSpecificRole(roleId, value)
        return self

    def checkIfAuthorized(self, roleId: str) -> bool | PermissionError:
        try:
            role = self.roles.getRole(roleId)
        except ValueError:
            raise PermissionError(
                AllRoles().getSpecificRole(roleId).unauthorizedMessage)
        if role.value == False:
            raise PermissionError(
                AllRoles().getSpecificRole(roleId).unauthorizedMessage)
        return True

    def isAuthorized(self, roleId: str) -> bool:
        try:
            role = self.roles.getRole(roleId)
        except Exception:
            return AllRoles().getSpecificRole(roleId).value
        return role.value


class UserView(StoredDocumentKeys, User):
    """A Users document as stored."""


class RoleResponse(ResponseModel):
    """A role as a response sends it: roles.Role as it serializes (its
    excluded fields are never sent)."""
    id: str = Field(..., alias='_id', description='Role id')
    value: bool = Field(..., description='Role value')
    description: str = Field(..., description='Role description')


class UserResponse(ResponseModel):
    """A Users document as loginWithGoogle and devLogin send it: the client
    keeps it (client/lib/storedShapes.ts). Read with
    projection_of(UserResponse), or cut down with projectedCopy."""
    id: str | None = Field(...,
                           alias='_id',
                           description='Unique identifier for the user')
    name: str | None = Field(..., description='Username of the user')
    email: str = Field(..., description='Email address of the user')
    roles: list[RoleResponse] = Field(...,
                                      description='Roles assigned to the user')
    userType: str | None = Field(None, description='User type')
    createdAt: datetime.datetime = Field(...,
                                         description='Account creation date')

    @model_validator(mode='after')
    def _user_has_an_id(self):
        # Only the id: a user Pub/Sub created has no name until it signs in.
        if not self.id:
            raise ValueError('User id cannot be empty')
        return self


# A Firebase ID token is a JWT of about a kilobyte; anything far longer is
# refused before it reaches the verifier.
FIREBASE_ID_TOKEN_MAX_LENGTH = 4096


class FirebaseUserObject(AppModel):
    """The sign-in payload loginWithGoogle receives. Anything else the client
    sends is dropped.

    The client sends just `idToken`, and the uid, email and name are then
    taken from the VERIFIED token. The raw identity fields are honoured ONLY
    in localdev without an idToken (signing in with no Firebase project, and
    the tests); trusting them anywhere else would let a client pick whose
    account it signs into."""
    idToken: str | None = Field(
        None,
        max_length=FIREBASE_ID_TOKEN_MAX_LENGTH,
        description='Firebase ID token; the only field read outside '
        'localdev')
    uid: str | None = Field(None,
                            description='Localdev only: Firebase uid, the '
                            'Users _id')
    email: str | None = Field(None, description='Localdev only: email')
    displayName: str | None = Field(None,
                                    description='Localdev only: display name')


class DevUserRow(ResponseModel):
    """One row of devGetUserList: the fields its projection reads
    (projection_of(DevUserRow)). Each field restates User's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Users _id')
    name: str | None = Field(..., description='Username of the user')
    userType: str | None = Field(None, description='User type')


class DevUserListResponse(ResponseModel):
    users: list[DevUserRow] = Field(..., description='Every user')


# ──────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────


class RoleListResponse(ResponseModel):
    roles: list[RoleResponse] = Field(..., description='Every role')


# ──────────────────────────────────────────────
# Sample todo models: no route uses them any more. The framework's model
# tests use them as fixtures until the product models replace them.
# ──────────────────────────────────────────────


class SampleTodo(MongoModel):
    """A SampleTodos document, as createTodo builds it."""
    title: str = Field(..., min_length=1, description='What to do')
    completed: bool = Field(False, description='Whether it is done')


class SampleTodoView(StoredDocumentKeys, SampleTodo):
    """A SampleTodos document as stored."""


class SampleTodoRow(ResponseModel):
    """One row of getTodos: the fields its projection reads
    (projection_of(SampleTodoRow)). Each field restates SampleTodo's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='SampleTodos _id')
    title: str = Field(..., min_length=1, description='What to do')
    # Required: every stored todo has it, so the row always carries it.
    completed: bool = Field(..., description='Whether it is done')


class SampleTodoListResponse(ResponseModel):
    todos: list[SampleTodoRow] = Field(..., description='Every todo')


# Add your project-specific models below


# ──────────────────────────────────────────────
# Stores (one per admin) and customers
# ──────────────────────────────────────────────

STORE_NAME_MAX_LENGTH = 60
STORE_DESCRIPTION_MAX_LENGTH = 300
STORE_ADDRESS_MAX_LENGTH = 200
STORE_OPENING_HOURS_MAX_LENGTH = 100
LATITUDE_LIMIT = 90
LONGITUDE_LIMIT = 180
# The store's link name: lowercase letters and digits, single hyphens between.
STORE_SLUG_PATTERN = r'^[a-z0-9]+(-[a-z0-9]+)*$'
STORE_SLUG_MAX_LENGTH = 50
# A verified Firebase phone number is E.164: +, country code, subscriber.
PHONE_NUMBER_PATTERN = r'^\+[1-9][0-9]{6,14}$'
# The currencies a store can price in (ISO 4217 codes); PHP by default.
StoreCurrency = Literal['PHP', 'USD', 'EUR', 'GBP', 'JPY', 'SGD', 'AUD',
                        'CAD', 'HKD', 'CNY', 'KRW', 'MYR', 'THB', 'IDR',
                        'VND', 'INR', 'AED']
DEFAULT_STORE_CURRENCY = 'PHP'


class StoreLocation(AppModel):
    """Where a store is on the map, pinned by its admin."""
    latitude: float = Field(..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
                            description='Degrees north (negative: south)')
    longitude: float = Field(..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
                             description='Degrees east (negative: west)')


class StoreLocationResponse(ResponseModel):
    """A store's map location as a route sends it. Each field restates
    StoreLocation's, which test_route_response_models.py's MIRRORS keeps
    in step."""
    latitude: float = Field(..., ge=-LATITUDE_LIMIT, le=LATITUDE_LIMIT,
                            description='Degrees north (negative: south)')
    longitude: float = Field(..., ge=-LONGITUDE_LIMIT, le=LONGITUDE_LIMIT,
                             description='Degrees east (negative: west)')


class Store(MongoModel):
    """A Stores document. One per admin: ownerUserId (the admin's Users _id)
    and slug are both unique indexes (mongoDb.ensureIndexes)."""
    ownerUserId: str = Field(..., min_length=1,
                             description="The admin's Users _id")
    name: str = Field(..., min_length=1, max_length=STORE_NAME_MAX_LENGTH,
                      description='Store name shown to customers')
    slug: str = Field(..., max_length=STORE_SLUG_MAX_LENGTH,
                      pattern=STORE_SLUG_PATTERN,
                      description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str = Field(..., min_length=1,
                             max_length=STORE_DESCRIPTION_MAX_LENGTH,
                             description='What the store sells')
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description='Contact number, E.164')
    address: str = Field(..., min_length=1, max_length=STORE_ADDRESS_MAX_LENGTH,
                         description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        None, max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocation | None = Field(
        None, description='Map location; null until the admin pins it')
    createdAt: datetime.datetime = Field(..., description='When it was created')

    @field_validator('name', 'description', 'contactNumber', 'address',
                     'openingHours', mode='before')
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v


class StoreView(StoredDocumentKeys, Store):
    """A Stores document as stored."""


class Customer(MongoModel):
    """A Customers document. The _id is the customer's Firebase uid: an
    anonymous guest at first, linked to a phone at checkout (same uid)."""
    phoneNumber: str | None = Field(None, pattern=PHONE_NUMBER_PATTERN,
                                    description='Verified contact number')
    createdAt: datetime.datetime = Field(..., description='First visit')


class CustomerView(StoredDocumentKeys, Customer):
    """A Customers document as stored."""


class StoreResponse(ResponseModel):
    """A store as a route sends it. Each field restates Store's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Stores _id')
    name: str = Field(..., min_length=1, max_length=STORE_NAME_MAX_LENGTH,
                      description='Store name shown to customers')
    slug: str = Field(..., max_length=STORE_SLUG_MAX_LENGTH,
                      pattern=STORE_SLUG_PATTERN,
                      description="The store's link name, e.g. bakery-a")
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code every price of the store is in")
    description: str = Field(..., min_length=1,
                             max_length=STORE_DESCRIPTION_MAX_LENGTH,
                             description='What the store sells')
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description='Contact number, E.164')
    address: str = Field(..., min_length=1, max_length=STORE_ADDRESS_MAX_LENGTH,
                         description='Street address, as the admin typed it')
    openingHours: str | None = Field(
        ..., max_length=STORE_OPENING_HOURS_MAX_LENGTH,
        description='Opening hours, free text')
    location: StoreLocationResponse | None = Field(
        ..., description='Map location; null until the admin pins it')


class MyStoreResponse(ResponseModel):
    store: StoreResponse | None = Field(
        ..., description="The admin's store; null until it is created")


class CustomerSessionResponse(ResponseModel):
    """What startGuestSession sends: its _id becomes the JWT identity."""
    id: str = Field(..., alias='_id', description='Customers _id')
    phoneNumber: str | None = Field(..., pattern=PHONE_NUMBER_PATTERN,
                                    description='Verified contact number')
    store: StoreResponse = Field(..., description='The store being visited')


class PhoneVerifiedResponse(ResponseModel):
    phoneNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                             description='The verified contact number')


# ──────────────────────────────────────────────
# Products (each belongs to one store)
# ──────────────────────────────────────────────

PRODUCT_NAME_MAX_LENGTH = 80
PRODUCT_DESCRIPTION_MAX_LENGTH = 500
# Caps on what an admin can type: they keep a stray huge number from
# overflowing MongoDB's int64 (or a price's float precision).
PRODUCT_INVENTORY_MAX = 1_000_000
PRODUCT_PRICE_MAX = 10_000_000
# The most products one addStoreProducts call adds.
PRODUCT_BATCH_MAX = 50
# The most products one store holds.
PRODUCTS_PER_STORE_MAX = 2000


def productNameKey(name: str) -> str:
    """The key that makes a product name unique in its store: lowercased,
    with every run of whitespace collapsed to one space."""
    return ' '.join(name.lower().split())


class ReservedInventory(AppModel):
    """Units of a product an order is holding (taken out of inventory)."""
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    quantity: int = Field(..., ge=1, description='Units the order holds')


class Product(MongoModel):
    """A Products document, priced in its store's currency. Listed best
    sellers first: the (storeId, isAvailable, soldCount, name) index
    (mongoDb.ensureIndexes) serves that sort. (storeId, nameKey) is a
    unique index: one store never has two products of the same name.

    Every write to inventory or reservedInventory (an order reserving or
    releasing units included) must also $inc _version, or
    updateStoreProduct's _version lock misses it and the admin's save
    overwrites it."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    nameKey: str = Field(..., min_length=1,
                         description='productNameKey(name), unique per store')
    description: str | None = Field(
        None, max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedInventory: list[ReservedInventory] = Field(
        default_factory=list, description='Units orders are holding')
    soldCount: int = Field(0, ge=0, description='Units sold so far')
    isAvailable: bool = Field(True, description='Shown to customers')
    createdAt: datetime.datetime = Field(..., description='When it was added')


class ProductView(StoredDocumentKeys, Product):
    """A Products document as stored."""


class NewProduct(AppModel):
    """One product of an addStoreProducts call, as the admin described it.
    Types only, no limits: the route checks them (_validProduct), so a
    refusal names the product's position and never quotes the value."""
    name: str = Field(..., description='Product name')
    price: float = Field(..., description="Price in the store's currency")
    inventory: int = Field(..., description='Units in stock, free to sell')
    description: str | None = Field(None, description='What the product is')
    isAvailable: bool = Field(True, description='Shown to customers')


class ProductRow(ResponseModel):
    """One product as a customer's list sends it. Each field restates
    Product's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        None, max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')


class ProductListResponse(ResponseModel):
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[ProductRow] = Field(
        ..., description='The products, best sellers first')


class ReservedInventoryResponse(ResponseModel):
    """ReservedInventory as a route sends it. Each field restates
    ReservedInventory's, which test_route_response_models.py's MIRRORS
    keeps in step."""
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    quantity: int = Field(..., ge=1, description='Units the order holds')


class AdminProductRow(ResponseModel):
    """One product as its store's admin sees it. Each field restates
    Product's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        ..., max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedInventory: list[ReservedInventoryResponse] = Field(
        ..., description='Units orders are holding')
    isAvailable: bool = Field(..., description='Shown to customers')
    soldCount: int = Field(..., ge=0, description='Units sold so far')


class AdminProductListRow(ResponseModel):
    """One product of an admin's list: AdminProductRow with the
    reservations summed (reservedQuantity), so a row never grows with its
    open orders. Each field restates Product's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name')
    description: str | None = Field(
        ..., max_length=PRODUCT_DESCRIPTION_MAX_LENGTH,
        description='What the product is')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description="Price in the store's currency")
    inventory: int = Field(..., ge=0, le=PRODUCT_INVENTORY_MAX,
                           description='Units free to sell; 0 is sold out')
    reservedQuantity: int = Field(
        ..., ge=0, description='Units orders are holding, all orders summed')
    isAvailable: bool = Field(..., description='Shown to customers')
    soldCount: int = Field(..., ge=0, description='Units sold so far')


class AdminProductListResponse(ResponseModel):
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of every price in the list")
    products: list[AdminProductListRow] = Field(...,
                                                description='The products')


# ──────────────────────────────────────────────
# Orders (a customer's order at one store)
# ──────────────────────────────────────────────

# Where an order is:
#   pending: placed, its units held in each product's reservedInventory;
#   confirmed: the store accepted it;
#   completed: handed over, so its units count as sold;
#   cancelled: its units went back to the products' inventory.
OrderStatus = Literal['pending', 'confirmed', 'completed', 'cancelled']
# The most items one order holds (the checkout enforces it).
ORDER_ITEMS_MAX = 50


class OrderItem(AppModel):
    """One product of an order, a snapshot taken when the order is placed:
    a later change of the product's name or price never rewrites it."""
    productId: str = Field(..., min_length=1, description='The Products _id')
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name when ordered')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description='Unit price paid, in the order currency')
    quantity: int = Field(..., ge=1, description='Units ordered')


class Order(MongoModel):
    """An Orders document, read newest first: the (storeId, status,
    createdAt) and (customerId, storeId, createdAt) indexes
    (mongoDb.ensureIndexes) serve the admin's and the customer's lists.

    Every write to a product's inventory or reservedInventory an order
    makes (placing, cancelling or completing it) must also $inc the
    product's _version, or updateStoreProduct's _version lock misses it and
    the admin's save overwrites it."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    customerId: str = Field(..., min_length=1,
                            description='The Customers _id')
    items: list[OrderItem] = Field(..., min_length=1,
                                   max_length=ORDER_ITEMS_MAX,
                                   description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    status: OrderStatus = Field(..., description='Where the order is')
    createdAt: datetime.datetime = Field(..., description='When it was placed')


class OrderView(StoredDocumentKeys, Order):
    """An Orders document as stored."""


class OrderItemRow(ResponseModel):
    """One item of an order as a route sends it: OrderItem without its
    productId. Each field restates OrderItem's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    name: str = Field(..., min_length=1, max_length=PRODUCT_NAME_MAX_LENGTH,
                      description='Product name when ordered')
    price: float = Field(..., ge=0, le=PRODUCT_PRICE_MAX,
                         description='Unit price paid, in the order currency')
    quantity: int = Field(..., ge=1, description='Units ordered')


class StoreOrderRow(ResponseModel):
    """One order as its store's admin sees it. Each field restates
    Order's, which test_route_response_models.py's MIRRORS keeps in
    step."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., min_length=1,
                                      max_length=ORDER_ITEMS_MAX,
                                      description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    createdAt: datetime.datetime = Field(..., description='When it was placed')


class MyOrderRow(ResponseModel):
    """One order as the customer who placed it sees it: StoreOrderRow
    without the contact number. Each field restates Order's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Orders _id')
    status: OrderStatus = Field(..., description='Where the order is')
    items: list[OrderItemRow] = Field(..., min_length=1,
                                      max_length=ORDER_ITEMS_MAX,
                                      description='What was ordered')
    total: float = Field(..., ge=0, description='What the order costs')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the store's currency when ordered")
    createdAt: datetime.datetime = Field(..., description='When it was placed')


class StoreOrderListResponse(ResponseModel):
    orders: list[StoreOrderRow] = Field(
        ..., description='The orders, newest first')


class MyOrderListResponse(ResponseModel):
    orders: list[MyOrderRow] = Field(...,
                                     description='The orders, newest first')

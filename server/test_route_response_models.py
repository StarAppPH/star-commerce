"""Typed API responses: @route_config(responseModel=...).

Covers:
  - Enforcement: every ApiRequests route declares a responseModel that is
    an objects.py class (the only kind AppCreator emits a TS schema for),
    and validateRoutes refuses one without, while PubSubRequests push
    handlers stay exempt. A RootModel (a bare list or number) only on a
    route of CROSS_REPO_BARE_RESPONSE_ROUTES, which is pinned. On a
    publisher route, pubSubDecorator wraps route_config, so the check runs
    before the publish.
  - ResponseModels all the way down: every route's responseModel and every
    publisher's payload model, and every model nested in either, is an
    objects.ResponseModel inheriting from ResponseModels only
    (responseModelCheck.py, which also stops the generator).
  - Coverage: every route is called by a test outside pytest.raises (so the
    check ran on it), every publisher route has a payload pin (the
    publish recorder is exercised with a throwaway publisher), and no row
    list carries whole documents except the listed ones.
  - The runtime check in route_config's wrapper runs in localdev only: it
    raises there (never quoting input values, which carry customer and
    staff data) and is skipped everywhere else (nothing validated, copied or
    logged), as under the responseCheckOff fixture, and the route's own
    return value is always handed back untouched. It validates the result
    itself, with the responseCheck context, and catches a before-validator
    that mutates it. A ResponseModel rejects a key it does not declare, at
    any depth, under that context only.
  - Decoration-time rejection of non-BaseModel response models; a
    RootModel is checked like any other.
  - AppCreator: responseModel metadata, objects.py validation of response
    models and param types, the Promise<ApiResponse<Model>> return type (no
    `any` anywhere in the generated client), a rejected JWT and a failed
    fetch turned into the envelope, the discriminated
    ApiResponse union, RouteResponseModels, RouteRequestBodies, PublicRoute,
    `unknown` for untyped containers, a union array element in parentheses
    and imports limited to the models the routes reference (read from the
    routes, never by scanning the generated code). A login route never logs
    the Firebase payload in its body; every other POST logs which of its
    params it was sent (a push handler its messageId and message keys),
    every route binds its body whatever was sent, a scalar param refuses an
    object or a list, the push envelope is parsed inside the try, and a
    local run binds loopback.
    Params of every route: no datetime/date, a None default needs an
    Optional annotation, only renderable container origins, only objects.py
    classes. Schemas: a $ref with siblings is wrapped in allOf, so json2ts
    never emits a duplicate `X1` interface, nested models are closed, and no
    schema outlives its class.
  - StoredDocumentKeys rejects a null _id or a null or missing _version
    instead of defaulting one, and keeps a stored _id the domain model would
    rebuild; the empty-string coercion copies instead of mutating;
    projection_of and projectedCopy (a union of models cut only by its
    tag), the read-and-cut helpers (db.readResponse, db.readResponses,
    db.aggregateResponses), the only code that reads a nullable
    sub-document whole; the row models mirror their domain fields
    (MIRRORS).
  - utils' date param parsers are strict and never quote the value.
  - conftest's suite guard: installed before the mongoDb singletons connect,
    and no client to a non-local host can be opened.
"""

import ast
import collections.abc
import copy
import dataclasses
import datetime
import enum
import functools
import glob
import inspect
import json
import logging
import os
import re
import sys
import types
import typing
from types import SimpleNamespace
from typing import Any, List, Literal

import annotated_types
import pydantic
import pytest
from pydantic import BaseModel, RootModel, ValidationInfo, model_validator
from pydantic.json_schema import SkipJsonSchema

import AppCreator as AppCreatorModule
import objects as ObjectsModule
import route_config as RouteConfigModule
from mongoDb import db
from AppCreator import (API_RESPONSE_TS, BASIC_PY_TO_TS, CLIENT_API_DIR,
                        CLIENT_SCHEMAS_DIR, CROSS_REPO_BARE_RESPONSE_ROUTES,
                        SERVER_DIR, SERVER_SCHEMAS_DIR, STATUS_BAD_REQUEST,
                        STATUS_FORBIDDEN, STATUS_OK, AppCreator, Parameters,
                        _wrap_sibling_refs, py_annotation_to_ts)
from conftest import (apiRoutes, lastPublishedPayload, publishedPayloads,
                      routeMeta)
from objects import (RESPONSE_CHECK_CONTEXT_KEY, DevUserRow,
                     FirebaseUserObject, ResponseModel, SampleTodo,
                     SampleTodoListResponse, SampleTodoRow, SampleTodoView,
                     StoredDocumentKeys, User, UserResponse, UserView,
                     _projection_of_for_cut, projectedCopy, projection_of)
from responseModelCheck import (assertPublisherModels, models_in,
                                publisherModelProblems, responseModelProblems)
from route_config import route_config

# A value standing in for customer or staff data. It must never appear in a
# mismatch message or log record.
PII_SENTINEL = 'juan.delacruz.private@example.com'


# ----------------------------- Enforcement ----------------------------- #


def test_everyApiRoute_declaresAnObjectsResponseModel():
    routes = apiRoutes()
    assert routes
    assert all(hasattr(func, 'httpMethod') for func in routes.values())
    missing = sorted(name for name, func in routes.items()
                     if getattr(func, 'responseModel', None) is None)
    assert not missing, (
        f'{missing} declare no responseModel: add '
        '@route_config(responseModel=<objects.py model>)')
    objectsClasses = AppCreator(apiRequests=None)._objects_classes()
    foreign = sorted(name for name, func in routes.items()
                     if func.responseModel not in objectsClasses)
    assert not foreign, (
        f'{foreign} declare a responseModel that is not a top-level '
        'objects.py class, so it has no generated TS schema')


def test_responseModelsAreResponseModels():
    """Every ApiRequests route's responseModel and every Pub/Sub
    publisher's payload model (pubSubModels.collectPublishers, as AppCreator
    reads them) is a ResponseModel all the way down: AppCreator refuses to
    generate otherwise, and this catches a hand edit."""
    problems = [
        f'{name}: {problem}' for name, func in sorted(apiRoutes().items())
        for problem in responseModelProblems(func.responseModel)
    ]
    problems += publisherModelProblems(AppCreatorModule.PROJECT_ROOT)

    assert problems == []


class _StoredThing(ObjectsModule.MongoModel):
    name: str


class _EmbeddedThing(ObjectsModule.AppModel):
    name: str


class _ThingResponse(ResponseModel):
    name: str


class _HoldsAStoredThing(ResponseModel):
    things: list[_StoredThing]
    maybe: dict[str, _EmbeddedThing] | None = None
    fine: _ThingResponse


class _InheritsAStoredThing(ResponseModel, _StoredThing):
    pass


class _BareThings(RootModel[list[_StoredThing]]):
    pass


def test_responseModelProblems_holdForAResponseModelTree():
    assert responseModelProblems(SampleTodoListResponse) == []
    assert responseModelProblems(UserResponse) == []


def test_responseModelProblems_nameEveryDatabaseModelWithItsFileAndLine():
    """A database model anywhere in the tree (a list item, a dict value,
    an Optional) is named with its path, file and line; a ResponseModel
    beside it is not."""
    problems = responseModelProblems(_HoldsAStoredThing)

    assert len(problems) == 2
    assert problems[0].startswith('_HoldsAStoredThing.things: _StoredThing (')
    assert problems[1].startswith(
        '_HoldsAStoredThing.maybe: _EmbeddedThing (')
    line = inspect.getsourcelines(_StoredThing)[1]
    assert f'test_route_response_models.py:{line})' in problems[0]
    assert all(
        problem.endswith('is not a ResponseModel') for problem in problems)


@pytest.mark.parametrize('model',
                         [SampleTodoView, UserView, User, FirebaseUserObject])
def test_responseModelProblems_refuseADatabaseModel(model):
    assert responseModelProblems(model)[0].startswith(
        f'{model.__name__}: {model.__name__} (')


def test_responseModelProblems_refuseAResponseModelInheritingADatabaseModel():
    problems = responseModelProblems(_InheritsAStoredThing)

    assert any('inherits _StoredThing (' in problem for problem in problems)
    assert any('inherits MongoModel (' in problem for problem in problems)


def test_responseModelProblems_lookThroughABareRootModel():
    assert responseModelProblems(_BareThings)[0].startswith(
        '_BareThings: _StoredThing (')


class _ThingDict(typing.TypedDict):
    name: str


@dataclasses.dataclass
class _ThingData:
    name: str


@pytest.mark.parametrize('annotation, leaf', [
    (Any, 'Any'),
    (dict, 'dict'),
    (list, 'list'),
    (typing.List, 'list'),
    (object, 'object'),
    (_ThingDict, '_ThingDict'),
    (_ThingData, '_ThingData'),
    (tuple[int, ...], 'tuple'),
    (set[str], 'set'),
    (dict[int, str], 'dict[int, str]'),
    (_ThingResponse | dict, 'dict'),
    (list[dict[str, Any]] | None, 'Any'),
    (typing.Annotated[Any, pydantic.Field(description='x')], 'Any'),
    (list[typing.Annotated[dict, pydantic.Field(min_length=1)]], 'dict'),
])
def test_responseModelProblems_refuseALeafThatCouldCarryAWholeDocument(
        annotation, leaf):
    """Only scalars, datetime, date, Literal, Enum and ResponseModels, through
    Optional, unions, list and dict[str, ...]: anything else could carry a
    whole document past the undeclared-key check."""
    model = pydantic.create_model('_Loose', __base__=ResponseModel,
                                  loose=(annotation, ...))

    problems = responseModelProblems(model)

    assert len(problems) == 1
    assert problems[0].startswith('_Loose.loose: _Loose (')
    assert f'types it with {leaf}, which is no allowed response leaf' in (
        problems[0])


class _BadRoot(RootModel[list[Any]]):
    pass


def test_responseModelProblems_refuseARootModelHoldingABadLeaf():
    assert 'holds Any, which is no allowed response leaf' in (
        responseModelProblems(_BadRoot)[0])
    model = pydantic.create_model('_HoldsABadRoot', __base__=ResponseModel,
                                  rows=(_BadRoot, ...))
    assert any('holds Any' in problem
               for problem in responseModelProblems(model))


class _HiddenRow(ResponseModel):
    n: int


@pytest.mark.parametrize('annotation, default', [
    (SkipJsonSchema[str], None),
    (int | SkipJsonSchema[None], None),
    (list[SkipJsonSchema[_HiddenRow]], ...),
    (typing.Annotated[str, pydantic.Field(exclude=True)], None),
    (typing.Annotated[str, pydantic.Field(
        json_schema_extra=lambda schema: schema.clear())], None),
])
def test_responseModelProblems_refuseAFieldHiddenFromTheTsType(
        annotation, default):
    """A field sent but invisible to the TS type (and so to the
    unused-response-field gate) is refused, however it hides."""
    model = pydantic.create_model('_Hiding', __base__=ResponseModel,
                                  hidden=(annotation, default))

    problems = responseModelProblems(model)

    assert problems and all(
        problem.startswith('_Hiding.hidden: _Hiding (')
        for problem in problems)
    assert any('hides it from the TS type' in problem
               or 'excluded from, or rewrites' in problem
               for problem in problems)


def test_responseModelProblems_allowASecretKeyHiddenFromTheTsType(
        monkeypatch):
    """A SECRET_RESPONSE_KEYS key is stripped by every egress, so it may be
    hidden from the TS type; the same field under another name may not."""
    import AppConfig
    monkeypatch.setattr(AppConfig, 'SECRET_RESPONSE_KEYS',
                        frozenset({'claimToken'}))

    def hiding(name):
        return pydantic.create_model('_HidesASecret', __base__=ResponseModel,
                                     **{name: (SkipJsonSchema[str], None)})

    assert responseModelProblems(hiding('claimToken')) == []
    assert any('hides it from the TS type' in problem
               for problem in responseModelProblems(hiding('claimTokens')))


class _RewritesItsSchema(ResponseModel):
    model_config = pydantic.ConfigDict(
        json_schema_extra=lambda schema: schema['properties'].clear())

    name: str


def test_responseModelProblems_refuseAClassLevelCallableJsonSchemaExtra():
    problems = responseModelProblems(_RewritesItsSchema)

    assert len(problems) == 1
    assert problems[0].startswith('_RewritesItsSchema: _RewritesItsSchema (')
    assert 'rewrites its JSON schema (a callable json_schema_extra)' in (
        problems[0])


class _TagOne(ResponseModel):
    kind: Literal['one']
    a: int


class _TagTwo(ResponseModel):
    kind: Literal['two']
    b: int


class _UntaggedEither(ResponseModel):
    either: _TagOne | _TagTwo


def _byKind(value):
    return value.get('kind') if isinstance(value, dict) else value.kind


_TaggedByCall = (typing.Annotated[_TagOne, pydantic.Tag('one')]
                 | typing.Annotated[_TagTwo, pydantic.Tag('two')])


@pytest.mark.parametrize('annotation, default', [
    (_TagOne | _TagTwo, ...),
    (_TagOne | _TagTwo | None, None),
    (list[_TagOne | _TagTwo], ...),
    (dict[str, _TagOne | _TagTwo | str], ...),
    (list[typing.Annotated[_TagOne | _TagTwo,
                           pydantic.Field(description='x')]], ...),
    (_TaggedByCall, pydantic.Field(...,
                                   discriminator=pydantic.Discriminator(_byKind))),
    (list[typing.Annotated[_TaggedByCall,
                           pydantic.Discriminator(_byKind)]], ...),
    # Models carried by other members can carry no tag at all.
    (list[_TagOne] | list[_TagTwo], ...),
    (list[_TagOne] | _TagTwo | None, None),
    (_TagOne | list[_TagTwo], ...),
    (dict[str, list[_TagOne] | list[_TagTwo]], ...),
])
def test_responseModelProblems_refuseAnUntaggedUnionOfModels(
        annotation, default):
    """A field that can be one of two or more ResponseModels (at any depth)
    declares a str discriminator on a union of the models themselves, so the
    member a value is cut as is the one its tag names; a callable
    discriminator is no str tag, and a union carrying its models in other
    members (a list of each, a dict) can carry none."""
    model = pydantic.create_model('_Either', __base__=ResponseModel,
                                  either=(annotation, default))

    problems = responseModelProblems(model)

    assert len(problems) == 1
    assert problems[0].startswith('_Either.either: _Either (')
    assert 'untagged union of models (_TagOne | _TagTwo)' in problems[0]


@pytest.mark.parametrize('annotation', [
    _TagOne | dict[str, str],
    _TagOne | dict[str, str] | None,
    _TagOne | dict[str, _TagOne],
    list[_TagOne | dict[str, int]],
])
def test_responseModelProblems_refuseAModelUnionedWithADict(annotation):
    """Both accept a dict: a value that fails the model would go out whole
    as the dict, and the check would pass it."""
    model = pydantic.create_model('_ModelOrDict', __base__=ResponseModel,
                                  either=(annotation, ...))

    problems = responseModelProblems(model)

    assert len(problems) == 1
    assert problems[0].startswith('_ModelOrDict.either: _ModelOrDict (')
    assert 'a union of a model and a dict (_TagOne | dict)' in problems[0]


class _RootUntagged(RootModel[list[_TagOne | _TagTwo]]):
    pass


class _RootTagged(RootModel[list[typing.Annotated[
        _TagOne | _TagTwo, pydantic.Field(discriminator='kind')]]]):
    pass


def test_projectedCopy_neverSendsADictValueAsTheDictBesideAModel():
    """Backstop to the refusal above: a legacy document that fails the model
    is cut to the model's keys, never sent whole as the dict member."""
    model = pydantic.create_model('_ModelOrDict', __base__=ResponseModel,
                                  either=(_TagOne | dict[str, str], ...))
    probe = {'kind': 'a', 'passwordHash': 'h', 'email': 'e'}

    assert projectedCopy(model, {'either': probe}) == {
        'either': {
            'kind': 'a'
        }
    }


def test_responseModelProblems_holdARootModelsRootToTheUnionRule():
    problems = responseModelProblems(_RootUntagged)

    assert len(problems) == 1
    assert problems[0].startswith('_RootUntagged: _RootUntagged (')
    assert 'holds an untagged union of models (_TagOne | _TagTwo)' in (
        problems[0])
    assert responseModelProblems(_RootTagged) == []


@pytest.mark.parametrize('annotation', [
    typing.Union[typing.Annotated[_TagOne | _TagTwo,
                                  pydantic.Field(discriminator='kind')],
                 _ThingResponse],
    typing.Annotated[_TagOne | _TagTwo,
                     pydantic.Field(discriminator='kind')]
    | list[_ThingResponse],
])
def test_responseModelProblems_carryAnInnerTagOutwardOnlyThroughNone(
        annotation):
    """A tagged union beside another member (a model, a list of them) is no
    tagged union: a value of the other member would be cut to every
    member's keys."""
    model = pydantic.create_model('_Beside', __base__=ResponseModel,
                                  either=(annotation, ...))

    problems = responseModelProblems(model)

    assert len(problems) == 1
    assert ('untagged union of models (_TagOne | _TagTwo | _ThingResponse)'
            in problems[0])


def test_unionProblems_refuseATaggedMemberWithoutTheTagLiteral():
    """pydantic refuses such a member where it builds the discriminated
    union; the check refuses it wherever the walk reads a tag."""
    from responseModelCheck import _unionProblems
    problems = _unionProblems(_TagOne | _ThingResponse, 'kind')

    assert problems == [
        "a union tagged 'kind' whose member _ThingResponse declares no "
        "Literal 'kind' field: a value of it would be cut to every member's "
        'keys'
    ]
    assert _unionProblems(_TagOne | _TagTwo, 'kind') == []


def test_responseModelProblems_countAModelBesideADictOfAnotherTwice():
    model = pydantic.create_model('_ModelOrDict', __base__=ResponseModel,
                                  either=(_TagOne | dict[str, _TagTwo], ...))

    problems = responseModelProblems(model)

    assert len(problems) == 2
    assert 'untagged union of models (_TagOne | _TagTwo)' in problems[0]
    assert 'a union of a model and a dict' in problems[1]


class _RootOwnTag(RootModel[_TagOne | _TagTwo]):
    root: _TagOne | _TagTwo = pydantic.Field(..., discriminator='kind')


class _HoldsARootMember(ResponseModel):
    held: _RootOwnTag | None = None


def test_rootModel_tagsItsRootAndIsReadAsAUnionMember():
    """A RootModel's own Field(discriminator=) tags its root, also when it
    is a member of a field's union; an untagged one is refused there too."""
    assert responseModelProblems(_RootOwnTag) == []
    assert responseModelProblems(_HoldsARootMember) == []
    assert projectedCopy(_HoldsARootMember, {
        'held': {
            'kind': 'two',
            'a': 1,
            'b': 2
        }
    }) == {'held': {'kind': 'two', 'b': 2}}
    untagged = pydantic.create_model('_HoldsAnUntaggedRoot',
                                     __base__=ResponseModel,
                                     held=(_RootUntagged | None, None))
    assert any('untagged union of models (_TagOne | _TagTwo)' in problem
               for problem in responseModelProblems(untagged))


def test_responseModelProblems_nameAnUntaggedUnionWithItsFileAndLine():
    problems = responseModelProblems(_UntaggedEither)

    line = inspect.getsourcelines(_UntaggedEither)[1]
    assert len(problems) == 1
    assert problems[0].startswith('_UntaggedEither.either: _UntaggedEither (')
    assert f'test_route_response_models.py:{line})' in problems[0]


@pytest.mark.parametrize('annotation, default', [
    (_TagOne | _TagTwo, pydantic.Field(..., discriminator='kind')),
    (_TagOne | _TagTwo | None, pydantic.Field(None, discriminator='kind')),
    (list[typing.Annotated[_TagOne | _TagTwo,
                           pydantic.Field(discriminator='kind')]], ...),
    (dict[str, typing.Annotated[_TagOne | _TagTwo,
                                pydantic.Discriminator('kind')]], ...),
    (typing.Annotated[_TagOne | _TagTwo, pydantic.Discriminator('kind')], ...),
    (_TagOne | _TagTwo,
     pydantic.Field(..., discriminator=pydantic.Discriminator('kind'))),
    (_TagOne | str | None, None),
    (list[_TagOne] | _TagOne | None, None),
    (list[typing.Annotated[_TagOne | _TagTwo,
                           pydantic.Field(discriminator='kind')]] | None, None),
])
def test_responseModelProblems_acceptATaggedUnionOrASingleModel(
        annotation, default):
    model = pydantic.create_model('_Either', __base__=ResponseModel,
                                  either=(annotation, default))

    assert responseModelProblems(model) == []


@pytest.mark.parametrize('field', [
    pydantic.Field(..., serialization_alias='totalSent'),
    pydantic.Field(..., validation_alias='totalRead'),
    pydantic.Field(...,
                   validation_alias=pydantic.AliasChoices('total', 'sum')),
])
def test_responseModelProblems_refuseAnotherSerializationOrValidationAlias(
        field):
    """One key per field (its alias, else its name), so the check, the
    projections and the TS type always agree on it."""
    model = pydantic.create_model('_Realiased', __base__=ResponseModel,
                                  total=(int, field))

    assert any("alias other than 'total'" in problem
               for problem in responseModelProblems(model))


class _Colour(enum.Enum):
    RED = 'red'


@pytest.mark.parametrize('annotation', [
    str, int, float, bool, datetime.datetime, datetime.date, int | None,
    Literal['a', 'b'], _Colour, _ThingResponse, list[_ThingResponse],
    dict[str, list[int | None]], _ThingResponse | str | None,
    typing.Annotated[list[_ThingResponse], pydantic.Field(min_length=1)]
])
def test_responseModelProblems_acceptTheAllowedLeaves(annotation):
    model = pydantic.create_model('_Typed', __base__=ResponseModel,
                                  typed=(annotation, ...))

    assert responseModelProblems(model) == []


class _WithAComputedThing(ResponseModel):
    name: str

    @pydantic.computed_field
    @property
    def stored(self) -> _StoredThing:
        return _StoredThing(name=self.name)


class _Guarded(ResponseModel):
    name: str
    stripped: SkipJsonSchema[None] = None


def test_neverSentGuard_isAllowedNeverReadDroppedAndFailsAValue():
    """`SkipJsonSchema[None] = None` declares a key that never goes out:
    allowed, out of the TS type, never read, dropped by projectedCopy, and a
    route that builds its dict without projectedCopy and leaves a value in
    it fails the check."""
    assert responseModelProblems(_Guarded) == []
    assert 'stripped' not in _Guarded.model_json_schema(
        mode='serialization')['properties']
    assert projection_of(_Guarded) == {'name': 1, '_id': 0}
    assert projectedCopy(_Guarded, {
        'name': 'x',
        'stripped': 'secret'
    }) == {'name': 'x'}
    with pytest.raises(pydantic.ValidationError):
        ObjectsModule.validate_route_response(_Guarded, {
            'name': 'x',
            'stripped': 'secret'
        })
    ObjectsModule.validate_route_response(_Guarded, {'name': 'x'})


def test_routeReturningAGuardWithoutTheCut_failsTheCheck(monkeypatch):
    """The strict check still catches a guard value a route sends without
    projectedCopy."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    api = _api_returning({'name': 'x', 'stripped': PII_SENTINEL},
                         model=_Guarded)

    with pytest.raises(ValueError) as raised:
        api.fn()

    message = str(raised.value)
    assert '_Guarded' in message and 'stripped' in message
    assert PII_SENTINEL not in message


@pytest.mark.parametrize('annotation, default', [
    (None, None),
    (SkipJsonSchema[None], ...),
    (typing.Annotated[None, annotated_types.Predicate(bool)], None),
    (typing.Annotated[None, pydantic.Field(exclude=True)], None),
])
def test_responseModelProblems_allowANoneFieldOnlyAsANeverSentGuard(
        annotation, default):
    model = pydantic.create_model('_NoneField', __base__=ResponseModel,
                                  nothing=(annotation, default))

    problems = responseModelProblems(model)

    assert problems[0].startswith('_NoneField.nothing: _NoneField (')
    assert 'the only None-typed field is a never-sent guard' in problems[0]


def test_responseModelProblems_refuseAComputedField():
    """A ResponseModel sends only declared fields, and a computed field was
    also the one path to a database model the walk could miss."""
    problems = responseModelProblems(_WithAComputedThing)

    assert problems == [
        problem for problem in problems
        if problem.startswith('_WithAComputedThing.stored: ')
    ]
    assert 'declares it a @computed_field' in problems[0]


def test_modelsIn_findsEveryModelThroughContainers():
    assert models_in(dict[str, list[_ThingResponse | None]] | _StoredThing
                     | tuple[_EmbeddedThing, ...]) == [
                         _ThingResponse, _StoredThing, _EmbeddedThing
                     ]


def _undecorated(responseModel):
    """A route function as route_config would mark it, without route_config's
    own check (which would refuse a bad model at decoration)."""

    def fn(self) -> dict:
        return {}

    fn.httpMethod, fn.jwtRequired, fn.createAccessToken = 'POST', False, False
    fn.successMessage, fn.roleAccess = 'Success', None
    fn.responseModel = responseModel
    return fn


def test_routeConfig_refusesADatabaseModelInAResponseAtDecoration():
    """The import fails, naming the route and every offender."""
    with pytest.raises(ValueError,
                       match='(?s)responseModel _HoldsAStoredThing of fn must '
                       'be a ResponseModel all the way down.*'
                       '_HoldsAStoredThing.things: _StoredThing'):

        class Api:

            @route_config(httpMethod='POST', responseModel=_HoldsAStoredThing)
            def fn(self) -> dict:
                return {}


def test_validateRoutes_refusesADatabaseModelInAResponse(monkeypatch):
    """A generation error, before anything is written, naming the nested
    model; a push handler's model is exempt."""

    class Api:
        fn = _undecorated(_HoldsAStoredThing)

    realClasses = AppCreator(apiRequests=None)._objects_classes()
    monkeypatch.setattr(AppCreator, '_objects_classes',
                        lambda self: realClasses + [_HoldsAStoredThing])
    creator = _creator_with_routes(monkeypatch,
                                   app={'fn': Api.fn},
                                   appPubSub={})
    with pytest.raises(ValueError,
                       match='(?s)fn in ApiRequests must be a ResponseModel '
                       'all the way down.*_HoldsAStoredThing.things: '
                       '_StoredThing'):
        creator.validateRoutes()

    _creator_with_routes(monkeypatch, app={},
                         appPubSub={'fn': Api.fn}).validateRoutes()


def test_publisherModelProblems_nameThePublisher(monkeypatch):
    import pubSubModels
    spec = SimpleNamespace(anchor='server/x.py::makeThing',
                           model=_StoredThing)
    monkeypatch.setattr(pubSubModels, 'collectPublishers',
                        lambda root: [spec])

    assert publisherModelProblems(AppCreatorModule.PROJECT_ROOT) == [
        f'server/x.py::makeThing: {problem}'
        for problem in responseModelProblems(_StoredThing)
    ]


def test_assertPublisherModels_stopsAppCreatorNamingThePublisher(
        monkeypatch):
    """AppCreator's step after the Pub/Sub manifest: a payload model that
    breaks the rule stops the run; a sound one lets it go on."""
    import pubSubModels
    spec = SimpleNamespace(anchor='server/x.py::makeThing',
                           model=_StoredThing)
    monkeypatch.setattr(pubSubModels, 'collectPublishers',
                        lambda root: [spec])

    with pytest.raises(SystemExit) as caught:
        assertPublisherModels(AppCreatorModule.PROJECT_ROOT)
    assert str(caught.value).startswith(
        'Pub/Sub payload models must be ResponseModels all the way down')
    assert 'server/x.py::makeThing: _StoredThing: _StoredThing (' in str(
        caught.value)

    spec.model = SampleTodoRow
    assertPublisherModels(AppCreatorModule.PROJECT_ROOT)
    assert 'assertPublisherModels(PROJECT_ROOT)' in (
        AppCreatorModule.PROJECT_ROOT / 'server' / 'AppCreator.py').read_text(
            encoding='utf-8')


def test_validateRoutes_passesForTheRealRoutes():
    """The check the generator runs before writing anything: every
    ApiRequests route has a model and passes the param guards, and a
    PubSubRequests handler that declares none is let through (the exemption
    means it MAY omit one)."""
    pubSubHandlers = AppCreator(
        apiRequests=None)._get_user_defined_methods('appPubSub')
    assert pubSubHandlers
    assert any(
        getattr(func, 'responseModel', None) is None
        for func in pubSubHandlers.values())
    AppCreator(apiRequests=None).validateRoutes()


class _UntypedRoutes:

    @route_config(httpMethod='POST')
    def fn(self) -> dict:
        return {}


class _TypedRouteWithLegacyParam:

    @route_config(httpMethod='POST', responseModel=UserResponse)
    def fn(self, projection: dict) -> dict:
        return {}


class _TypedRoutesWithDateParams:

    @route_config(httpMethod='POST', responseModel=UserResponse)
    def withDatetime(self, startDate: datetime.datetime) -> dict:
        return {}

    @route_config(httpMethod='POST', responseModel=UserResponse)
    def withOptionalDate(self, endDate: datetime.date | None = None) -> dict:
        return {}


def _creator_with_routes(monkeypatch, app: dict,
                         appPubSub: dict) -> AppCreator:
    routesByKind = {'app': app, 'appPubSub': appPubSub}
    monkeypatch.setattr(AppCreator, '_get_user_defined_methods',
                        lambda self, kind: routesByKind[kind])
    return AppCreator(apiRequests=None)


def test_validateRoutes_rejectsAnApiRouteWithoutResponseModel(monkeypatch):
    creator = _creator_with_routes(monkeypatch,
                                   app={'fn': _UntypedRoutes.fn},
                                   appPubSub={})
    with pytest.raises(ValueError,
                       match='fn in ApiRequests declares no responseModel'):
        creator.validateRoutes()


def test_validateRoutes_exemptsAPubSubHandlerWithoutResponseModel(
        monkeypatch):
    """A push handler's return value only goes back to Pub/Sub, never to
    the typed client."""
    creator = _creator_with_routes(monkeypatch,
                                   app={},
                                   appPubSub={'fn': _UntypedRoutes.fn})
    creator.validateRoutes()


def test_validateRoutes_guardsTheParamsOfEveryRoute(monkeypatch):
    """A bare dict param fails the guards."""
    creator = _creator_with_routes(
        monkeypatch, app={'fn': _TypedRouteWithLegacyParam.fn}, appPubSub={})
    with pytest.raises(ValueError, match="parameter 'projection'"):
        creator.validateRoutes()


@pytest.mark.parametrize('route', ['withDatetime', 'withOptionalDate'])
def test_validateRoutes_rejectsADateParam(monkeypatch, route):
    """A date or datetime param fails generation before anything is written:
    app.py hands the route the raw JSON string. Dates are `str` ISO params
    parsed in the route."""
    creator = _creator_with_routes(
        monkeypatch,
        app={route: getattr(_TypedRoutesWithDateParams, route)},
        appPubSub={})
    with pytest.raises(ValueError, match=r"Annotate it `str`"):
        creator.validateRoutes()


# ----------------------------- Bare responses ----------------------------- #


def test_crossRepoBareResponseRoutes_areExactlyTheRootModelRoutes():
    """A RootModel response (a bare list or number, which no route but a
    cross-repo one may answer) is declared by exactly the allowlisted
    routes, each listed with its consumers: an entry cannot outlive its
    route's RootModel, and a new one cannot slip in unlisted. A new project
    has none."""
    bare = {
        name: func.responseModel
        for name, func in apiRoutes().items()
        if issubclass(func.responseModel, RootModel)
    }

    assert bare == {}
    assert set(CROSS_REPO_BARE_RESPONSE_ROUTES) == set(bare)
    assert all(reason.strip()
               for reason in CROSS_REPO_BARE_RESPONSE_ROUTES.values())


class _PartnerInventory(RootModel[list[int]]):
    """A bare list, as a route other services read as `data` would answer."""


class _BareRoutes:

    @route_config(httpMethod='POST', responseModel=_PartnerInventory)
    def notCrossRepo(self) -> list:
        return []

    @route_config(httpMethod='POST', responseModel=_PartnerInventory)
    def viewInventoryForPartnerService(self) -> list:
        return []


def test_validateRoutes_rejectsARootModelOutsideTheAllowlist(monkeypatch):
    """A generation error, before anything is written: every other route
    answers a dict with explicit keys. An allowlisted route passes."""
    monkeypatch.setattr(
        AppCreatorModule, 'CROSS_REPO_BARE_RESPONSE_ROUTES', {
            'viewInventoryForPartnerService':
            'a bare list: partner job_syncInventory.py reads data["data"]'
        })
    realClasses = AppCreator(apiRequests=None)._objects_classes()
    monkeypatch.setattr(AppCreator, '_objects_classes',
                        lambda self: realClasses + [_PartnerInventory])
    creator = _creator_with_routes(
        monkeypatch,
        app={'notCrossRepo': _BareRoutes.notCrossRepo},
        appPubSub={})
    with pytest.raises(ValueError,
                       match='notCrossRepo in ApiRequests is a RootModel'):
        creator.validateRoutes()

    creator = _creator_with_routes(monkeypatch,
                                   app={
                                       'viewInventoryForPartnerService':
                                       _BareRoutes.viewInventoryForPartnerService
                                   },
                                   appPubSub={})
    creator.validateRoutes()


def test_routeConfig_checksARootModelResponse(monkeypatch):
    """route_config checks a bare list like any response: it passes as
    is, and a mismatch raises in localdev."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    result = [1, 2]

    assert _api_returning(result, model=_RootList).fn() is result
    with pytest.raises(ValueError, match='0: Input should be a valid integer'):
        _api_returning(['x'], model=_RootList).fn()


# ----------------------------- Publishers ----------------------------- #


def _decorator_name(decorator) -> str:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return target.attr if isinstance(target, ast.Attribute) else target.id


def test_publisherRoutes_checkTheResponseBeforePublishing():
    """pubSubDecorator must be the outer decorator: route_config's check
    then runs before the publish, so a response that fails it in localdev
    is never published. Swapped, the payload would go out first."""
    with open(os.path.join(str(SERVER_DIR), 'ApiRequests.py'),
              encoding='utf-8') as f:
        tree = ast.parse(f.read())
    publishers = set()
    for routeClass in tree.body:
        if not isinstance(routeClass, ast.ClassDef):
            continue
        for node in routeClass.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            names = [_decorator_name(d) for d in node.decorator_list]
            if 'pubSubDecorator' not in names:
                continue
            publishers.add(node.name)
            assert 'route_config' in names
            assert (names.index('pubSubDecorator') <
                    names.index('route_config')), (
                        f'{node.name}: put @pubSubDecorator above '
                        '@route_config')

    assert publishers == {
        name
        for name, func in apiRoutes().items()
        if hasattr(func, 'projectNameConsumers')
    }


# The routes no test can call outside pytest.raises, with the reason. A route
# leaves this set when it is fixed and gets a test of its own.
KNOWN_UNCALLABLE_ROUTES = set()


def _scanCallsAndPins(source: str) -> tuple[set, set]:
    """The names `source` calls as `<x>.<name>(...)` outside a
    `with pytest.raises` block, and the string literals it passes to
    lastPublishedPayload(...). Only calls count (an attribute read such as
    `ApiRequests.<route>.successMessage` does not); a service-client method
    sharing a route's name is an accepted false positive."""
    called, pinned = set(), set()

    class _Visitor(ast.NodeVisitor):

        def __init__(self):
            self.insideRaises = 0

        def visit_With(self, node):
            raises = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == 'raises'
                for item in node.items)
            self.insideRaises += raises
            self.generic_visit(node)
            self.insideRaises -= raises

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and not self.insideRaises:
                called.add(node.func.attr)
            if (isinstance(node.func, ast.Name)
                    and node.func.id == 'lastPublishedPayload' and node.args
                    and isinstance(node.args[0], ast.Constant)):
                pinned.add(node.args[0].value)
            self.generic_visit(node)

    _Visitor().visit(ast.parse(source))
    return called, pinned


@functools.lru_cache(maxsize=None)
def _calledRoutesAndPins() -> tuple[frozenset, frozenset]:
    """_scanCallsAndPins over every test file."""
    called, pinned = set(), set()
    for path in glob.glob(os.path.join(str(SERVER_DIR), 'test_*.py')):
        with open(path, encoding='utf-8') as f:
            fileCalled, filePinned = _scanCallsAndPins(f.read())
        called |= fileCalled
        pinned |= filePinned
    return frozenset(called), frozenset(pinned)


def test_coverageScan_countsOnlyWhatProvesARouteOrAPin():
    """The scan the coverage tests rely on: a call inside pytest.raises, an
    attribute read and a pin whose route is not a literal prove nothing."""
    called, pinned = _scanCallsAndPins("""
import pytest
api.calledRoute(1)
with pytest.raises(ValueError):
    api.raisingRoute(2)
ApiRequests.readOnlyRoute.successMessage
lastPublishedPayload('pinnedRoute')
lastPublishedPayload(routeName)
publishedPayloads('notAPin')
""")

    assert 'calledRoute' in called
    assert 'raisingRoute' not in called
    assert 'readOnlyRoute' not in called
    assert pinned == {'pinnedRoute'}


def test_everyApiRoute_isCalledOutsidePytestRaises():
    """Every route is called by some test that expects it to succeed, so
    route_config's localdev check (which raises) ran on its response."""
    called, _ = _calledRoutesAndPins()

    assert set(apiRoutes()) - called == KNOWN_UNCALLABLE_ROUTES


def test_everyPublisherRoute_hasAPayloadPin():
    """What a publisher route returns IS the message its consumers
    receive: every one has a test reading lastPublishedPayload('<route>')
    (and comparing it with the response)."""
    _, pinned = _calledRoutesAndPins()
    publishers = {
        name
        for name, func in apiRoutes().items()
        if hasattr(func, 'projectNameConsumers')
    }

    assert publishers - pinned == set()


def _samplePublisher():
    """A throwaway publisher route, decorated as a real one is
    (@pubSubDecorator outside @route_config). Built inside a function: a
    module-level one would register with pubSub's topic registry at
    import."""
    from pubSub import pubSubDecorator
    # Any registered service will do as the consumer (`create-star-app link
    # --remove-example` replaces exampleService with a real sibling).
    from services.AllServices import serviceGetter
    consumer = serviceGetter.listOfServices[0]

    class Api:

        @pubSubDecorator(projectNameConsumers=[consumer])
        @route_config(httpMethod='POST', responseModel=SampleTodoRow)
        def publishSampleTodo(self, title: str, publishToProdPubSub: bool,
                              publishToPubSubMockDb: bool = False) -> dict:
            return projectedCopy(
                SampleTodoRow,
                SampleTodo(title=title, _id='rrm-published',
                           _version=0).model_dump(by_alias=True))

    return Api()


def test_publishedPayload_isWhatThePublisherRouteReturned(monkeypatch):
    """The conftest recorder hands a test the payload the consumers
    receive, so a publisher's pin compares it with the response; the shared
    pubSubMockDb is never written."""
    # The throwaway publisher registers with pubSub's topic registry; a copy
    # keeps it out of the suite's later registry checks (test_pubSubModels).
    import pubSub
    monkeypatch.setattr(pubSub, '_pubsubTopicRegistry',
                        list(pubSub._pubsubTopicRegistry))
    api = _samplePublisher()

    res = api.publishSampleTodo('Rrm title', publishToProdPubSub=False)

    assert lastPublishedPayload('publishSampleTodo') == res
    assert publishedPayloads('publishSampleTodo') == [res]
    assert publishedPayloads('publishSample') == []


# A route row list must not carry whole documents: a whole document per row
# makes the payload, and the response check, grow with every row. Which
# ResponseModels are whole documents is derived (_isWholeDocument), never
# listed by hand. (responseModel, dotted path of the row list) -> who needs
# whole documents there, and why. Every other row list is a narrowed row
# model (projection_of). Empty in a new project.
WHOLE_DOCUMENT_ROWS = {}


def _modelsIn(annotation, inList=False):
    """(insideAList, model) for every BaseModel an annotation holds, seen
    through Optional/unions, dict values and RootModels."""
    origin = typing.get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        for arg in typing.get_args(annotation):
            yield from _modelsIn(arg, True)
    elif origin is dict:
        yield from _modelsIn(typing.get_args(annotation)[1], inList)
    elif origin is not None:
        for arg in typing.get_args(annotation):
            yield from _modelsIn(arg, inList)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if issubclass(annotation, RootModel):
            yield from _modelsIn(annotation.model_fields['root'].annotation,
                                 inList)
        else:
            yield inList, annotation


def _storedKeys(model) -> dict:
    """{serialized key: field} of the fields a stored model writes (an
    excluded field is never stored)."""
    return {
        field.alias or name: field
        for name, field in model.model_fields.items() if not field.exclude
    }


def _holdsADocumentPart(annotation) -> bool:
    """A field that is no scalar: a nested model, a list or a dict."""
    return bool(models_in(annotation)) or any(
        origin in (list, dict, tuple)
        for origin in (typing.get_origin(arg)
                       for arg in (annotation, *typing.get_args(annotation))))


def _unwrapped(models: list) -> list:
    """The models, a RootModel replaced by the models of its root."""
    return [
        inner for model in models for inner in (_unwrapped(
            models_in(model.model_fields['root'].annotation)) if issubclass(
                model, RootModel) else [model])
    ]


def _copiesWhole(response, stored) -> bool:
    """`response` declares every key `stored` writes, and every nested
    stored model is copied whole by one of the response's nested models."""
    sent = {
        field.alias or name: field
        for name, field in response.model_fields.items()
    }
    for key, field in _storedKeys(stored).items():
        if key not in sent:
            return False
        storedNested = _unwrapped(models_in(field.annotation))
        sentNested = _unwrapped(models_in(sent[key].annotation))
        if not all(
                any(_copiesWhole(r, s) for r in sentNested)
                for s in storedNested):
            return False
    return True


def _isWholeDocument(model) -> bool:
    """Whether a ResponseModel sends a stored document whole: for a stored
    model (a MongoModel in objects.py) with a field that is no scalar, it
    copies every such field whole (no nested key left out, recursively) and
    at least half of the scalar ones. Leaving out a scalar (_version) alone
    still sends the document whole; a document of scalars only is never
    heavy.

    A heuristic, with blind spots: a stored model that is not a MongoModel
    in objects.py (a sibling's, a dict collection) is not considered; a
    document of scalars only is never flagged, however many there are; a
    row that renames the stored keys, or nests the document under another
    key, is not matched; and a narrowed nested model counts as narrowed even
    when what it leaves out is tiny."""
    if not issubclass(model, ResponseModel):
        return False
    sent = {
        field.alias or name: field
        for name, field in model.model_fields.items()
    }
    for stored in _storedModels():
        fields = _storedKeys(stored)
        parts = {
            key for key, field in fields.items()
            if _holdsADocumentPart(field.annotation)
        }
        scalars = set(fields) - parts
        if not parts or not parts <= set(sent):
            continue
        if not all(
                any(
                    _copiesWhole(r, s)
                    for r in _unwrapped(models_in(sent[key].annotation)))
                for key in parts
                for s in _unwrapped(models_in(fields[key].annotation))):
            continue
        if len(scalars & set(sent)) * 2 >= len(scalars):
            return True
    return False


@functools.cache
def _storedModels() -> tuple:
    return tuple(cls for cls in AppCreator(apiRequests=None)._objects_models()
                 if issubclass(cls, ObjectsModule.MongoModel))


class _RoleWithoutDescription(ResponseModel):
    id: str = pydantic.Field(..., alias='_id')
    value: bool


def test_isWholeDocument_isDerivedFromTheStoredModels():
    """A user with its roles is a whole Users document; a row without them
    is not, nor is a row of a scalars-only document, one that copies the
    roles but under half of the scalars, or one whose roles are cut
    down."""
    assert _isWholeDocument(UserResponse)
    assert not _isWholeDocument(DevUserRow)
    assert not _isWholeDocument(SampleTodoRow)
    assert not _isWholeDocument(User)
    rolesOnly = pydantic.create_model(
        '_RolesOnly',
        __base__=ResponseModel,
        id=(str, pydantic.Field(..., alias='_id')),
        roles=(list[ObjectsModule.RoleResponse], ...))
    assert not _isWholeDocument(rolesOnly)
    cutRoles = pydantic.create_model(
        '_CutRoles',
        __base__=UserResponse,
        roles=(list[_RoleWithoutDescription], ...))
    assert not _isWholeDocument(cutRoles)


def _wholeDocumentRows(responseModel) -> set:
    """The row lists of `responseModel` whose rows are, or directly carry, a
    whole domain document.

    The rule: walk down from the response model through its single
    (non-list) sub-models only. Each list of models reached is a row list,
    flagged when its row model is a whole document or has a DIRECT field
    holding one; the walk does not go into the rows. It never descends into
    a whole-document model either, and a responseModel that is itself one
    (UserResponse, ...) is skipped: the lists INSIDE the one
    document a route returns (a user's roles) are that document's own
    fields, not rows."""
    found = set()

    def visit(model, path, seen):
        if model in seen or _isWholeDocument(model):
            return
        if issubclass(model, RootModel):
            fields = {'': model.model_fields['root']}
        else:
            fields = {
                field.alias or name: field
                for name, field in model.model_fields.items()
            }
        for key, field in fields.items():
            fieldPath = '.'.join(part for part in (path, key) if part)
            for inList, sub in _modelsIn(field.annotation):
                if not inList:
                    visit(sub, fieldPath, seen | {model})
                elif _isWholeDocument(sub) or any(
                        _isWholeDocument(rowField)
                        for f in sub.model_fields.values()
                        for _, rowField in _modelsIn(f.annotation)):
                    found.add(fieldPath)

    visit(responseModel, '', frozenset())
    return found


def test_routeRowLists_neverCarryWholeDocuments():
    """A list route sends narrowed rows, never whole documents. The few
    lists that do are listed with who needs them; a stale entry fails
    too."""
    responseModels = {func.responseModel for func in apiRoutes().values()}

    found = {(model.__name__, path)
             for model in responseModels
             for path in _wholeDocumentRows(model)}

    assert found == set(WHOLE_DOCUMENT_ROWS)


# ----------------------------- Throwaway models ----------------------------- #


class _Line(ResponseModel):
    qty: int


class _Shape(ResponseModel):
    note: str | None = None
    count: int
    lines: list[_Line] = []


class _CrashingShape(ResponseModel):
    count: int

    @model_validator(mode='before')
    @classmethod
    def _crash(cls, data):
        raise KeyError('validator bug')


def _api_returning(result, model=_Shape):

    class Api:

        @route_config(httpMethod='POST', responseModel=model)
        def fn(self) -> dict:
            return result

    return Api()


def _assertCheckSkipped(monkeypatch, caplog, result, model=_Shape):
    """The route hands back `result` itself, unchanged, and the response
    check neither validated nor copied it, nor logged anything. The caller
    sets ENVIRONMENT (or requests responseCheckOff)."""
    calls = []
    monkeypatch.setattr(RouteConfigModule, 'validate_route_response',
                        lambda *args, **kwargs: calls.append('validate'))
    monkeypatch.setattr(
        RouteConfigModule, 'copy',
        SimpleNamespace(
            deepcopy=lambda value, *args: calls.append('deepcopy') or value))
    snapshot = copy.deepcopy(result)

    with caplog.at_level(logging.WARNING):
        out = _api_returning(result, model=model).fn()

    assert calls == []
    assert out is result
    assert out == snapshot
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ----------------------------- route_config check ----------------------------- #


def test_routeConfig_storesResponseModel():

    class Api:

        @route_config(httpMethod='POST', responseModel=_Shape)
        def typed(self) -> dict:
            return {'count': 1}

        @route_config(httpMethod='POST')
        def untyped(self) -> dict:
            return {}

    assert Api.typed.responseModel is _Shape
    assert Api.untyped.responseModel is None


def test_matchingResponse_returnsSameObjectUntouched(monkeypatch):
    """Validation coerces '3' -> 3 on what it validates; the caller must
    still get exactly what the route built."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    result = {'note': '', 'count': '3', 'lines': [{'qty': '2'}]}
    snapshot = copy.deepcopy(result)

    out = _api_returning(result).fn()

    assert out is result
    assert out == snapshot


def test_mismatch_raisesInLocaldev(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    api = _api_returning({'count': PII_SENTINEL, 'lines': [{}]})

    with pytest.raises(ValueError) as excinfo:
        api.fn()

    message = str(excinfo.value)
    assert 'fn' in message
    assert '_Shape' in message
    assert 'count' in message
    assert 'lines.0.qty' in message
    assert PII_SENTINEL not in message
    # The pydantic error quotes input values, so it is not chained on.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_undeclaredKey_raisesInLocaldevAtAnyDepth(monkeypatch):
    """A ResponseModel declares exactly what the route sends: a key it does
    not declare fails the check, named and never quoted, at the top and in a
    nested model alike, next to the other mismatches."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    with pytest.raises(ValueError) as excinfo:
        _api_returning({
            'count': PII_SENTINEL,
            'secretNote': PII_SENTINEL,
            'lines': [{
                'qty': 1,
                'unitCost': PII_SENTINEL
            }]
        }).fn()

    message = str(excinfo.value)
    assert 'count: Input should be a valid integer' in message
    assert 'secretNote: Extra inputs are not permitted' in message
    assert 'lines.0.unitCost: Extra inputs are not permitted' in message
    assert PII_SENTINEL not in message


def test_undeclaredKey_isRejectedUnderTheResponseCheckOnly():
    """Only the response check forbids an undeclared key: a ResponseModel
    validated anywhere else ignores it, as pydantic does by default."""
    assert _Shape.model_validate({'count': 1, 'extra': 'x'}).count == 1
    with pytest.raises(pydantic.ValidationError,
                       match='Extra inputs are not permitted'):
        ObjectsModule.validate_route_response(_Shape, {
            'count': 1,
            'extra': 'x'
        })


class _AliasedThing(ResponseModel):
    thingId: str = pydantic.Field(..., alias='_id')
    label: str


def test_undeclaredKey_acceptsOnlyTheSerializedKey():
    """A response carries each field under its serialized key: the alias if
    there is one, else the name. A field's name when it has an alias is an
    undeclared key, and so is any other key."""
    sent = {'_id': 'rrm-thing', 'label': 'Rrm'}
    ObjectsModule.validate_route_response(_AliasedThing, sent)
    assert sent == _AliasedThing.model_validate(sent).model_dump(by_alias=True)
    assert responseModelProblems(_AliasedThing) == []

    for key, renamed in (('_id', 'thingId'), ('label', 'caption')):
        wrong = {renamed if k == key else k: v for k, v in sent.items()}
        with pytest.raises(pydantic.ValidationError,
                           match=f'{renamed}\n  Extra inputs are not '
                           'permitted'):
            ObjectsModule.validate_route_response(_AliasedThing, wrong)


def test_mismatch_namesAUnionMemberWithoutPydanticsValidatorSteps():
    assert RouteConfigModule._loc(
        ('hold', 'function-wrap[_response_declares_every_key()]', 'kind',
         0)) == 'hold.kind.0'


def test_mismatch_inLocaldev_summarisesLongErrorLists(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    api = _api_returning({'count': 1, 'lines': [{}] * 25})

    with pytest.raises(ValueError, match='and 15 more'):
        api.fn()


@pytest.mark.parametrize('result', [['not', 'a', 'dict'], None])
def test_mismatch_inLocaldev_nonDictReturnNamesRoot(monkeypatch, result):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    with pytest.raises(ValueError) as excinfo:
        _api_returning(result).fn()

    assert '<root>' in str(excinfo.value)


@pytest.mark.parametrize('environment', ['localprod', 'clouddev', 'cloudprod'])
def test_mismatch_skippedOutsideLocaldev(monkeypatch, caplog, environment):
    """Outside localdev the check never runs, because validating every
    response would cost production time: a mismatching response goes out
    exactly as the route built it."""
    monkeypatch.setenv('ENVIRONMENT', environment)

    _assertCheckSkipped(monkeypatch, caplog,
                        {'note': PII_SENTINEL, 'count': PII_SENTINEL})


def test_crashingValidator_skippedOutsideLocaldev(monkeypatch, caplog):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')

    _assertCheckSkipped(monkeypatch, caplog, {'count': 1},
                        model=_CrashingShape)


def test_crashingValidator_raisesInLocaldev(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    with pytest.raises(ValueError, match='validation crashed with KeyError'):
        _api_returning({'count': 1}, model=_CrashingShape).fn()


def test_responseCheckOff_fixtureTurnsTheCheckOff(responseCheckOff,
                                                   monkeypatch, caplog):
    _assertCheckSkipped(monkeypatch, caplog, {'count': PII_SENTINEL})


def test_routeWithoutResponseModel_isNotChecked(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    result = ['not', 'a', 'dict']

    class Api:

        @route_config(httpMethod='POST')
        def fn(self) -> dict:
            return result

    assert Api().fn() is result


# A payload published outside its route (a hand-publish site), or by a
# publisher that is no route, is held to the topic's payload model too:
# route_config.check_route_response.


def _noRoutePublisher(monkeypatch, hatch: dict):
    """A publisher that is no route, in a fake module whose
    PUBSUB_PAYLOAD_MODELS is `hatch`."""
    module = types.ModuleType('fakeNoRoutePublishers')
    module._Shape, module.PUBSUB_PAYLOAD_MODELS = _Shape, hatch

    def makeFakeThing(payload: dict) -> dict:
        return payload

    makeFakeThing.__module__ = module.__name__
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return makeFakeThing


def test_checkRouteResponse_holdsAPayloadToTheRouteResponseModel(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    route = type(_api_returning({})).fn
    RouteConfigModule.check_route_response(route, {'count': 1})
    with pytest.raises(ValueError, match='fn returned a response that does not match its '
                                         'responseModel _Shape') as excinfo:
        RouteConfigModule.check_route_response(route, {'count': PII_SENTINEL})
    assert PII_SENTINEL not in str(excinfo.value)


def test_checkRouteResponse_readsAPublisherModelFromItsModule(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    publisher = _noRoutePublisher(monkeypatch, {'makeFakeThing': '_Shape'})
    RouteConfigModule.check_route_response(publisher, {'count': 1})
    with pytest.raises(ValueError, match='makeFakeThing returned a response that does not match its '
                                         'responseModel _Shape'):
        RouteConfigModule.check_route_response(publisher, {'count': 'many'})


@pytest.mark.parametrize('hatch', [{}, {'makeOtherThing': '_Shape'}, {'makeFakeThing': '_Missing'}])
def test_checkRouteResponse_refusesAPublisherWithoutAModel(monkeypatch, hatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    with pytest.raises(ValueError, match='makeFakeThing has no payload model: give its @route_config a '
                                         'responseModel, or add it to PUBSUB_PAYLOAD_MODELS in '
                                         'fakeNoRoutePublishers'):
        RouteConfigModule.check_route_response(_noRoutePublisher(monkeypatch, hatch), {'count': 1})


def test_checkRouteResponse_skippedOutsideLocaldev(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'cloudprod')
    RouteConfigModule.check_route_response(_noRoutePublisher(monkeypatch, {}), {'count': PII_SENTINEL})


def test_roleAccess_stillRunsBeforeTheRoute(monkeypatch):
    """The check wraps the route call; the role gate before it is
    unchanged, so an unknown caller never reaches the route."""
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    called = []

    class Api:

        @route_config(httpMethod='POST',
                      jwtRequired=True,
                      roleAccess='canUpdateUserRole',
                      responseModel=_Shape)
        def fn(self, userId: str) -> dict:
            called.append(userId)
            return {'count': 1}

    with pytest.raises(ValueError, match='User not found'):
        Api().fn(userId='rrm-no-such-user')
    assert called == []


def test_routeConfig_refusesRoleAccessWithoutAJwt():
    """The role check reads the user the body's userId names; only a JWT
    route ties that userId to the caller, so on a public route anyone
    could name an admin. Every roleAccess route is a JWT route."""
    with pytest.raises(ValueError,
                       match='fn: roleAccess needs jwtRequired=True'):

        class Api:

            @route_config(httpMethod='POST',
                          roleAccess='canUpdateUserRole',
                          responseModel=_Shape)
            def fn(self, userId: str) -> dict:
                return {'count': 1}

    assert not [
        name for name, func in apiRoutes().items()
        if func.roleAccess is not None and not func.jwtRequired
    ]


# ----------------------- No copy, context, mutation guard ----------------------- #


class _MutatingShape(ResponseModel):
    """The bug the localdev guard exists for: a before-validator that edits
    the dict it is handed instead of returning a new one."""
    count: int

    @model_validator(mode='before')
    @classmethod
    def _mutate(cls, data):
        data['count'] = 7
        return data


def _recording_model(seen: list):

    class _Recording(ResponseModel):
        count: int

        @model_validator(mode='before')
        @classmethod
        def _record(cls, data, info: ValidationInfo):
            seen.append((data, info.context))
            return data

    return _Recording


def test_mutatingBeforeValidator_raisesInLocaldev(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')

    with pytest.raises(ValueError,
                       match='before-validators must copy, not mutate'):
        _api_returning({'count': 1}, model=_MutatingShape).fn()


def test_check_validatesTheResultWithTheResponseCheckContext(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'localdev')
    seen = []
    result = {'count': 1}

    assert _api_returning(result, model=_recording_model(seen)).fn() is result

    assert len(seen) == 1
    assert seen[0][0] is result
    assert seen[0][1] == {RESPONSE_CHECK_CONTEXT_KEY: True}


@pytest.mark.parametrize('environment', ['localprod', 'clouddev', 'cloudprod'])
def test_check_neverValidatesNorCopiesOutsideLocaldev(monkeypatch, caplog,
                                                      environment):
    """Validation and the deepcopy are the check's whole cost; outside
    localdev neither runs, so no validator ever sees the response."""
    monkeypatch.setenv('ENVIRONMENT', environment)
    seen = []

    _assertCheckSkipped(monkeypatch, caplog, {'count': 1},
                        model=_recording_model(seen))

    assert seen == []


def test_emptyStringCoercion_copiesInsteadOfMutating():
    """'' becomes None in a `str | None` field, on a copy: the input (a
    response the check validates) is never rewritten, and an input with
    nothing to coerce is passed through as is."""
    payload = {'idToken': '', 'uid': 'u1', 'email': '', 'extra': ''}
    snapshot = copy.deepcopy(payload)

    built = FirebaseUserObject.model_validate(payload)

    assert built.idToken is None
    assert built.email is None
    assert built.uid == 'u1'
    assert payload == snapshot
    untouched = {'uid': 'u1'}
    assert ObjectsModule._coerce_empty_strings_to_none(
        FirebaseUserObject, untouched) is untouched


def test_objectsBeforeValidators_neverMutateTheirInput():
    """The before-validators objects.py declares (the empty-string coercion
    on MongoModel, AppModel and ResponseModel, and ResponseModel's
    undeclared-key check) return a new value."""
    stored = {'_id': 'rrm-todo', '_version': 0, 'title': 'Rrm', 'note': ''}
    snapshot = copy.deepcopy(stored)
    SampleTodoView.model_validate(stored)
    assert stored == snapshot
    sent = {
        '_id': 'rrm-user',
        'name': '',
        'email': 'rrm-user@test.com',
        'roles': [],
        'userType': '',
        'createdAt': '2026-01-01T00:00:00Z'
    }
    snapshot = copy.deepcopy(sent)
    assert ObjectsModule.validate_route_response(UserResponse,
                                                 sent).name is None
    assert sent == snapshot


# ----------------------------- Stored documents ----------------------------- #


def _storedUser() -> dict:
    from roles import AllRoles, UserRoles
    return User(_id='rrm-user',
                name='Rrm User',
                email='rrm-user@test.com',
                createdAt=datetime.datetime.now(datetime.timezone.utc),
                roles=UserRoles(AllRoles().getAllRoles()),
                _version=0).model_dump(by_alias=True)


@pytest.mark.parametrize('view, domain, doc', [
    (UserView, User, _storedUser()),
    (SampleTodoView, SampleTodo, {
        '_id': 'rrm-todo',
        '_version': 2,
        'title': 'Rrm',
        'completed': False
    }),
])
def test_storedDocumentView_rejectsANullIdOrANullOrMissingVersion(
        view, domain, doc):
    """A stored document is read, so its *View never defaults a _version
    or mints an _id the way the domain model does for a new document."""
    view.model_validate(doc)
    nullId = doc | {'_id': None}
    nullVersion = doc | {'_version': None}
    noVersion = {key: value for key, value in doc.items() if key != '_version'}

    for bad in (nullVersion, noVersion):
        with pytest.raises(pydantic.ValidationError):
            view.model_validate(bad)
    with pytest.raises(pydantic.ValidationError):
        view.model_validate(nullId)
    # The domain model still builds a new document without a _version.
    assert domain.model_validate(noVersion).version == domain.model_fields[
        'version'].default


def test_storedDocumentKeys_comesFirstInAView():
    """Its required _id/_version win over the domain model's defaults."""
    for view, domain in ((UserView, User), (SampleTodoView, SampleTodo)):
        assert view.model_fields['version'].is_required()
        assert view.model_fields['id'].is_required()
        assert view.__mro__.index(StoredDocumentKeys) < view.__mro__.index(
            domain)


# ----------------------------- Projections ----------------------------- #


class _ProjectedLine(ResponseModel):
    qty: int
    unitId: str = pydantic.Field(..., alias='_id')


class _ProjectedNote(ResponseModel):
    text: str


class _RowWithLines(ResponseModel):
    lines: list[_ProjectedLine]
    oneLine: _ProjectedLine
    either: _ProjectedLine | _ProjectedNote
    whole: list[int]


class _RowWithNullables(ResponseModel):
    maybeLine: _ProjectedLine | None = None
    lines: list[_ProjectedLine | None]
    maybeLines: list[_ProjectedLine] | None = None
    mixed: _ProjectedNote | str | None = None


def test_projectionOf_readsNestedResponseModelsFieldByField():
    """A nested ResponseModel (alone, in a list, in a union) is read field by
    field; a list of scalars whole. The row declares no _id, so Mongo is told
    not to return it."""
    assert projection_of(_RowWithLines) == {
        'lines.qty': 1,
        'lines._id': 1,
        'oneLine.qty': 1,
        'oneLine._id': 1,
        'either.qty': 1,
        'either._id': 1,
        'either.text': 1,
        'whole': 1,
        '_id': 0,
    }


def test_projectionOf_readsAFieldThatMayBeNullWholeOnlyForACut():
    """A dotted projection drops a null (or a legacy scalar) instead of
    returning it, so a nested model that may be null, or a list whose items
    may be, is read whole, which leaves it uncut: projection_of refuses it
    unless the read cuts what it reads (_projection_of_for_cut, private to
    the read-and-cut helpers)."""
    with pytest.raises(ValueError,
                       match='maybeLine may be null, so it is read whole and '
                       'must be cut'):
        projection_of(_RowWithNullables)
    assert _projection_of_for_cut(_RowWithNullables) == {
        'maybeLine': 1,
        'lines': 1,
        'maybeLines': 1,
        'mixed': 1,
        '_id': 0,
    }


class _HoldsNullables(ResponseModel):
    inner: _RowWithNullables


def test_projectionOf_refusesANullableModelUnderANonNullOne():
    """The refusal reaches every depth: a nested model that may be null,
    inside one that may not, is refused too."""
    with pytest.raises(ValueError,
                       match='inner.maybeLine may be null, so it is read '
                       'whole and must be cut'):
        projection_of(_HoldsNullables)
    assert _projection_of_for_cut(_HoldsNullables) == {
        'inner.maybeLine': 1,
        'inner.lines': 1,
        'inner.maybeLines': 1,
        'inner.mixed': 1,
        '_id': 0,
    }


def test_projectionOf_hasNoWayToSkipTheCut():
    """Route code cannot read a nullable sub-document whole: projection_of
    takes the model alone."""
    assert list(inspect.signature(projection_of).parameters) == ['model']
    with pytest.raises(TypeError):
        projection_of(_RowWithNullables, cutAfter=True)


def _handWrittenServerSources() -> list:
    """The project's hand-written server modules, tests and conftest
    aside."""
    skipped = {'venv', '.venv', 'node_modules', '__pycache__', 'schemas'}
    sources = []
    for folder, dirs, files in os.walk(SERVER_DIR):
        dirs[:] = [d for d in dirs if d not in skipped]
        sources += [
            os.path.join(folder, name) for name in files
            if name.endswith('.py') and not name.startswith('test')
            and name != 'conftest.py'
        ]
    return sources


def test_cutProjection_isReachedOnlyThroughTheReadAndCutHelpers():
    """Only db.readResponse(s) and db.aggregateResponses read a nullable
    sub-document whole, because they cut every document they read: no other
    module reaches the cut projection, and none passes a cut flag.
    The objects package's __init__ may name them in its re-export imports
    only."""
    defining = {
        os.path.normcase(os.path.abspath(path)) for path in (
            inspect.getsourcefile(ObjectsModule._projection_of_for_cut),
            inspect.getsourcefile(type(db).aggregateResponses))
    }
    reExports = os.path.normcase(os.path.abspath(ObjectsModule.__file__))
    reach = re.compile(r'\b(_projection_of_for_cut|_projection|cutAfter)\b')
    reaching = []
    for path in _handWrittenServerSources():
        path = os.path.normcase(os.path.abspath(path))
        if path in defining:
            continue
        with open(path, encoding='utf-8') as source:
            text = source.read()
        importLines = set()
        if path == reExports:
            importLines = {
                number
                for node in ast.parse(text).body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for number in range(node.lineno, node.end_lineno + 1)
            }
        reaching += [
            f'{path}:{number}'
            for number, line in enumerate(text.splitlines(), 1)
            if reach.search(line) and number not in importLines
        ]
    assert len(defining) == 2
    assert reaching == []


class _Empty(ResponseModel):
    pass


@pytest.mark.parametrize('annotation', [
    dict[str, _ProjectedLine],
    list[list[_ProjectedLine]],
    _ProjectedLine | str,
    tuple[_ProjectedLine, ...],
    _Empty,
])
def test_projectionOf_refusesWhatItCannotNarrow(annotation):
    """Never a silent whole read of a nested ResponseModel that no copy
    would cut: the route reads such a field itself and cuts it with
    projectedCopy."""
    model = pydantic.create_model('_Unprojectable', __base__=ResponseModel,
                                  things=(annotation, ...))

    with pytest.raises(ValueError,
                       match='projection_of\\(_Unprojectable\\): things'):
        projection_of(model)


PROBE_COLLECTION = 'RrmProjectionProbe'


def test_projectionOf_keepsANullAndALegacyScalarThatMongoWouldDrop():
    """Against the database: a nullable sub-document stored as null, or as a
    legacy scalar, arrives as stored (a dotted projection would have left
    the key out), and projectedCopy keeps it (a null and a legacy scalar,
    never a guard's value)."""
    db.delete({}, PROBE_COLLECTION)
    assert db.readResponse(_RowWithNullables, {'_id': 'rrm-none'},
                           PROBE_COLLECTION) is None
    try:
        db.create(
            {
                '_id': 'rrm-null',
                '_version': 0,
                'maybeLine': None,
                'lines': [None, {'qty': 1, '_id': 'u1', 'cost': 2}],
                'maybeLines': None,
                'mixed': 'legacy',
                'secret': 1,
            }, PROBE_COLLECTION)
        db.create(
            {
                '_id': 'rrm-scalar',
                '_version': 0,
                'maybeLine': 'legacy',
                'lines': [],
                'maybeLines': [{'qty': 2, '_id': 'u2', 'cost': 3}],
                'mixed': {'text': 'x', 'cost': 4},
            }, PROBE_COLLECTION)

        rows = sorted(db.readResponses(_RowWithNullables, {},
                                       PROBE_COLLECTION),
                      key=lambda row: str(row.get('maybeLine')))

        assert rows == [
            {
                'maybeLine': None,
                'lines': [None, {'qty': 1, '_id': 'u1'}],
                'maybeLines': None,
                'mixed': 'legacy',
            },
            {
                'maybeLine': 'legacy',
                'lines': [],
                'maybeLines': [{'qty': 2, '_id': 'u2'}],
                'mixed': {'text': 'x'},
            },
        ]
    finally:
        db.delete({}, PROBE_COLLECTION)


@pytest.mark.parametrize('flag', ['findOne', 'countOnly'])
def test_readResponses_refusesASingleOrACountRead(flag):
    with pytest.raises(ValueError, match=f'{flag} is refused'):
        db.readResponses(SampleTodoRow, {}, PROBE_COLLECTION, **{flag: True})


def _probeRows() -> None:
    db.delete({}, PROBE_COLLECTION)
    db.create(
        {
            '_id': 'rrm-a',
            '_version': 0,
            'maybeLine': {'qty': 1, '_id': 'u1', 'cost': 2},
            'lines': [],
            'mixed': {'text': 'x', 'cost': 4},
            'secret': 1,
        }, PROBE_COLLECTION)
    db.create(
        {
            '_id': 'rrm-b',
            '_version': 0,
            'maybeLine': None,
            'lines': [{'qty': 2, '_id': 'u2', 'cost': 3}],
            'mixed': 'legacy',
        }, PROBE_COLLECTION)


def test_readResponse_readsOneDocumentCutOrNone():
    _probeRows()
    try:
        assert db.readResponse(_RowWithNullables, {'_id': 'rrm-a'},
                               PROBE_COLLECTION) == {
            'maybeLine': {'qty': 1, '_id': 'u1'},
            'lines': [],
            'mixed': {'text': 'x'},
        }
        assert db.readResponse(_RowWithNullables, {'_id': 'rrm-none'},
                               PROBE_COLLECTION) is None
    finally:
        db.delete({}, PROBE_COLLECTION)


def test_aggregateResponses_projectsAndCutsEveryRow(monkeypatch):
    """The pipeline ends with the model's projection (the caller's list
    untouched) and every row, a sub-document read whole included, is cut."""
    _probeRows()
    stages = []
    aggregate = db.aggregate

    def spy(pipeline, *args, **kwargs):
        stages.extend(pipeline)
        return aggregate(pipeline, *args, **kwargs)

    monkeypatch.setattr(db, 'aggregate', spy)
    pipeline = [{'$addFields': {'added': 1}}, {'$sort': {'_id': 1}}]
    try:
        rows = db.aggregateResponses(_RowWithNullables, pipeline,
                                     PROBE_COLLECTION)
    finally:
        db.delete({}, PROBE_COLLECTION)

    assert rows == [
        {
            'maybeLine': {'qty': 1, '_id': 'u1'},
            'lines': [],
            'mixed': {'text': 'x'},
        },
        {
            'maybeLine': None,
            'lines': [{'qty': 2, '_id': 'u2'}],
            'mixed': 'legacy',
        },
    ]
    assert pipeline == [{'$addFields': {'added': 1}}, {'$sort': {'_id': 1}}]
    assert stages == [
        *pipeline, {'$project': _projection_of_for_cut(_RowWithNullables)}
    ]


def test_readHelpers_passTheirProjectionAndSessionToTheRead(monkeypatch):
    calls = []

    def read(query, collection, **kwargs):
        calls.append((query, collection, kwargs))
        row = {'text': 'x', 'cost': 1}
        return row if kwargs.get('findOne') else [row]

    monkeypatch.setattr(db, 'read', read)
    session = object()

    assert db.readResponse(_ProjectedNote, {'_id': 'a'}, PROBE_COLLECTION,
                           session) == {'text': 'x'}
    assert db.readResponses(_ProjectedNote, {}, PROBE_COLLECTION,
                            session=session, limit=2) == [{'text': 'x'}]
    projection = {'text': 1, '_id': 0}
    assert calls == [
        ({'_id': 'a'}, PROBE_COLLECTION, {
            'projection': projection,
            'session': session,
            'findOne': True
        }),
        ({}, PROBE_COLLECTION, {
            'projection': projection,
            'session': session,
            'limit': 2
        }),
    ]


def _prepared(doc: dict) -> dict:
    return {**doc, 'text': doc.pop('extra')}


def test_readHelpers_forwardReadWholeAndPrepare(monkeypatch):
    calls = []

    def read(query, collection, **kwargs):
        calls.append(kwargs)
        row = {'text': 'x', 'extra': 'prepared', 'cost': 1}
        return row if kwargs.get('findOne') else [row]

    monkeypatch.setattr(db, 'read', read)

    assert db.readResponse(_ProjectedNote, {}, PROBE_COLLECTION,
                           readWhole=('extra', ),
                           prepare=_prepared) == {'text': 'prepared'}
    assert db.readResponses(_ProjectedNote, {}, PROBE_COLLECTION,
                            readWhole=('extra', ),
                            prepare=_prepared) == [{'text': 'prepared'}]
    projection = {'text': 1, '_id': 0, 'extra': 1}
    assert calls == [{
        'projection': projection,
        'findOne': True
    }, {
        'projection': projection
    }]


def test_aggregateResponses_forwardsItsSessionReadWholeAndPrepare(
        monkeypatch):
    calls = []

    def aggregate(pipeline, collection, **kwargs):
        calls.append((pipeline, kwargs))
        return [{'text': 'x', 'extra': 'prepared', 'cost': 1}]

    monkeypatch.setattr(db, 'aggregate', aggregate)
    session = object()

    assert db.aggregateResponses(_ProjectedNote, [], PROBE_COLLECTION,
                                 session, readWhole=('extra', ),
                                 prepare=_prepared) == [{'text': 'prepared'}]
    assert calls == [([{
        '$project': {
            'text': 1,
            '_id': 0,
            'extra': 1
        }
    }], {
        'session': session
    })]


def test_readResponse_aMissRunsNoPrepare(monkeypatch):
    monkeypatch.setattr(db, 'read', lambda *args, **kwargs: None)

    def prepare(doc):
        raise AssertionError('prepare ran on a miss')

    assert db.readResponse(_ProjectedNote, {'_id': 'none'}, PROBE_COLLECTION,
                           prepare=prepare) is None


def test_readWhole_readsTopLevelKeysWholeInPlaceOfTheirDottedKeys():
    """A declared field read whole replaces its dotted keys; an undeclared
    key is read for the route (the cut drops it). A dotted key, and a
    never-sent guard (its stored value would go out), are refused."""
    assert _projection_of_for_cut(_RowWithLines, ('oneLine', 'extra')) == {
        'lines.qty': 1,
        'lines._id': 1,
        'either.qty': 1,
        'either._id': 1,
        'either.text': 1,
        'whole': 1,
        '_id': 0,
        'oneLine': 1,
        'extra': 1,
    }
    for key in ('oneLine.qty', ''):
        with pytest.raises(ValueError, match='top-level keys'):
            _projection_of_for_cut(_RowWithLines, (key, ))
    with pytest.raises(ValueError, match='stripped is a never-sent guard'):
        _projection_of_for_cut(_Guarded, ('stripped', ))


class _GuardedInner(ResponseModel):
    shown: str
    hidden: SkipJsonSchema[None] = None


class _GuardedOuter(ResponseModel):
    inner: _GuardedInner
    innerOpt: _GuardedInner | None = None


class _GuardedPlain(ResponseModel):
    inner: _GuardedInner


class _GuardedMid(ResponseModel):
    deep: _GuardedInner


class _GuardedTwoDeep(ResponseModel):
    inner: _GuardedMid


class _GuardedTwoDeepOpt(ResponseModel):
    inner: _GuardedMid | None = None


def test_cutProjection_refusesAGuardInsideAWholeRead():
    """A sub-document read whole (a readWhole key, or one that may be null)
    keeps a nested guard's stored value, so it is refused at any depth; read
    field by field, the guard is never read."""
    with pytest.raises(ValueError,
                       match='innerOpt is read whole and innerOpt.hidden is '
                       'a never-sent guard'):
        _projection_of_for_cut(_GuardedOuter)
    with pytest.raises(ValueError,
                       match='inner is read whole and inner.hidden is a '
                       'never-sent guard'):
        _projection_of_for_cut(_GuardedPlain, ('inner', ))
    assert _projection_of_for_cut(_GuardedPlain) == {
        'inner.shown': 1,
        '_id': 0
    }
    # Two levels down: Outer.inner -> Mid.deep -> a guarded Inner.
    with pytest.raises(ValueError,
                       match='inner is read whole and inner.deep.hidden is '
                       'a never-sent guard'):
        _projection_of_for_cut(_GuardedTwoDeep, ('inner', ))
    with pytest.raises(ValueError,
                       match='inner is read whole and inner.deep.hidden is '
                       'a never-sent guard'):
        _projection_of_for_cut(_GuardedTwoDeepOpt)
    assert _projection_of_for_cut(_GuardedTwoDeep) == {
        'inner.deep.shown': 1,
        '_id': 0
    }


class _GuardedTagA(ResponseModel):
    kind: Literal['a']
    shown: str
    hidden: SkipJsonSchema[None] = None


class _GuardedTagB(ResponseModel):
    kind: Literal['b']
    other: str
    hidden: SkipJsonSchema[None] = None


class _GuardedRoot(RootModel[list[_GuardedInner]]):
    pass


class _GuardedShapes(ResponseModel):
    one: _GuardedInner
    maybe: _GuardedInner | None = None
    many: list[_GuardedInner | None] = []
    byKey: dict[str, _GuardedInner] = {}
    tagged: typing.Annotated[_GuardedTagA | _GuardedTagB,
                             pydantic.Field(discriminator='kind')] | None = None
    unmatched: typing.Annotated[_GuardedTagA | _GuardedTagB,
                                pydantic.Field(discriminator='kind')] | None = None
    rooted: _GuardedRoot | None = None
    hidden: SkipJsonSchema[None] = None


_STAFF = 'staff only'


def _guardedShapesStored() -> dict:
    """A _GuardedShapes document with a value in every guard, at every
    depth and in every shape."""
    inner = {'shown': 's', 'hidden': _STAFF}
    return {
        'one': dict(inner),
        'maybe': dict(inner),
        'many': [dict(inner), None],
        'byKey': {
            'k': dict(inner)
        },
        'tagged': {
            'kind': 'a',
            'shown': 's',
            'hidden': _STAFF
        },
        'unmatched': {
            'kind': 'future',
            'shown': 's',
            'hidden': _STAFF
        },
        'rooted': [dict(inner)],
        'hidden': _STAFF,
    }


_GUARDED_SHAPES_SENT = {
    'one': {'shown': 's'},
    'maybe': {'shown': 's'},
    'many': [{'shown': 's'}, None],
    'byKey': {'k': {'shown': 's'}},
    'tagged': {'kind': 'a', 'shown': 's'},
    'unmatched': {'kind': 'future', 'shown': 's'},
    'rooted': [{'shown': 's'}],
}


def test_projectedCopy_dropsAGuardAtEveryDepthInEveryShape():
    """A nested model, an Optional, a list item, a dict value, a tagged
    union member, a value whose tag names no member, a RootModel, the top
    level, two levels down: a guard's stored value never survives the
    cut."""
    assert responseModelProblems(_GuardedShapes) == []
    assert projectedCopy(_GuardedShapes,
                         _guardedShapesStored()) == _GUARDED_SHAPES_SENT
    # A guard stored as an explicit null is dropped too: the key is absent.
    assert projectedCopy(_GuardedOuter, {
        'inner': {
            'shown': 's',
            'hidden': None
        },
        'innerOpt': None,
    }) == {
        'inner': {
            'shown': 's'
        },
        'innerOpt': None
    }
    twoDeep = {'inner': {'deep': {'shown': 's', 'hidden': _STAFF}}}
    assert projectedCopy(_GuardedTwoDeep, twoDeep) == {
        'inner': {
            'deep': {
                'shown': 's'
            }
        }
    }
    assert projectedCopy(_GuardedTwoDeepOpt, twoDeep) == {
        'inner': {
            'deep': {
                'shown': 's'
            }
        }
    }


class _GuardedRootOne(RootModel[_GuardedInner]):
    pass


def test_projectedCopy_cutsATopLevelRootModelThroughItsRoot():
    """A RootModel is cut as its root: a list item or the one model loses a
    guard's value and an undeclared key."""
    stored = {'shown': 's', 'hidden': _STAFF, 'z': 1}
    assert projectedCopy(_GuardedRoot, [dict(stored)]) == [{'shown': 's'}]
    assert projectedCopy(_GuardedRootOne, dict(stored)) == {'shown': 's'}
    assert projectedCopy(_RootOwnTag, {
        'kind': 'two',
        'a': 1,
        'b': 2
    }) == {
        'kind': 'two',
        'b': 2
    }


@pytest.mark.parametrize('model', [_GuardedRoot, _GuardedRootOne])
def test_projectionOf_refusesATopLevelRootModel(model):
    """A RootModel has no fields to project: the read helpers cannot read
    one, and the route cuts what it read with projectedCopy."""
    for projection in (projection_of, _projection_of_for_cut):
        with pytest.raises(ValueError, match='a RootModel has no fields'):
            projection(model)


def test_readHelpers_dropAGuardAtEveryDepth(monkeypatch):
    """The read-and-cut helpers cut with projectedCopy: a guard value the
    read returns (a legacy whole read, a mocked read) never goes out."""
    monkeypatch.setattr(
        db, 'read', lambda *args, **kwargs: _guardedShapesStored()
        if kwargs.get('findOne') else [_guardedShapesStored()])
    monkeypatch.setattr(db, 'aggregate',
                        lambda *args, **kwargs: [_guardedShapesStored()])
    monkeypatch.setattr(ObjectsModule, '_projection_of_for_cut',
                        lambda model, readWhole=(): {})

    assert db.readResponse(_GuardedShapes, {},
                           PROBE_COLLECTION) == _GUARDED_SHAPES_SENT
    assert db.readResponses(_GuardedShapes, {},
                            PROBE_COLLECTION) == [_GUARDED_SHAPES_SENT]
    assert db.aggregateResponses(_GuardedShapes, [],
                                 PROBE_COLLECTION) == [_GUARDED_SHAPES_SENT]


def test_prepare_mustReturnTheDocument(monkeypatch):
    monkeypatch.setattr(db, 'read', lambda *args, **kwargs: [{'text': 'x'}])

    with pytest.raises(TypeError, match='prepare returned None'):
        db.readResponses(_ProjectedNote, {}, PROBE_COLLECTION,
                         prepare=lambda doc: None)


def test_prepare_runsOnTheReadDocumentBeforeTheCut(monkeypatch):
    seen = []

    def prepare(doc):
        seen.append(dict(doc))
        return {**doc, 'text': doc.pop('legacyText')}

    monkeypatch.setattr(db, 'read', lambda *args, **kwargs: [{
        'legacyText': 'x',
        'cost': 1
    }])

    assert db.readResponses(_ProjectedNote, {}, PROBE_COLLECTION,
                            readWhole=('legacyText', ),
                            prepare=prepare) == [{'text': 'x'}]
    assert seen == [{'legacyText': 'x', 'cost': 1}]


def test_projectionStage_isExpandedByAggregateResponsesOnly(monkeypatch):
    """A projection stage mid-pipeline reads what its model declares (a
    nullable sub-document whole), so only aggregateResponses, which cuts
    every row, takes one; db.aggregate refuses it."""
    stage = db.projectionStage(_RowWithNullables, ('extra', ))
    with pytest.raises(TypeError, match='only db.aggregateResponses'):
        db.aggregate([stage], PROBE_COLLECTION)

    stages = []
    monkeypatch.setattr(
        db, 'aggregate', lambda pipeline, *args, **kwargs:
        stages.extend(pipeline) or [])
    db.aggregateResponses(_ProjectedNote, [stage, {'$limit': 1}],
                          PROBE_COLLECTION)
    assert stages == [
        {
            '$project': _projection_of_for_cut(_RowWithNullables,
                                                ('extra', ))
        },
        {
            '$limit': 1
        },
        {
            '$project': {
                'text': 1,
                '_id': 0
            }
        },
    ]


def test_projectionOf_theSampleRows():
    """The sample routes read exactly their models' fields."""
    assert projection_of(SampleTodoRow) == {
        '_id': 1,
        'title': 1,
        'completed': 1
    }
    assert projection_of(DevUserRow) == {
        '_id': 1,
        'name': 1,
        'userType': 1
    }
    assert projection_of(UserResponse) == {
        '_id': 1,
        'name': 1,
        'email': 1,
        'roles._id': 1,
        'roles.value': 1,
        'roles.description': 1,
        'userType': 1,
        'createdAt': 1,
    }


def test_projectedCopy_keepsWhatTheModelDeclaresAtEveryDepth():
    """What projection_of would read, cut in Python: undeclared keys go at
    every depth (an _id too), lists are walked, a field read whole is kept
    whole, and the input is not mutated."""
    doc = {
        '_id': 'rrm-row',
        'lines': [{
            'qty': 1,
            '_id': 'u1',
            'cost': 2
        }],
        'oneLine': {
            'qty': 1,
            '_id': 'u0',
            'cost': 2
        },
        'either': {
            'text': 'x',
            'cost': 2
        },
        'whole': [1, 2],
        'secret': 1,
    }
    snapshot = copy.deepcopy(doc)

    assert projectedCopy(_RowWithLines, doc) == {
        'lines': [{
            'qty': 1,
            '_id': 'u1'
        }],
        'oneLine': {
            'qty': 1,
            '_id': 'u0'
        },
        'either': {
            'text': 'x'
        },
        'whole': [1, 2],
    }
    assert doc == snapshot
    assert projectedCopy(_RowWithLines, None) is None


@pytest.mark.parametrize('annotation, stored, sent', [
    (list[list[_ProjectedNote]], [[{'text': 'a', 'cost': 1}]], [[{'text': 'a'}]]),
    (list[typing.Annotated[_ProjectedNote, pydantic.Field(title='Note')]],
     [{'text': 'a', 'cost': 1}], [{'text': 'a'}]),
    (_ProjectedNote | str, {'text': 'a', 'cost': 1}, {'text': 'a'}),
    (_ProjectedNote | str, 'legacy', 'legacy'),
    (dict[str, _ProjectedNote], {'k': {'text': 'a', 'cost': 1}}, {'k': {'text': 'a'}}),
    (tuple[_ProjectedNote, ...], [{'text': 'a', 'cost': 1}], [{'text': 'a'}]),
    (list[_ProjectedNote] | None, [{'text': 'a', 'cost': 1}], [{'text': 'a'}]),
    (_ProjectedNote | None, None, None),
])
def test_projectedCopy_neverLetsAStoredExtraThrough(annotation, stored, sent):
    """Whatever shape holds a nested ResponseModel, an undeclared stored key
    never survives the copy (a null or a legacy scalar is kept as it is)."""
    model = pydantic.create_model('_Shaped', __base__=ResponseModel,
                                  things=(annotation, ...))

    assert projectedCopy(model, {'things': stored, 'secret': 1}) == {
        'things': sent
    }


class _OverdueKind(ResponseModel):
    kind: Literal['overdue']
    overdueAmount: float


class _CreditKind(ResponseModel):
    kind: Literal['credit']
    creditLimit: float


class _Tagged(ResponseModel):
    hold: _OverdueKind | _CreditKind = pydantic.Field(..., discriminator='kind')


class _Untagged(ResponseModel):
    hold: _ProjectedLine | _ProjectedNote


def test_projectedCopy_copiesAUnionWithTheMemberTheValueIs():
    """A legacy document carrying both members' keys is copied as the one its
    tag names, never with both members' keys merged; an untagged union of
    models (refused by responseModelCheck) never guesses a member."""
    legacy = {
        'kind': 'credit',
        'overdueAmount': 5.0,
        'creditLimit': 10.0,
        'cost': 1
    }
    assert projectedCopy(_Tagged, {'hold': legacy}) == {
        'hold': {
            'kind': 'credit',
            'creditLimit': 10.0
        }
    }
    assert projectedCopy(_Untagged, {
        'hold': {
            'text': 'x',
            'cost': 1
        }
    }) == {
        'hold': {
            'text': 'x'
        }
    }
    # A value no member accepts is cut down to the keys a member declares,
    # never sent whole (the check reports it).
    unknown = {'kind': 'future', 'overdueAmount': 5.0, 'secret': 1}
    assert projectedCopy(_Tagged, {'hold': unknown}) == {
        'hold': {
            'kind': 'future',
            'overdueAmount': 5.0
        }
    }


class _LooseMember(ResponseModel):
    a: int | None = None


class _ExactMember(ResponseModel):
    b: int
    c: int


class _LooseFirst(ResponseModel):
    thing: _LooseMember | _ExactMember


def test_projectedCopy_neverGuessesTheMemberOfAnUntaggedUnion():
    """Every value validates as the all-optional member listed first, so a
    guess by validation would cut it to that member: an untagged union of
    models (refused by responseModelCheck) is cut to the keys any member
    declares instead."""
    assert responseModelProblems(_LooseFirst)
    assert projectedCopy(_LooseFirst, {'thing': {'b': 1, 'c': 2, 'x': 3}}) == {
        'thing': {
            'b': 1,
            'c': 2
        }
    }
    assert projectedCopy(_LooseFirst, {'thing': {'a': 1, 'b': 2, 'x': 3}}) == {
        'thing': {
            'a': 1,
            'b': 2
        }
    }


class _XKind(ResponseModel):
    kind: Literal['x']
    v: int
    note: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _acceptsEveryTag(cls, data):
        # A value tagged 'y' validates as _XKind too, with more of its fields
        # set than as _YKind: only the tag can name _YKind.
        return {**data, 'kind': 'x'} if isinstance(data, dict) else data


class _YKind(ResponseModel):
    kind: Literal['y']
    v: int


class _TaggedXY(ResponseModel):
    item: _XKind | _YKind = pydantic.Field(..., discriminator='kind')


def test_projectedCopy_letsTheTagDecideBetweenMembersThatBothFit():
    value = {'kind': 'y', 'v': 1, 'note': 'x'}
    assert len(_XKind.model_validate(value).model_fields_set) > len(
        _YKind.model_validate(value).model_fields_set)
    assert projectedCopy(_TaggedXY, {'item': {'kind': 'y', 'v': 1,
                                              'note': 'x'}}) == {
        'item': {
            'kind': 'y',
            'v': 1
        }
    }


class _NoteOrLines(ResponseModel):
    things: dict[str, _ProjectedLine] | str | None


def test_projectedCopy_cutsADictMemberAndAnUnmatchedDict():
    assert projectedCopy(_NoteOrLines, {
        'things': {
            'k': {
                'qty': 1,
                '_id': 'u1',
                'cost': 2
            }
        }
    }) == {
        'things': {
            'k': {
                'qty': 1,
                '_id': 'u1'
            }
        }
    }
    # No dict of lines: cut to the keys a member model declares (none).
    assert projectedCopy(_NoteOrLines, {'things': {'zzz': 5}}) == {
        'things': {}
    }


class _TaggedInAList(ResponseModel):
    holds: list[typing.Annotated[_OverdueKind | _CreditKind,
                                 pydantic.Field(discriminator='kind')]]


def test_projectedCopy_cutsATaggedUnionInAListByItsTag():
    """A union nested in a list carries its tag in Annotated: each item is
    cut as the member its tag names."""
    legacy = {
        'kind': 'credit',
        'overdueAmount': 5.0,
        'creditLimit': 10.0,
        'cost': 1
    }
    assert responseModelProblems(_TaggedInAList) == []
    assert projectedCopy(_TaggedInAList, {'holds': [legacy]}) == {
        'holds': [{
            'kind': 'credit',
            'creditLimit': 10.0
        }]
    }


class _TaggedInMetadata(ResponseModel):
    hold: typing.Annotated[_OverdueKind | _CreditKind,
                           pydantic.Discriminator('kind')]


def test_projectedCopy_readsAFieldsTagFromItsMetadata():
    """A field tagged by an Annotated Discriminator('kind') (kept in the
    field's metadata, not its discriminator) is cut by that tag too."""
    legacy = {'kind': 'credit', 'overdueAmount': 5.0, 'creditLimit': 10.0}
    assert responseModelProblems(_TaggedInMetadata) == []
    assert projectedCopy(_TaggedInMetadata, {'hold': legacy}) == {
        'hold': {
            'kind': 'credit',
            'creditLimit': 10.0
        }
    }


_OptionalTaggedHold = typing.Annotated[_OverdueKind | _CreditKind,
                                       pydantic.Field(discriminator='kind')]


class _OptionalTagged(ResponseModel):
    hold: _OptionalTaggedHold | None = None
    maybe: typing.Optional[_OptionalTaggedHold] = None
    holds: list[_OptionalTaggedHold | None] = []


def test_projectedCopy_cutsAnOptionalTaggedUnionByItsTag():
    """pydantic's standard optional form puts the tag on an Annotated inside
    the Optional (and inside a list item): the cut finds it there, so the
    value is cut as its member, never to {}; a null stays null."""
    legacy = {
        'kind': 'credit',
        'overdueAmount': 5.0,
        'creditLimit': 10.0,
        'cost': 1
    }
    sent = {'kind': 'credit', 'creditLimit': 10.0}
    assert responseModelProblems(_OptionalTagged) == []
    assert projectedCopy(_OptionalTagged, {
        'hold': legacy,
        'maybe': legacy,
        'holds': [legacy, None],
    }) == {
        'hold': sent,
        'maybe': sent,
        'holds': [sent, None],
    }
    assert projectedCopy(_OptionalTagged, {'hold': None}) == {'hold': None}


class _AliasedOverdue(ResponseModel):
    kind: Literal['overdue'] = pydantic.Field(..., alias='type')
    overdueAmount: float


class _AliasedCredit(ResponseModel):
    kind: Literal['credit'] = pydantic.Field(..., alias='type')
    creditLimit: float


class _AliasTagged(ResponseModel):
    hold: _AliasedOverdue | _AliasedCredit = pydantic.Field(
        ..., discriminator='kind')


def test_projectedCopy_readsAnAliasedTagUnderItsAlias():
    """The discriminator names the tag field; its value is read under the
    field's serialized key (its alias), as every field is."""
    assert projectedCopy(_AliasTagged, {
        'hold': {
            'type': 'credit',
            'overdueAmount': 5.0,
            'creditLimit': 10.0
        }
    }) == {'hold': {'type': 'credit', 'creditLimit': 10.0}}


class _OptionalTagField(BaseModel):
    kind: Literal['o'] | None = None
    o: int = 0


class _AnnotatedTagField(BaseModel):
    kind: typing.Annotated[Literal['p'],
                           pydantic.Field(description='tag')] | None = None
    p: int = 0


def test_memberFor_unwrapsAnOptionalOrAnnotatedTagField():
    """pydantic only builds a discriminated union over bare Literal tags,
    but the cut reads a tag field's Literal through Optional and Annotated
    all the same."""
    members = (_OptionalTagField, _AnnotatedTagField)
    assert ObjectsModule._member_for(members, {'kind': 'o'},
                                     'kind') is _OptionalTagField
    assert ObjectsModule._member_for(members, {'kind': 'p'},
                                     'kind') is _AnnotatedTagField


class _NotesOrLines(ResponseModel):
    things: list[_ProjectedNote] | list[_ProjectedLine]


class _NotesOrLineMap(ResponseModel):
    things: list[_ProjectedNote] | dict[str, _ProjectedLine]


class _NoteOrLineMap(ResponseModel):
    things: _ProjectedNote | dict[str, _ProjectedLine]


class _NotesOrText(ResponseModel):
    things: list[_ProjectedNote] | str


def test_projectedCopy_neverGuessesAmongMembersCarryingSeveralModels():
    """A union whose members carry different models in lists or dicts
    (refused by responseModelCheck: no tag can decide it) never has a member
    guessed by validation: a list sends nothing, a dict the keys a member
    model declares."""
    lines = [{'qty': 1, '_id': 'u1', 'cost': 2}]
    for model in (_NotesOrLines, _NotesOrLineMap, _NoteOrLineMap):
        assert responseModelProblems(model)
    assert projectedCopy(_NotesOrLines, {'things': lines}) == {'things': []}
    assert projectedCopy(_NotesOrLines, {'things': [{'zzz': 1}]}) == {
        'things': []
    }
    assert projectedCopy(_NotesOrLineMap, {
        'things': [{'text': 'a', 'cost': 1}]
    }) == {'things': []}
    assert projectedCopy(_NoteOrLineMap, {
        'things': {'k': lines[0], 'text': 'a'}
    }) == {'things': {'text': 'a'}}


def test_projectedCopy_cutsAListThroughTheOneListMember():
    """With one list member, any list is cut item by item through it, even
    an item it does not accept."""
    assert responseModelProblems(_NotesOrText) == []
    assert projectedCopy(_NotesOrText, {
        'things': [{'text': 'a', 'cost': 1}, {'zzz': 1}]
    }) == {'things': [{'text': 'a'}, {}]}


def _objectsModels() -> list:
    """The objects.py models with fields of their own (no RootModel)."""
    return [
        cls for cls in AppCreator(apiRequests=None)._objects_models()
        if not issubclass(cls, RootModel)
    ]


@pytest.mark.parametrize('model', [
    model for model in _objectsModels() if issubclass(model, ResponseModel)
], ids=lambda model: model.__name__)
def test_projectionOf_neverCollidesAPathWithItsParent(model):
    """Mongo refuses a projection holding both `a` and `a.b` (a path
    collision): no ResponseModel may derive one."""
    keys = set(_projection_of_for_cut(model))
    assert not [(key, other) for key in keys for other in keys
                if other.startswith(key + '.')]


# (row model, domain model, fields the row deliberately types otherwise).
# Every row model that restates fields of a domain model is listed here, so a
# field changed on the domain model must change on the row too.
MIRRORS = [
    # A row is read, so its _id is a required str.
    (ObjectsModule.DevUserRow, ObjectsModule.User, {'id'}),
    (ObjectsModule.SampleTodoRow, ObjectsModule.SampleTodo, {'id'}),
    # location is a StoreLocationResponse on the row, a StoreLocation on
    # the document: mirrored by the entry after this one.
    (ObjectsModule.StoreResponse, ObjectsModule.Store, {'id', 'location'}),
    (ObjectsModule.StoreLocationResponse, ObjectsModule.StoreLocation,
     set()),
    (ObjectsModule.CustomerSessionResponse, ObjectsModule.Customer, {'id'}),
    (ObjectsModule.ProductRow, ObjectsModule.Product, {'id'}),
    # reservedInventory is a list of ReservedInventoryResponse on the row,
    # of ReservedInventory on the document: mirrored by the entry after
    # this one.
    (ObjectsModule.AdminProductRow, ObjectsModule.Product,
     {'id', 'reservedInventory'}),
    (ObjectsModule.ReservedInventoryResponse, ObjectsModule.ReservedInventory,
     set()),
    # reservedQuantity is derived (the reservations summed): no Product
    # field to mirror.
    (ObjectsModule.AdminProductListRow, ObjectsModule.Product, {'id'}),
    # items is a list of OrderItemRow on the rows, of OrderItem on the
    # document: mirrored by the entry after these two.
    (ObjectsModule.StoreOrderRow, ObjectsModule.Order, {'id', 'items'}),
    (ObjectsModule.MyOrderRow, ObjectsModule.Order, {'id', 'items'}),
    (ObjectsModule.OrderItemRow, ObjectsModule.OrderItem, set()),
    (ObjectsModule.StorePolicyResponse, ObjectsModule.Policy, set()),
]


def _type_schema(field) -> dict:
    """A field's type as JSON schema, with its constraints (min_length, ge,
    pattern, ...), so equal types compare equal however they are spelled (a
    Literal alias, Optional[X] or X | None), and a constraint changed on the
    domain model must change on the row too."""
    annotation = field.annotation
    if field.metadata:
        annotation = typing.Annotated[(annotation, *field.metadata)]
    return pydantic.TypeAdapter(annotation).json_schema(mode='serialization')


def test_typeSchema_keepsTheFieldConstraints():
    class Loose(BaseModel):
        title: str

    class Strict(BaseModel):
        title: str = pydantic.Field(..., min_length=1)

    assert (_type_schema(Loose.model_fields['title']) !=
            _type_schema(Strict.model_fields['title']))


@pytest.mark.parametrize('row, domain, overrides',
                         MIRRORS,
                         ids=[row.__name__ for row, _, _ in MIRRORS])
def test_rowModels_mirrorTheirDomainFields(row, domain, overrides):
    """A field changed on the domain model must change on every row model
    restating it too, or this fails: same type, and the same default where
    the row may omit it (a projection leaves out what the stored document
    lacks, and the row then falls back to it)."""
    assert overrides <= set(row.model_fields) & set(domain.model_fields)
    shared = (set(row.model_fields) & set(domain.model_fields)) - overrides
    assert shared
    for name in sorted(shared):
        rowField = row.model_fields[name]
        domainField = domain.model_fields[name]
        assert _type_schema(rowField) == _type_schema(domainField), name
        assert rowField.alias == domainField.alias, name
        if not rowField.is_required():
            assert (rowField.get_default(call_default_factory=True) ==
                    domainField.get_default(call_default_factory=True)), name


# ----------------------------- route_config arguments ----------------------------- #


class _RootList(RootModel[list[int]]):
    pass


class _NotAModel:
    pass


class _PlainShape(BaseModel):
    count: int


class _LooseShape(ResponseModel):
    data: Any


@pytest.mark.parametrize('badModel', [
    dict,
    _NotAModel,
    _Shape(count=1),
    'UserResponse',
    _PlainShape,
    SampleTodoView,
    User,
    _LooseShape,
    RootModel[list[_PlainShape]],
])
def test_routeConfig_rejectsInvalidResponseModel(badModel):
    """Only a ResponseModel (or a bare cross-repo route's RootModel) whose
    whole tree holds: the import fails, naming the route."""
    with pytest.raises(ValueError, match='of fn must be a ResponseModel'):

        class Api:

            @route_config(httpMethod='POST', responseModel=badModel)
            def fn(self) -> dict:
                return {}


# ----------------------------- AppCreator ----------------------------- #


def test_appCreator_routeMeta_readsResponseModel():

    class Api:

        @route_config(httpMethod='POST', responseModel=UserResponse)
        def fn(self) -> dict:
            return {}

    meta = AppCreator(apiRequests=None)._route_meta(Api.fn)
    assert meta['responseModel'] is UserResponse


def test_appCreator_validateRouteMeta_acceptsObjectsModel():
    AppCreator(apiRequests=None)._validate_route_meta(
        'fn', routeMeta(responseModel=SampleTodoListResponse), 'ApiRequests')


def test_appCreator_validateRouteMeta_rejectsATypedSecretKey(monkeypatch):
    """ApiResponse strips SECRET_RESPONSE_KEYS from every response, so a
    model typing one would promise the client a key it never receives."""

    class Holder(ResponseModel):
        shareToken: str

    monkeypatch.setattr(AppCreatorModule, 'SECRET_RESPONSE_KEYS',
                        frozenset({'shareToken'}))
    AppCreatorModule._secret_keys_typed_by.cache_clear()
    monkeypatch.setattr(AppCreator, '_objects_classes',
                        lambda self: [Holder])
    try:
        with pytest.raises(ValueError, match="types \\['shareToken'\\]"):
            AppCreator(apiRequests=None)._validate_route_meta(
                'fn', routeMeta(responseModel=Holder), 'ApiRequests')
    finally:
        AppCreatorModule._secret_keys_typed_by.cache_clear()


class _UserViewLookalike(StoredDocumentKeys, User):
    pass


@pytest.mark.parametrize('badModel', [_Shape, _Line, _UserViewLookalike])
def test_appCreator_validateRouteMeta_rejectsNonObjectsModel(badModel):
    with pytest.raises(ValueError, match='defined in objects.py'):
        AppCreator(apiRequests=None)._validate_route_meta(
            'fn', routeMeta(responseModel=badModel), 'ApiRequests')


def test_appCreator_validateRouteMeta_rejectsLookalikeName():
    """Same __name__ and even __module__ as an objects.py class, but not that
    class's top-level definition (a nested class)."""

    class Holder:

        class UserView(BaseModel):
            pass

    Holder.UserView.__module__ = 'objects'
    with pytest.raises(ValueError, match='defined in objects.py'):
        AppCreator(apiRequests=None)._validate_route_meta(
            'fn', routeMeta(responseModel=Holder.UserView), 'ApiRequests')


def test_appCreator_validateRouteMeta_requiresModelOnApiRequestsRoute():
    with pytest.raises(ValueError,
                       match='fn in ApiRequests declares no responseModel'):
        AppCreator(apiRequests=None)._validate_route_meta(
            'fn', routeMeta(responseModel=None), 'ApiRequests')


def test_appCreator_validateRouteMeta_exemptsPubSubHandler():
    AppCreator(apiRequests=None)._validate_route_meta(
        'fn', routeMeta(responseModel=None), 'PubSubRequests')


def test_appCreator_objectsClasses_areReadOnce(monkeypatch):
    creator = AppCreator(apiRequests=None)
    reads = []
    realRead = AppCreator._classes_from_module

    def countingRead(self, module):
        reads.append(module.__name__)
        return realRead(self, module)

    monkeypatch.setattr(AppCreator, '_classes_from_module', countingRead)
    first = creator._objects_classes()
    assert creator._objects_classes() is first
    assert reads == ['objects']


def test_appCreator_tsMethod_typedReturnWhenModelDeclared():
    code = AppCreator(apiRequests=None)._ts_method_code(
        'fn', {'userId': str}, routeMeta(responseModel=UserView))
    assert 'async fn(userId: string): Promise<ApiResponse<UserView>>' in code
    # The parsed body is typed, so the method itself is checked.
    assert ('const data: ApiResponse<UserView> & { msg?: string } = '
            'await res.json();') in code
    assert 'any' not in code


def test_appCreator_tsMethod_rejectedJwtAndFailedFetchAreEnvelopes():
    """flask_jwt_extended answers a rejected token with a bare {"msg"};
    the typed method hands the caller an ApiFailure instead, and a network
    error or non-JSON body becomes one too."""
    code = AppCreator(apiRequests=None)._ts_method_code(
        'fn', {}, routeMeta(jwtRequired=True))
    assert ('if (data.msg) { localStorage.removeItem("access_token"); '
            'localStorage.removeItem("user_data"); '
            'window.dispatchEvent(new Event("jwt-expired")); '
            'return { message: data.msg, status: 401, data: null }; }') in code
    assert ('return { message: "Failed to fetch data", status: 0, '
            'data: null };') in code
    assert 'Authorization: `Bearer ${localStorage.getItem("access_token")}`' in code


def test_appCreator_tsMethod_aServerFaultIsAFailedFetch():
    """routeErrors.py answers a server fault with a real 5xx, which the
    ApiFailure union has no arm for: the client hands it to the page as
    STATUS_FETCH_FAILED, keeping the server's generic message."""
    code = AppCreator(apiRequests=None)._ts_method_code('fn', {}, routeMeta())
    assert ('if (res.status >= 500) { return { message: data.message, '
            'status: 0, data: null }; }') in code
    assert code.index('if (res.status >= 500)') < code.index('return data;')


def test_appCreator_tsMethod_failedLoginStoresNoToken():
    code = AppCreator(apiRequests=None)._ts_method_code(
        'fn', {}, routeMeta(createAccessToken=True))
    assert ('if (data.access_token) { localStorage.setItem("access_token", '
            'data.access_token); }') in code


def test_appCreator_routeCode_usesSharedStatuses():
    """ApiResponse.ts derives its status literals from the same constants
    the generated Flask routes answer with."""
    code = AppCreator(apiRequests=None)._function_code(
        'fn', Parameters(names=['userId']), routeMeta(jwtRequired=True),
        'app', 'POST', None)
    statuses = {int(s) for s in re.findall(r'status=(\d+)', code)}
    assert statuses == {STATUS_OK, STATUS_FORBIDDEN, STATUS_BAD_REQUEST}


@pytest.mark.parametrize('annotation, expected', [
    (dict, 'Record<string, unknown>'),
    (list, 'unknown[]'),
    (Any, 'unknown'),
    (list[SampleTodoRow] | None, 'SampleTodoRow[] | null'),
    (UserView, 'UserView'),
])
def test_pyAnnotationToTs_neverEmitsAny(annotation, expected):
    assert py_annotation_to_ts(annotation) == expected


@pytest.mark.parametrize('annotation, expected', [
    (list[str | None], '(string | null)[]'),
    (typing.Optional[list[int | str]], '(number | string)[] | null'),
    (tuple[int | None, ...], '(number | null)[]'),
    (list[Literal['a', 'b']], "('a' | 'b')[]"),
    (list[Literal['a']], "'a'[]"),
    (list[list[str] | None], '(string[] | null)[]'),
    (dict[str, list[str | None]], 'Record<string, (string | null)[]>'),
    (Literal['a', None], "'a' | null"),
])
def test_pyAnnotationToTs_parenthesizesAUnionArrayElement(
        annotation, expected):
    """`[]` binds tighter than `|`: unparenthesized, `string | null[]`
    would type a string or an array of nulls."""
    assert py_annotation_to_ts(annotation) == expected


def test_pyAnnotationToTs_hasNoDatetimeMapping():
    assert datetime.datetime not in BASIC_PY_TO_TS
    assert datetime.date not in BASIC_PY_TO_TS


def test_appCreator_schemaImports_useEachFilesOwnModel():
    """Each model is imported from its own schema file, once."""
    imports = AppCreator(apiRequests=None)._ts_schema_imports(
        ['UserView', 'SampleTodoRow', 'UserView'])
    assert imports.splitlines() == [
        "import type { SampleTodoRow } from '../schemas/SampleTodoRowSchema';",
        "import type { UserView } from '../schemas/UserViewSchema';",
    ]


def test_appCreator_routeModels_comeFromTheRouteNotItsCode(monkeypatch):
    """The imported models are the route's responseModel and its param
    models, read from the route itself. A model named like a word of the
    generated body (the "Content-Type" header, `Authorization`) is not
    imported unless a route uses it."""

    class Content(BaseModel):
        pass

    class Authorization(BaseModel):
        pass

    monkeypatch.setattr(
        AppCreator, '_objects_classes',
        lambda self: [Content, Authorization, UserView, SampleTodoRow])
    creator = AppCreator(apiRequests=None)
    meta = routeMeta(jwtRequired=True)
    code = creator._ts_method_code('fn', {'userId': str}, meta)
    assert '"Content-Type"' in code and 'Authorization' in code

    assert creator._ts_route_models(meta, {'userId': str}) == {'UserView'}
    assert creator._ts_route_models(
        meta, {'rows': List[SampleTodoRow] | None}) == {
            'UserView', 'SampleTodoRow'
        }


def _generated_server_requests() -> str:
    with open(os.path.join(str(CLIENT_API_DIR), 'ServerRequests.ts'),
              encoding='utf-8') as f:
        return f.read().replace('\r\n', '\n')


def test_generatedServerRequests_importsExactlyTheRoutesModels():
    """The generated client imports the models its routes use and nothing
    else: no `Content` for the "Content-Type" header."""
    creator = AppCreator(apiRequests=None)
    referenced = set()
    for func in apiRoutes().values():
        referenced |= creator._ts_route_models(
            creator._route_meta(func), creator._params_with_annotations(func))
    imported = set(
        re.findall(r"^import type \{ (\w+) \} from '\.\./schemas/",
                   _generated_server_requests(), re.M))
    assert imported == referenced
    assert 'Content' not in imported


def test_appCreator_schemaImports_rejectRootNameCollision(
        monkeypatch, tmp_path):
    (tmp_path / 'FooSchema.ts').write_text(
        'export type Foo = string;\nexport interface Foo1 {\n  foo?: Foo;\n}\n')
    monkeypatch.setattr(AppCreatorModule, 'CLIENT_SCHEMAS_DIR', tmp_path)

    with pytest.raises(ValueError, match='exports both Foo and Foo1'):
        AppCreator(apiRequests=None)._ts_schema_imports(['Foo'])


def test_appCreator_schemaImports_rejectAFileWithoutTheModel(
        monkeypatch, tmp_path):
    (tmp_path / 'FooSchema.ts').write_text('export type Bar = string;\n')
    monkeypatch.setattr(AppCreatorModule, 'CLIENT_SCHEMAS_DIR', tmp_path)

    with pytest.raises(ValueError, match='FooSchema.ts does not export Foo'):
        AppCreator(apiRequests=None)._ts_schema_imports(['Foo'])


def test_appCreator_loginRouteCode_neverLogsTheFirebasePayload():
    """loginWithGoogle's body carries the Firebase ID token: the generated
    success log and both error logs (PermissionError and any other) carry
    no request data at all, not even its keys."""
    creator = AppCreator(apiRequests=None)
    login = creator._function_code('fn',
                                   Parameters(names=['firebaseUserObject']),
                                   routeMeta(createAccessToken=True), 'app',
                                   'POST', None)

    logged = [line for line in login.splitlines() if 'logging.' in line]
    assert len(logged) == 3
    assert not [
        line for line in logged if 'data' in line or 'requestKeys' in line
    ]


def test_appCreator_routeCode_logsRequestKeysOnly():
    """A request body carries customer data, passwords and bank numbers:
    every other POST route logs which of its own params it was sent, never
    a value, and never a key it does not take (the caller named those). The
    publisher flags are not body params."""
    creator = AppCreator(apiRequests=None)
    code = creator._function_code(
        'fn',
        Parameters(names=['userId', 'password', 'publishToProdPubSub']),
        routeMeta(jwtRequired=True), 'app', 'POST', None)
    noParams = creator._function_code('fn', Parameters(names=[]),
                                      routeMeta(), 'app', 'POST', None)

    logged = [line for line in code.splitlines() if 'logging.' in line]
    assert len(logged) == 3
    assert all(
        "'requestKeys': sorted(key for key in data if key in "
        "('userId', 'password'))" in line for line in logged)
    assert not [line for line in logged if "'data'" in line]
    assert noParams.count("'requestKeys': []") == 3


def test_appCreator_pubSubCode_logsTheMessageIdAndKeys():
    code = AppCreator(apiRequests=None)._function_code(
        'fn', Parameters(names=['data']), routeMeta(responseModel=None),
        'appPubSub', 'POST', None)

    logged = [line for line in code.splitlines() if 'logging.' in line]
    assert len(logged) == 3
    assert all(
        "'messageId': messageId, 'messageKeys': sorted(originalMessage) "
        "if isinstance(originalMessage, dict) else None" in line
        for line in logged)
    assert 'requestKeys' not in code


def test_appCreator_pubSubCode_parsesTheEnvelopeInsideTheTry():
    """A malformed push (no message, data or messageId) raises inside the
    try: a 400 envelope, not a 500."""
    code = AppCreator(apiRequests=None)._function_code(
        'fn', Parameters(names=['data']), routeMeta(responseModel=None),
        'appPubSub', 'POST', None)

    beforeTry, afterTry = code.split('    try:\n', 1)
    assert 'data["message"]' not in beforeTry
    assert '        originalMessage = data["message"]["data"]\n' in afterTry
    assert '        messageId = data["message"]["messageId"]\n' in afterTry
    assert '    messageId = None\n    originalMessage = None\n' in beforeTry


@pytest.mark.parametrize('annotation, isScalar', [
    (str, True),
    (int, True),
    (float, True),
    (bool, True),
    (str | None, True),
    (typing.Optional[int], True),
    (Literal['a', 'b'], True),
    (Literal['a'] | None, True),
    (list[str], False),
    (dict[str, int], False),
    (SampleTodoRow, False),
    (str | list[str], False),
    (inspect.Parameter.empty, False),
])
def test_appCreator_scalarAnnotations(annotation, isScalar):
    assert AppCreatorModule._is_scalar_annotation(annotation) is isScalar


def test_appCreator_routeCode_refusesAnObjectForAScalarParam():
    """app.py hands a route the raw JSON value: a {"$ne": null} for a str
    param would reach a Mongo query as an operator. The handler refuses an
    object or a list for each scalar param, inside the try (a 400)."""

    def deleteThing(self, thingId: str, count: int | None, tags: list[str],
                    publishToProdPubSub: bool) -> dict:
        return {}

    creator = AppCreator(apiRequests=None)
    scalarParams = creator._scalar_params(deleteThing)
    assert scalarParams == ['thingId', 'count']

    code = creator._function_code(
        'deleteThing', Parameters(names=['thingId', 'count', 'tags']),
        routeMeta(), 'app', 'POST', None, scalarParams)

    afterTry = code.split('    try:\n', 1)[1]
    assert afterTry.startswith(
        "        if isinstance(thingId, (dict, list)):\n"
        "            raise ValueError('Invalid thingId')\n"
        "        if isinstance(count, (dict, list)):\n"
        "            raise ValueError('Invalid count')\n")
    assert 'isinstance(tags' not in code


def test_generatedApp_refusesAnObjectForEveryScalarParam():
    with open(os.path.join(str(SERVER_DIR), 'app.py'), encoding='utf-8') as f:
        app = f.read()
    stale = 'app.py is stale: run `cd server && python AppCreator.py`'
    creator = AppCreator(apiRequests=None)
    for name, func in apiRoutes().items():
        route = _generated_route(app, name)
        for param in creator._scalar_params(func):
            assert (f"        if isinstance({param}, (dict, list)):\n"
                    f"            raise ValueError('Invalid {param}')\n"
                    ) in route, stale


def test_appCreator_mainBlock_bindsLoopbackLocally():
    """A local run binds what _localBindHost answers (127.0.0.1 unless
    LOCAL_BIND_HOST is set); only Cloud Run binds every interface."""
    block = AppCreator(apiRequests=None)._main_function_block('app')

    assert 'def _localBindHost() -> str:' in block
    assert 'os.getenv("LOCAL_BIND_HOST")' in block
    assert "app.run(debug=False, host=_localBindHost(), port=" in block
    assert block.count("host='0.0.0.0'") == 1
    assert "app.run(host='0.0.0.0', port=8080)" in block


@pytest.mark.parametrize('kind', ['app', 'appPubSub'])
def test_appCreator_routeCode_bindsTheBodyUnconditionally(kind):
    """A POST without a JSON object body reads as {}: before, `data` and the
    params were bound only under `if request.is_json`, so a non-JSON POST
    raised UnboundLocalError outside the try (a 500 HTML page)."""
    code = AppCreator(apiRequests=None)._function_code(
        'fn', Parameters(names=['todoId']), routeMeta(), kind, 'POST', None)

    body = code.split('def fn():\n', 1)[1]
    assert body.startswith('    data = request.get_json(silent=True)\n'
                           '    if not isinstance(data, dict):\n'
                           '        data = {}\n')
    assert 'request.is_json' not in code
    if kind == 'app':
        assert "\n    todoId = data.get('todoId')\n    try:\n" in body


def test_appCreator_routeResponseModels():
    creator = AppCreator(apiRequests=None)
    assert creator._ts_route_response_models({
        'loginWithGoogle': 'UserView'
    }) == ('export interface RouteResponseModels {\n'
           '    loginWithGoogle: UserView;\n}\n')
    assert creator._ts_route_response_models({}) == (
        'export type RouteResponseModels = Record<never, never>;\n')


@pytest.mark.parametrize('annotation', [
    dict,
    list,
    Any,
    typing.Dict,
    typing.List,
    list | None,
    dict[str, Any],
    _Shape,
    list[_Line] | None,
])
def test_appCreator_params_rejectUntypedAndForeignTypes(annotation):

    def fn(self, x: annotation) -> dict:
        return {}

    with pytest.raises(ValueError, match="parameter 'x'"):
        AppCreator(apiRequests=None)._params_with_annotations(fn)


@pytest.mark.parametrize('annotation', [
    str | None,
    dict[str, int],
    list[SampleTodoRow] | None,
    Literal['a', 'b'],
    UserView,
])
def test_appCreator_params_acceptTypedAnnotations(annotation):

    def fn(self, x: annotation) -> dict:
        return {}

    assert AppCreator(apiRequests=None)._params_with_annotations(fn) == {
        'x': annotation
    }


@pytest.mark.parametrize('annotation', [
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.datetime | None,
    typing.Optional[datetime.date],
    list[datetime.datetime],
    dict[str, datetime.date],
])
def test_appCreator_params_rejectDatetimes(annotation):
    """app.py passes the raw JSON value, so a datetime param would receive a
    string while the TS client sends a Date."""

    def fn(self, x: annotation) -> dict:
        return {}

    with pytest.raises(ValueError, match=r"parameter 'x'.*Annotate it `str`"):
        AppCreator(apiRequests=None)._params_with_annotations(fn)


@pytest.mark.parametrize('annotation', [
    set[str],
    frozenset[str],
    typing.Set[str],
    typing.Annotated[str, 'meta'],
    collections.abc.Sequence[str],
    type[SampleTodoRow],
    list[set[str]] | None,
])
def test_appCreator_params_rejectUnrenderableContainers(annotation):

    def fn(self, x: annotation) -> dict:
        return {}

    with pytest.raises(ValueError,
                       match=r"parameter 'x'.*may only be built from"):
        AppCreator(apiRequests=None)._params_with_annotations(fn)


@pytest.mark.parametrize('annotation', [str, list[str], SampleTodoRow])
def test_appCreator_params_rejectNoneDefaultWithoutOptional(annotation):

    def fn(self, x: annotation = None) -> dict:
        return {}

    with pytest.raises(ValueError, match=r"parameter 'x' defaults to None"):
        AppCreator(apiRequests=None)._params_with_annotations(fn)


@pytest.mark.parametrize('annotation, default', [
    (str | None, None),
    (typing.Optional[list[str]], None),
    (int, 20),
    (str, 'name'),
])
def test_appCreator_params_acceptDefaultsTheirAnnotationAdmits(
        annotation, default):

    def fn(self, x: annotation = default) -> dict:
        return {}

    assert AppCreator(apiRequests=None)._params_with_annotations(fn) == {
        'x': annotation
    }


def test_appCreator_params_skipThePublisherFlags():
    """The publish flags are set by app.py and pubSubDecorator, never sent
    by the client, so they are no part of the TS method."""

    def createOrder(self, userId: str, publishToProdPubSub: bool,
                    publishToPubSubMockDb: bool = False) -> dict:
        return {}

    creator = AppCreator(apiRequests=None)
    params = creator._params_with_annotations(createOrder)
    assert 'publishToProdPubSub' not in params
    assert 'publishToPubSubMockDb' not in params
    assert 'userId' in params


def test_wrapSiblingRefs_wrapsOnlyARefWithSiblings():
    schema = {
        'properties': {
            'bare': {'$ref': '#/$defs/X'},
            'described': {'$ref': '#/$defs/X', 'description': 'd'},
            'nullable': {
                'anyOf': [{'$ref': '#/$defs/X', 'default': None},
                          {'type': 'null'}]
            },
        },
        '$defs': {'X': {'type': 'object', 'properties': {}}},
    }
    snapshot = copy.deepcopy(schema)

    out = _wrap_sibling_refs(schema)

    assert schema == snapshot
    assert out['properties'] == {
        'bare': {'$ref': '#/$defs/X'},
        'described': {'allOf': [{'$ref': '#/$defs/X'}], 'description': 'd'},
        'nullable': {
            'anyOf': [{'allOf': [{'$ref': '#/$defs/X'}], 'default': None},
                      {'type': 'null'}]
        },
    }
    assert out['$defs'] == snapshot['$defs']


def _schema_files():
    for file in sorted(os.listdir(str(CLIENT_SCHEMAS_DIR))):
        if file.endswith('Schema.ts'):
            with open(os.path.join(str(CLIENT_SCHEMAS_DIR), file),
                      encoding='utf-8') as f:
                yield file, f.read()


def test_generatedSchemas_neverDuplicateAnInterface():
    """json2ts read a $ref with a sibling description as a new type and
    emitted copies such as User1 next to User."""
    for file, src in _schema_files():
        interfaces = set(re.findall(r'export interface (\w+)', src))
        for name in interfaces:
            numbered = re.fullmatch(r'(\w*?[A-Za-z_])\d+', name)
            assert not (numbered and numbered.group(1) in interfaces), (
                f'{file} declares {name} as a copy of {numbered.group(1)}: '
                'run `cd server && python AppCreator.py`')


def test_generatedSchemas_closeNestedModels():
    """A nested model left open gets `[k: string]: unknown` in its TS
    interface, and a renamed nested field would then still type-check.
    Only models that declare fields are checked: a field typed as a bare
    dict or Any stays free-form, which is the model's to fix, not the
    generator's."""
    open_ = []
    for file in sorted(os.listdir(str(SERVER_SCHEMAS_DIR))):
        if not file.endswith('Schema.json'):
            continue
        with open(os.path.join(str(SERVER_SCHEMAS_DIR), file),
                  encoding='utf-8') as f:
            schema = json.load(f)
        if schema.get('type') == 'object':
            if schema.get('additionalProperties') is not False:
                open_.append(file)
        else:
            # A RootModel's bare array or number: nothing to close.
            assert 'additionalProperties' not in schema, file
        open_.extend(f'{file}#{name}'
                     for name, definition in schema.get('$defs', {}).items()
                     if 'properties' in definition
                     and definition.get('additionalProperties') is not False)
    assert not open_, (f'{open_} leave a model open: run '
                       '`cd server && python AppCreator.py`')


def test_generatedSchemas_coverExactlyTheObjectsModels():
    """Every objects.py pydantic model has its schema, and no schema
    outlives the class it was generated from (a stale one could still be
    imported)."""
    models = {
        cls.__name__
        for cls in AppCreator(apiRequests=None)._objects_models()
    }
    schemas = {file[:-len('Schema.ts')] for file, _ in _schema_files()}
    assert schemas == models
    jsonSchemas = {
        file[:-len('Schema.json')]
        for file in os.listdir(str(SERVER_SCHEMAS_DIR))
        if file.endswith('Schema.json')
    }
    assert jsonSchemas == models


def test_appCreator_routeRequestBodies():
    creator = AppCreator(apiRequests=None)
    assert creator._ts_route_request_bodies({
        'getRows': {
            'userId': str,
            'rows': list[SampleTodoRow] | None
        },
        'ping': {},
    }) == ('export interface RouteRequestBodies {\n'
           '    getRows: { userId: string; rows: SampleTodoRow[] | null };\n'
           '    ping: Record<string, never>;\n}\n')
    assert creator._ts_route_request_bodies({}) == (
        'export type RouteRequestBodies = Record<never, never>;\n')


@pytest.mark.parametrize('meta, isPublic', [
    (routeMeta(), True),
    (routeMeta(jwtRequired=True), False),
    (routeMeta(createAccessToken=True), False),
    (routeMeta(httpMethod='GET'), False),
])
def test_appCreator_isPublicRoute(meta, isPublic):
    assert AppCreator._is_public_route(meta) is isPublic


def test_appCreator_publicRoutes():
    creator = AppCreator(apiRequests=None)
    assert creator._ts_public_routes(['getA', 'getB']) == (
        "export type PublicRoute =\n    | 'getA'\n    | 'getB';\n")
    assert creator._ts_public_routes([]) == 'export type PublicRoute = never;\n'


def test_generatedApiResponseTs_isCurrent():
    assert ('export interface ApiSuccess<T> extends EnvelopeMeta '
            '{ status: 200; data: T }') in API_RESPONSE_TS
    assert ('export interface ApiFailure extends EnvelopeMeta '
            '{ status: 0 | 400 | 401 | 403; data: null }') in API_RESPONSE_TS
    assert ('export type ApiResponse<T> = ApiSuccess<T> | ApiFailure;'
            ) in API_RESPONSE_TS
    with open(os.path.join(str(CLIENT_API_DIR), 'ApiResponse.ts'),
              encoding='utf-8') as f:
        onDisk = f.read()
    assert onDisk.replace('\r\n', '\n') == API_RESPONSE_TS, (
        'client/app/api/ApiResponse.ts is stale: run `cd server && '
        'python AppCreator.py`')


def test_generatedServerRequests_listsEveryRoute():
    """Every route has a method returning its model, and is in
    RouteResponseModels."""
    generated = _generated_server_requests()
    stale = 'ServerRequests.ts is stale: run `cd server && python AppCreator.py`'
    for name, func in apiRoutes().items():
        model = func.responseModel.__name__
        assert re.search(
            rf'  async {name}\([^)]*\): Promise<ApiResponse<{model}>>',
            generated), stale
        assert f'    {name}: {model};\n' in generated, stale


def test_generatedServerRequests_listsRouteBodiesAndPublicRoutes():
    generated = _generated_server_requests()
    creator = AppCreator(apiRequests=None)
    routes = apiRoutes()
    bodies = {
        name: creator._params_with_annotations(func)
        for name, func in routes.items()
    }
    public = [
        name for name, func in routes.items()
        if creator._is_public_route(creator._route_meta(func))
    ]
    # POSTed to without a token: the starter page's sample todo routes and
    # the localdev-only dev user list (test_auth_surface pins the whole
    # public surface).
    assert set(public) == {
        'devGetUserList', 'getTodos', 'createTodo', 'markTodoAsCompleted',
        'markTodoAsUnfinished', 'deleteTodo'
    }
    stale = ('ServerRequests.ts is stale: run `cd server && python '
             'AppCreator.py`')
    assert creator._ts_route_request_bodies(bodies) in generated, stale
    assert creator._ts_public_routes(public) in generated, stale


def _withoutComments(source: str) -> str:
    """TS source without its comments: json2ts copies each field's
    description into a doc comment, where `any` is just an English word."""
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    return re.sub(r'//[^\n]*', '', source)


def test_generatedServerRequests_hasNoAny():
    """Every method returns Promise<ApiResponse<Model>> and every param is
    typed, so the client needs neither `any` nor the eslint directive that
    once allowed it."""
    generated = _generated_server_requests()
    assert not re.search(r'\bany\b', generated)
    assert 'eslint-disable' not in generated


def test_generatedApiResponseAndSchemas_haveNoAny():
    with open(os.path.join(str(CLIENT_API_DIR), 'ApiResponse.ts'),
              encoding='utf-8') as f:
        files = [('ApiResponse.ts', f.read())] + list(_schema_files())
    assert len(files) > 1
    withAny = [
        file for file, src in files
        if re.search(r'\bany\b', _withoutComments(src))
    ]
    assert not withAny, f'{withAny} type something as `any`'


def _generated_route(app: str, name: str) -> str:
    return app.split(f'def {name}():', 1)[1].split('\n@app.route', 1)[0]


def test_generatedApp_loginNeverLogsTheFirebasePayload():
    with open(os.path.join(str(SERVER_DIR), 'app.py'), encoding='utf-8') as f:
        app = f.read()
    for name in ('loginWithGoogle', 'devLogin'):
        logged = [
            line for line in _generated_route(app, name).splitlines()
            if 'logging.' in line
        ]
        assert len(logged) == 3, (
            'app.py is stale: run `cd server && python AppCreator.py`')
        for line in logged:
            assert 'data' not in line and 'firebaseUserObject' not in line
            assert 'requestKeys' not in line


def test_generatedApp_otherRoutesLogRequestKeysOnly():
    with open(os.path.join(str(SERVER_DIR), 'app.py'), encoding='utf-8') as f:
        app = f.read()
    assert 'request.is_json' not in app, (
        'app.py is stale: run `cd server && python AppCreator.py`')
    createTodo = _generated_route(app, 'createTodo')
    assert createTodo.count(
        "'requestKeys': sorted(key for key in data if key in ('title',))"
    ) == 3
    assert "'data': data" not in app


# ----------------------------- conftest suite guard ----------------------------- #


def test_suiteGuard_isInstalledBeforeTheSingletonsConnect():
    """conftest replaces pymongo's MongoClient at import, before mongoDb
    binds the name: the singletons built when mongoDb is first imported are
    guarded clients, not just the clients a test builds later."""
    import pymongo
    import pymongo.mongo_client
    import mongoDb as mongoDbModule
    from conftest import _LocalOnlyMongoClient

    assert mongoDbModule.MongoClient is _LocalOnlyMongoClient
    assert pymongo.MongoClient is _LocalOnlyMongoClient
    for singleton in (mongoDbModule.db, mongoDbModule.pubSubMockDb):
        assert type(singleton.client) is _LocalOnlyMongoClient


@pytest.mark.parametrize('host', [
    'mongodb+srv://user:pass@cluster0.example.mongodb.net/?retryWrites=true',
    'mongodb://user:pass@db.example.com:27017/app',
    'mongodb://localhost:27017,db.example.com:27017/?replicaSet=rs0',
    'mongodb://evil.example.com/?appName=x@localhost',
    'mongodb://evil.example.com/db@localhost',
    'db.example.com',
    ['localhost', 'db.example.com'],
])
def test_suiteGuard_refusesANonLocalDatabaseClient(host):
    """Whatever a test does to ENVIRONMENT, no client to a cloud cluster
    can be opened. The refusal never quotes the URI (its credentials)."""
    import pymongo
    import mongoDb as mongoDbModule

    for mongoClient in (mongoDbModule.MongoClient, pymongo.MongoClient):
        with pytest.raises(AssertionError, match='non-local host') as excinfo:
            mongoClient(host)
        assert 'pass' not in str(excinfo.value)


@pytest.mark.parametrize('host', [
    None,
    'localhost',
    'mongodb://localhost:27017/?replicaSet=rs0',
    'mongodb://127.0.0.1:27017,localhost:27018',
])
def test_suiteGuard_allowsALocalDatabaseClient(host):
    from conftest import _isLocalDbHost
    assert _isLocalDbHost(host)


def test_suiteGuard_runsOnlyAgainstAScratchDatabase():
    """The suite wipes its database, and the default localdev one holds the
    restored prod dump: conftest refuses to start unless databaseName ends
    with 'Scratch', before it touches any database."""
    import subprocess
    import sys
    import mongoDb as mongoDbModule

    from AppConfig import Project

    assert os.environ['databaseName'].endswith('Scratch')
    assert mongoDbModule.db.databaseName.endswith('Scratch')
    for databaseName in (f'test{Project().projectName}', ''):
        env = dict(os.environ, databaseName=databaseName)
        result = subprocess.run([sys.executable, '-c', 'import conftest'],
                                cwd=str(SERVER_DIR),
                                env=env,
                                capture_output=True,
                                text=True,
                                timeout=60)
        assert result.returncode != 0
        assert 'scratch database' in result.stderr
        assert 'MongoClient' not in result.stderr


def test_suiteGuard_unsetsTheCloudDatabaseVariables():
    from conftest import _CLOUD_DB_ENV_VARS
    assert 'MONGO_URI' in _CLOUD_DB_ENV_VARS
    for name in _CLOUD_DB_ENV_VARS:
        assert name not in os.environ


@pytest.mark.parametrize('environment', ['clouddev', 'cloudprod'])
def test_newDatabaseClient_inACloudEnvironment_isRefused(monkeypatch,
                                                         environment):
    """What would happen to a mongoDb() built under a cloud ENVIRONMENT:
    refused, or failing for the unset URI, never connecting."""
    import mongoDb as mongoDbModule
    monkeypatch.setenv('ENVIRONMENT', environment)

    with pytest.raises(Exception) as excinfo:
        mongoDbModule.mongoDb()
    assert excinfo.type is AssertionError or (
        'MONGO_URI environment variable is not set' in str(excinfo.value))


def test_databaseSingletons_buildOneClientEach():
    """Python runs __init__ on every call of a Singleton class: a second
    mainDb() / PubSubMockDb() returns the instance and keeps its client
    (a new client per call would leak connections on the shared cluster)."""
    import mongoDb as mongoDbModule
    for existing, again in (
            (mongoDbModule.db, mongoDbModule.mainDb),
            (mongoDbModule.pubSubMockDb,
             lambda: mongoDbModule.PubSubMockDb(None, 'pubSubMockDb'))):
        client = existing.client
        assert again() is existing
        assert existing.client is client


@pytest.mark.parametrize('environment', ['clouddev', 'cloudprod'])
def test_mockDbWrite_inACloudEnvironment_isRefused(monkeypatch, environment):
    """No cloud environment writes pubSubMockDb, so its connect=False client
    never connects in the cloud (test_mongoDb_cloud_clients.py). The refusal
    is checked directly: only the generator may publish to the mock DB
    (test_onlyGeneratorWritesPubSubMockDb.py)."""
    import pubSub as pubSubModule
    monkeypatch.setenv('ENVIRONMENT', environment)

    with pytest.raises(Exception,
                       match='mock database in a cloud environment'):
        pubSubModule._refuseMockDbWriteInCloud()


def test_mockDbWrite_inLocaldev_isAllowed_andPublishMessageChecksFirst():
    import pubSub as pubSubModule
    pubSubModule._refuseMockDbWriteInCloud()

    # publishMessage refuses before its mock-DB write branch is reached
    source = inspect.getsource(pubSubModule.PubSub.publishMessage)
    assert (source.index('_refuseMockDbWriteInCloud()')
            < source.index('elif publishToPubSubMockDb:'))


# ----------------------------- Dates in params ----------------------------- #


def test_appDayStart_isTheAppTimezoneMidnight():
    from AppConfig import Timezone
    from utils import appDayBounds, appDayStart

    start = appDayStart('2026-10-05')
    assert start == datetime.datetime(2026, 10, 5,
                                      tzinfo=Timezone().getTimezone())
    first, last = appDayBounds('2026-10-01', '2026-10-05')
    assert first == datetime.datetime(2026, 10, 1,
                                      tzinfo=Timezone().getTimezone())
    assert last == start.replace(hour=23,
                                 minute=59,
                                 second=59,
                                 microsecond=999999)


@pytest.mark.parametrize('value', [
    '2026-10-05T00:00:00Z', '20261005', '2026-W40-1', '2026-13-01',
    '２０２６-10-05', None, 20261005, 'Sun, 04 Oct 2026 08:00:00 GMT'
])
def test_appDayStart_refusesAnythingButAnIsoDateWithoutQuotingIt(value):
    from utils import appDayStart

    with pytest.raises(ValueError,
                       match='^dates must be ISO YYYY-MM-DD dates$') as e:
        appDayStart(value)
    assert e.value.__cause__ is None


def test_parseIsoInstant_parsesAsAPydanticFieldWould():
    from utils import parseIsoInstant

    assert parseIsoInstant(None, 'deliveryDate') is None
    assert parseIsoInstant('2026-10-05T08:00:00Z', 'deliveryDate') == (
        datetime.datetime(2026, 10, 5, 8, tzinfo=datetime.timezone.utc))
    with pytest.raises(ValueError,
                       match='^deliveryDate must be an ISO 8601 date-time$'):
        parseIsoInstant(PII_SENTINEL, 'deliveryDate')

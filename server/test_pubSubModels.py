"""Tests for pubSubModels.py, the generated Pub/Sub message models.

The first group checks this repo: every server/objects_<publisher>.py is what
its publisher's committed server/pubsub_models.json renders to (an absent
sibling, or one that has no manifest yet, skips and says why), no generated
file is left that nothing generates, the generated modules import, this
repo's own manifest is fresh (that test imports the app), every publisher
declares a payload model, and the manifest renders for every consumer. They
skip when this file is not inside a Star App server/ folder.

The second group runs the tool on small manifests, fake modules and fake git
fleets written into tmp_path. Every fixture name is fake on purpose:
test_route_coverage.py counts '/'-prefixed strings in test files as route
coverage, and test_pubSubContracts.py counts names referenced in test files
as tested handlers.
"""
import ast
import datetime
import importlib
import json
import random
import sys
import textwrap
import traceback
from pathlib import Path

import pytest

import pubSubMap
import pubSubMapScan
import pubSubMapSection
import pubSubModels
from pubSubModels import GENERATED_HEADER, MANIFEST, ModelsError
# This repo's facts and the fake-fleet helpers, shared with the map's tests
# (_fakeRepoTemplates is an autouse fixture: importing it turns it on here).
from test_pubSubMap import (APPCONFIG, IN_STAR_APP, MARKER_TOOL, NOT_IN_STAR_APP, OWN, PROJECT_ROOT, SERVER_DIR,
                            SNAPSHOT, _commit, _committedRepos, _fakeRepoTemplates, _git, _gitState, _service,
                            _src, _writeRepo)

REGENERATE = pubSubModels.REGENERATE
PUBLISHERS = sorted(set((SNAPSHOT or {}).get('repos', {})) - {OWN}) or ['(none)']
HANDLERS = sorted(((SNAPSHOT or {}).get('repos', {}).get(OWN) or {}).get('handlers', {})) or ['(none)']
_loaded = pubSubModels.loadedModule


def _messageClasses(module) -> dict:
    from pydantic import BaseModel
    out = {}
    for topic, name in module.TOPIC_MESSAGES.items():
        cls = getattr(module, name, None)
        assert isinstance(cls, type) and issubclass(cls, BaseModel), f'{topic}: {name} is not a model'
        out[topic] = cls
    return out


# ----------------------------------------------------------------------------
# This repo
# ----------------------------------------------------------------------------

@pytest.fixture(scope='module')
def fleet():
    if not IN_STAR_APP:
        pytest.skip(NOT_IN_STAR_APP)
    fleet = pubSubMap.freshMap(PROJECT_ROOT)
    yield fleet
    pubSubMap.releaseTrees(fleet.sources)  # this module parsed this repo again


@pytest.fixture(scope='module')
def plan(fleet):
    try:
        return pubSubModels.planPull(PROJECT_ROOT, sources=fleet.sources, ownName=fleet.ownName)
    except ModelsError as e:
        pytest.fail(f'{e}\n{REGENERATE}', pytrace=False)


@pytest.mark.parametrize('publisher', PUBLISHERS)
def test_generatedModelFilesAreFresh(fleet, plan, publisher):
    """objects_<publisher>.py is exactly what the publisher's manifest, read
    at its committed ref, renders to (or absent when it names no topic for
    this repo). Never edit it: consumer logic belongs in helpers."""
    src = fleet.sources.get(publisher)
    if src is None or src.section is None:
        pytest.skip(f'{publisher} not checked: {src.reason if src else "not in the fleet"}')
    if publisher in plan.missing:
        pytest.skip(f'{publisher} not checked: {plan.missing[publisher]}')
    name = pubSubModels.generatedFileName(publisher)
    path = SERVER_DIR / name
    current = pubSubMap.readLF(path) if pubSubModels._hasHeader(path) else None
    assert current == plan.writes.get(name), (
        f'server/{name} is not what {publisher}\'s server/{MANIFEST} ({src.dirName} at {src.ref}) renders '
        f'to. Never edit it by hand. {REGENERATE}')


def test_noStrayGeneratedFiles(plan):
    """Every file carrying the generated header is one a publisher generates."""
    assert not plan.deletes, ('Generated files no publisher generates any more: '
                              + ', '.join(f'server/{n}' for n in plan.deletes) + f'. {REGENERATE}')
    assert set(plan.writes) <= {pubSubModels.generatedFileName(p) for p in PUBLISHERS}


def test_generatedModulesImport():
    """Each header-carrying module holds only what the renderer writes (so a
    hand-written file cannot borrow the header to dodge test_file_sizes.py),
    imports under this interpreter, and maps every topic to a pydantic model."""
    if not IN_STAR_APP:
        pytest.skip(NOT_IN_STAR_APP)
    for path in pubSubModels._generatedPaths(SERVER_DIR):
        problems = pubSubModels.generatedShapeProblems(path.read_text(encoding='utf-8'))
        assert not problems, f'server/{path.name} is not what pubSubModels.py writes: {problems}. {REGENERATE}'
        module = importlib.import_module(path.stem)
        assert _messageClasses(module), f'server/{path.name} has no TOPIC_MESSAGES'


@pytest.fixture(scope='module')
def ownManifest(fleet):
    """(this repo's freshly built manifest, the ModelsError text): one is None."""
    try:
        return pubSubModels.buildManifest(PROJECT_ROOT, fleet.sources[fleet.ownName].section), None
    except ModelsError as e:
        return None, str(e)


def test_everyPublisherDeclaresAPayloadModel(ownManifest):
    """Each publisher has exactly one payload model: its route_config
    responseModel, or (a publisher that is no route) its module's
    PUBSUB_PAYLOAD_MODELS entry."""
    assert ownManifest[1] is None, ownManifest[1]


def test_manifestIsFresh(ownManifest):
    """server/pubsub_models.json is what this repo's publishers' models give
    under this interpreter (it imports the app)."""
    manifest, error = ownManifest
    assert error is None, error
    committed = pubSubMap.readLF(SERVER_DIR / MANIFEST)
    if committed != pubSubModels.renderManifest(manifest):
        old = json.loads(committed) if committed else {'topics': {}, '$defs': {}}
        changed = [k for part in ('topics', '$defs') for k in sorted(set(old.get(part, {})) | set(manifest[part]))
                   if old.get(part, {}).get(k) != manifest[part].get(k)]
        pytest.fail(f'server/{MANIFEST} is stale (changed: {", ".join(changed[:10]) or "layout"}). Run '
                    'AppCreator.py, or `cd server && ENVIRONMENT=localdev python pubSubModels.py '
                    '--write-manifest`, with the interpreter that runs this suite, and commit it.', pytrace=False)


def test_ownManifestRendersForEveryConsumer():
    """The committed manifest renders, for every consumer it names, to a
    module that parses as Python 3.10 and imports without a warning."""
    if not IN_STAR_APP:
        pytest.skip(NOT_IN_STAR_APP)
    text = pubSubMap.readLF(SERVER_DIR / MANIFEST)
    assert text is not None, f'server/{MANIFEST} is missing: run AppCreator.py and commit it'
    manifest = pubSubModels.parseManifest(text, OWN, f'server/{MANIFEST}')
    for consumer in pubSubModels.manifestConsumers(manifest):
        topics = pubSubModels.checkRendered(manifest, consumer)
        assert sorted(topics) == sorted(t for t, e in manifest['topics'].items() if consumer in e['consumers'])


@pytest.mark.parametrize('handler', HANDLERS)
def test_everyHandlerParsesItsGeneratedMessage(fleet, handler):
    """Each push handler parses its whole message once, with its generated
    objects_<publisher>.<Fn>Message, and reads nothing else of the raw
    payload outside an `except ValidationError` fallback on that parse
    (pubSubModels.handlerProblems). The handler list comes from the
    committed map, which must name every handler the fresh section finds."""
    section = fleet.sources[fleet.ownName].section
    # The section omits a field at its default: no handler, no 'handlers'.
    unlisted = sorted(set(pubSubMapSection.flag(section.data, 'handlers')) - set(HANDLERS))
    assert not unlisted, (f'Push handlers the committed map does not list, so this gate never checks them: '
                          f'{", ".join(unlisted)}. Run `cd server && python pubSubMap.py --write` and commit the map.')
    if handler == '(none)':
        pytest.skip('this repo has no push handler in its committed map')
    problems = pubSubModels.handlerProblems(section, handler)
    assert not problems, '\n'.join(problems)


# ----------------------------------------------------------------------------
# The renderer, on small manifests
# ----------------------------------------------------------------------------

def _topic(fn: str, model: str, consumers=('fakeShop',)) -> dict:
    return {'fn': fn, 'anchor': f'actions/FakeActions.py::FakeActions.{fn}', 'consumers': list(consumers),
            'prodPublish': True, 'payload': {'model': model, 'source': 'responseModel'}}


def _manifest(defs: dict, topics: dict | None = None, project: str = 'fakeAcct') -> dict:
    topics = topics if topics is not None else {f'makeWidget_{project}': _topic('makeWidget', 'FakeGizmo')}
    return {'formatVersion': 1, 'projectName': project, 'topics': topics, '$defs': defs}


def _gizmo(properties: dict, required=(), **extra) -> dict:
    return {'type': 'object', 'properties': properties, **({'required': list(required)} if required else {}),
            **extra}


def _render(manifest: dict, consumer: str = 'fakeShop') -> str:
    source = pubSubModels.renderModule(manifest, consumer)
    ast.parse(source, feature_version=(3, 10))
    assert source.endswith('\n') and '\r' not in source and source.isascii()
    assert pubSubModels.generatedShapeProblems(source) == []
    return source


def _classBody(source: str, cls: str) -> list:
    lines = source.splitlines()
    start = lines.index(f'class {cls}(_Message):') if f'class {cls}(_Message):' in lines else next(
        i for i, line in enumerate(lines) if line.startswith(f'class {cls}('))
    body = []
    for line in lines[start + 1:]:
        if line and not line.startswith('    '):
            break
        if line:
            body.append(line)
    return body


SUB = _gizmo({'n': {'type': 'integer'}}, ['n'])
SHARED_DEFS = {
    'FakeSub': SUB,
    'FakeKind': {'enum': ['plain', 'fancy'], 'type': 'string'},
    'FakeBag': {'type': 'object'},
    'FakeSubs': {'items': {'$ref': '#/$defs/FakeSub'}, 'type': 'array'},
}
NO_INVALID = object()

# (property schema, required, the field line, a valid value, an invalid one)
TYPE_RULES = [
    ({'$ref': '#/$defs/FakeSub'}, True, 'x: FakeSub', {'n': 1}, {'n': 'a'}),
    ({'$ref': '#/$defs/FakeKind'}, True, "x: Literal['plain', 'fancy']", 'fancy', 'other'),
    ({'$ref': '#/$defs/FakeBag'}, True, 'x: dict[str, Any]', {'k': [1]}, [1]),
    ({'$ref': '#/$defs/FakeSubs'}, True, 'x: list[FakeSub]', [{'n': 1}], [{'n': 'a'}]),
    ({'$ref': '#/$defs/FakeKind', 'default': 'plain', 'description': 'd'}, False,
     "x: Literal['plain', 'fancy'] = Field('plain', description='d')", 'plain', 'other'),
    ({'allOf': [{'$ref': '#/$defs/FakeSub'}], 'description': 'd'}, True, "x: FakeSub = Field(description='d')",
     {'n': 2}, {}),
    ({'anyOf': [{'type': 'null'}, {'type': 'string'}]}, True, 'x: str | None', None, 1),
    ({'oneOf': [{'type': 'integer'}, {'$ref': '#/$defs/FakeSub'}]}, True, 'x: int | FakeSub', {'n': 1}, 'a'),
    ({'format': 'date-time', 'type': 'string'}, True, 'x: _dt.datetime', '2024-01-02T03:04:05Z', 'soon'),
    ({'format': 'date', 'type': 'string'}, True, 'x: _dt.date', '2024-01-02', 'soon'),
    ({'format': 'time', 'type': 'string'}, True, 'x: _dt.time', '03:04:05', 'soon'),
    ({'format': 'email', 'type': 'string'}, True, 'x: str', 'a@b', 1),
    ({'type': 'integer'}, True, 'x: int', 1, 'a'),
    ({'type': 'number'}, True, 'x: float', 1.5, 'a'),
    ({'type': 'boolean'}, True, 'x: bool', True, 'maybe'),
    ({'items': {'type': 'integer'}, 'type': 'array'}, True, 'x: list[int]', [1], ['a']),
    ({'type': 'array'}, True, 'x: list[Any]', [1, 'a'], 'a'),
    ({'prefixItems': [{'type': 'integer'}, {'type': 'string'}], 'type': 'array'}, True, 'x: tuple[int, str]',
     [1, 'a'], [1, 2, 3]),
    ({'additionalProperties': {'type': 'integer'}, 'type': 'object'}, True, 'x: dict[str, int]', {'k': 1},
     {'k': 'a'}),
    ({}, True, 'x: Any', {'any': ['thing']}, NO_INVALID),
    ({'type': ['string', 'null']}, True, 'x: str | None', None, 1),
    ({'const': 'only'}, True, "x: Literal['only']", 'only', 'other'),
    ({'anyOf': [{'type': 'integer'}, {}]}, True, 'x: Any', 'a', NO_INVALID),
]


@pytest.mark.parametrize('schema, required, line, valid, invalid', TYPE_RULES)
def test_typeRules(schema, required, line, valid, invalid):
    from pydantic import ValidationError
    defs = dict(SHARED_DEFS, FakeGizmo=_gizmo({'x': schema}, ['x'] if required else ()))
    source = _render(_manifest(defs))
    assert '    ' + line in _classBody(source, 'FakeGizmo')
    with _loaded(source, 'objects_fakeAcctTypes') as module:
        module.MakeWidgetMessage.model_validate({'x': valid})
        if invalid is not NO_INVALID:
            with pytest.raises(ValidationError):
                module.MakeWidgetMessage.model_validate({'x': invalid})


@pytest.mark.parametrize('schema, match', [
    ({'allOf': [{'$ref': '#/$defs/FakeSub'}, {'$ref': '#/$defs/FakeBag'}]}, 'an allOf of 2 schemas'),
    ({'properties': {'a': {'type': 'integer'}}, 'type': 'object'}, 'an inline object with properties'),
    ({'items': {'type': 'integer'}, 'prefixItems': [{'type': 'integer'}], 'type': 'array'}, 'variadic tail'),
])
def test_shapesWithoutAPydanticFieldTypeAreErrors(schema, match):
    defs = dict(SHARED_DEFS, FakeGizmo=_gizmo({'x': schema}, ['x']))
    with pytest.raises(ModelsError, match=match):
        pubSubModels.renderModule(_manifest(defs), 'fakeShop')


# wire key -> the field name it gets
ALIASED_KEYS = {'_id': 'id', 'class': 'class_', 'json': 'json_', 'copy': 'copy_', 'model_config': 'model_config_',
                'model_name': 'model_name_', 'FakeItem': 'FakeItem_', 'list': 'list_', 'Field': 'Field_',
                'date': 'date', 'a-b': 'a_b', '1st': 'f_1st', '__v': 'v', '@type': 'type', '$schema': 'schema_',
                '-x': 'x', 'Config': 'Config_'}


def test_aliasesForUnderscoreKeywordShadowingAndClashingKeys():
    """Keys that cannot be field names (underscore-led, keywords, BaseModel
    attributes, model_*, a generated class or a name an annotation uses,
    no identifier at all) get a derived name and alias=; leading
    underscores go after the non-word characters became underscores. The
    module validates the wire keys and dumps them back (_loaded turns any
    warning into an error)."""
    properties = {key: {'type': 'string'} for key in ALIASED_KEYS}
    properties['date'] = {'format': 'date', 'type': 'string'}
    properties['item'] = {'$ref': '#/$defs/FakeItem'}
    defs = {'FakeItem': _gizmo({'n': {'type': 'integer'}}), 'FakeGizmo': _gizmo(properties, list(properties))}
    source = _render(_manifest(defs))
    body = _classBody(source, 'FakeGizmo')
    for key, name in ALIASED_KEYS.items():
        expected = f'alias={key!r}' if name != key else None
        line = next(line for line in body if line.startswith(f'    {name}: '))
        assert (expected in line) if expected else ('alias=' not in line), line
    wire = {key: 'v' for key in ALIASED_KEYS}
    wire.update({'date': '2024-01-02', 'item': {'n': 1}})
    with _loaded(source, 'objects_fakeAcctAliases') as module:
        message = module.MakeWidgetMessage.model_validate(wire)
        assert message.id == 'v' and message.class_ == 'v' and message.f_1st == 'v' and message.a_b == 'v'
        assert str(message.date) == '2024-01-02' and message.item.n == 1
        assert message.type == 'v' and message.schema_ == 'v' and message.x == 'v'
        dumped = message.model_dump(by_alias=True, mode='json')
        assert set(dumped) == set(wire) and dumped['_id'] == 'v'


def test_aConfigKeyCannotChangeTheModelConfig():
    """pydantic still reads a class-namespace Config: a key `Config` with a
    config-shaped default becomes the field Config_, and the model config
    stays the generated one."""
    defs = {'FakeGizmo': _gizmo({'Config': {'default': {'extra': 'allow', 'frozen': True}, 'type': 'object'},
                                 'x': {'type': 'string'}})}
    source = _render(_manifest(defs))
    assert "    Config_: dict[str, Any] = Field({'extra': 'allow', 'frozen': True}, alias='Config')" in \
        _classBody(source, 'FakeGizmo')
    with _loaded(source, 'objects_fakeAcctConfig') as module:
        cls = module.MakeWidgetMessage
        assert cls.model_config.get('extra') == 'ignore' and not cls.model_config.get('frozen')
        message = cls.model_validate({'x': 'a', 'unknown': 1})
        assert message.model_dump(by_alias=True) == {'Config': {'extra': 'allow', 'frozen': True}, 'x': 'a'}


def test_requiredReadOnlyAndDefaults():
    defs = {'FakeKind': {'default': 'plain', 'enum': ['plain', 'fancy'], 'type': 'string'}, 'FakeSub': SUB,
            'FakeGizmo': _gizmo({
                'need': {'type': 'string'},
                'computed': {'readOnly': True, 'type': 'number'},
                'kind': {'$ref': '#/$defs/FakeKind'},
                'count': {'default': 3, 'type': 'integer'},
                'tags': {'items': {'type': 'string'}, 'type': 'array'},
                'extra': {'additionalProperties': {'type': 'integer'}, 'type': 'object'},
                'note': {'type': 'string'},
                'sub': {'$ref': '#/$defs/FakeSub', 'default': {'n': 4}},
                'subs': {'default': [{'n': 5}], 'items': {'$ref': '#/$defs/FakeSub'}, 'type': 'array'},
                'when': {'default': '2024-01-02T00:00:00Z', 'format': 'date-time', 'type': 'string'},
                'maybe': {'default': None, 'type': 'integer'},
                'maybeSub': {'$ref': '#/$defs/FakeSub'},
            }, ['need', 'computed'])}
    source = _render(_manifest(defs))
    assert _classBody(source, 'FakeGizmo') == [
        '    need: str',
        '    computed: float | None = None',
        "    kind: Literal['plain', 'fancy'] = 'plain'",
        '    count: int = 3',
        '    tags: list[str] = Field(default_factory=list)',
        '    extra: dict[str, int] = Field(default_factory=dict)',
        '    note: str | None = None',
        "    sub: FakeSub = Field({'n': 4}, validate_default=True)",
        "    subs: list[FakeSub] = Field([{'n': 5}], validate_default=True)",
        "    when: _dt.datetime = Field('2024-01-02T00:00:00Z', validate_default=True)",
        '    maybe: int | None = None',
        '    maybeSub: FakeSub | None = None',
    ]
    from pydantic import ValidationError
    with _loaded(source, 'objects_fakeAcctDefaults') as module:
        first = module.MakeWidgetMessage.model_validate({'need': 'x'})
        second = module.MakeWidgetMessage.model_validate({'need': 'y', 'computed': 2.5})
        assert first.computed is None and second.computed == 2.5 and first.kind == 'plain'
        assert isinstance(first.sub, module.FakeSub) and first.sub.n == 4 and first.subs[0].n == 5
        assert first.when.year == 2024 and first.tags == [] and first.tags is not second.tags
        assert first.when.utcoffset() == datetime.timedelta(0), 'the Z default stays aware UTC'
        assert first.extra == {} and first.note is None and first.maybe is None and first.maybeSub is None
        with pytest.raises(ValidationError):
            module.MakeWidgetMessage.model_validate({'computed': 1.0})


def test_nonObjectDefsAreInlined():
    defs = dict(SHARED_DEFS, FakeGizmo=_gizmo({'kind': {'$ref': '#/$defs/FakeKind'},
                                               'bag': {'$ref': '#/$defs/FakeBag'},
                                               'subs': {'$ref': '#/$defs/FakeSubs'}}))
    source = _render(_manifest(defs))
    classes = [line.split('(')[0][len('class '):] for line in source.splitlines() if line.startswith('class ')]
    assert classes == ['_Message', 'FakeSub', 'FakeGizmo', 'MakeWidgetMessage']


@pytest.mark.parametrize('defs', [
    {'FakeTree': {'items': {'$ref': '#/$defs/FakeTree'}, 'type': 'array'}},
    {'FakeLeft': {'anyOf': [{'$ref': '#/$defs/FakeRight'}, {'type': 'null'}]},
     'FakeRight': {'items': {'$ref': '#/$defs/FakeLeft'}, 'type': 'array'}},
])
def test_aCycleOfNonObjectDefsIsAnError(defs):
    first = sorted(defs)[0]
    defs = dict(defs, FakeGizmo=_gizmo({'x': {'$ref': f'#/$defs/{first}'}}))
    with pytest.raises(ModelsError, match='a cycle of defs that are no classes'):
        pubSubModels.renderModule(_manifest(defs), 'fakeShop')


def _chain(n: int, link) -> dict:
    """FakeGizmo.x -> FakeD0 -> ... -> FakeD<n> (a string): each def is
    link(<ref to the next one>)."""
    defs = {'FakeGizmo': _gizmo({'x': {'$ref': '#/$defs/FakeD0'}}, ['x']), f'FakeD{n}': {'type': 'string'}}
    defs.update({f'FakeD{i}': link({'$ref': f'#/$defs/FakeD{i + 1}'}) for i in range(n)})
    return defs


def test_eachInlinedDefIsRenderedOnce(monkeypatch):
    """A sibling's manifest cannot make the render explode: a chain of defs
    that each refer twice to the next would take 2**n steps if a def were
    rendered per reference (counted, so a regression fails fast)."""
    calls = []
    refAnn = pubSubModels._Renderer._refAnn
    monkeypatch.setattr(pubSubModels._Renderer, '_refAnn',
                        lambda self, name, stack: calls.append(name) or refAnn(self, name, stack))
    doubling = _chain(16, lambda ref: {'anyOf': [ref, dict(ref)]})
    assert _classBody(_render(_manifest(doubling)), 'FakeGizmo') == ['    x: str']
    assert len(calls) <= 2 * 17, len(calls)


def test_aChainOfDefsTooDeepIsRefused():
    with pytest.raises(ModelsError, match='nest deeper than 64'):
        pubSubModels.renderModule(_manifest(_chain(2000, lambda ref: {'anyOf': [ref, {'type': 'null'}]})),
                                  'fakeShop')


def test_anAnnotationTooLongIsRefused():
    """A def whose annotation doubles at every level (list[X] | dict[str, X])
    is refused once it passes the cap, before it can grow further (the
    field cap would only catch it after rendering it whole)."""
    growing = _chain(14, lambda ref: {'anyOf': [{'items': ref, 'type': 'array'},
                                                {'additionalProperties': ref, 'type': 'object'}]})
    with pytest.raises(ModelsError, match='the annotation of def FakeD[0-9]+ is longer than 10000 characters'):
        pubSubModels.renderModule(_manifest(growing), 'fakeShop')


def test_aFieldOrAModuleTooLongIsRefused():
    """Each field's annotation and the whole module are capped too: one def
    under the cap, referred to many times, cannot amplify a small manifest
    into a huge file."""
    values = [f'fake{i:03d}' + 'v' * 20 for i in range(300)]
    defs = {'FakeLong': {'enum': values, 'type': 'string'}}
    defs['FakeGizmo'] = _gizmo({'x': {'prefixItems': [{'$ref': '#/$defs/FakeLong'}] * 2, 'type': 'array'}}, ['x'])
    with pytest.raises(ModelsError, match="the annotation of field 'x' is longer than 10000 characters"):
        pubSubModels.renderModule(_manifest(defs), 'fakeShop')
    defs['FakeGizmo'] = _gizmo({f'x{i}': {'$ref': '#/$defs/FakeLong'} for i in range(250)})
    with pytest.raises(ModelsError, match='the module rendered from fakeAcct is larger than 2000000 characters'):
        pubSubModels.renderModule(_manifest(defs), 'fakeShop')


def test_aRecursionErrorIsAModelsError():
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(traceback.extract_stack()) + 60)
    try:
        with pytest.raises(ModelsError, match='nest too deeply to render'):
            pubSubModels.renderModule(_manifest(_chain(60, lambda ref: {'items': ref, 'type': 'array'})),
                                      'fakeShop')
    finally:
        sys.setrecursionlimit(limit)


@pytest.mark.parametrize('project', ['fakecoding', 'fakeOther'])
def test_theHeaderCannotBeACodingCookie(project):
    """Line 1 and 2 of a generated file are constant: a comment there can be
    a PEP 263 coding cookie, so no project name goes on them."""
    topics = {f'makeWidget_{project}': _topic('makeWidget', 'FakeGizmo')}
    source = _render(_manifest({'FakeGizmo': SUB}, topics, project=project))
    assert source.splitlines()[:2] == [
        GENERATED_HEADER,
        "# The Pub/Sub payload models one publisher sends this repo, from its server/pubsub_models.json"]
    compile(source.encode('ascii'), f'objects_{project}.py', 'exec')


def test_recursiveModelsRebuildAndValidate():
    defs = {
        'FakeNode': _gizmo({'children': {'items': {'$ref': '#/$defs/FakeNode'}, 'type': 'array'}}),
        'FakeLeft': _gizmo({'right': {'anyOf': [{'$ref': '#/$defs/FakeRight'}, {'type': 'null'}]}}),
        'FakeRight': _gizmo({'left': {'$ref': '#/$defs/FakeLeft'}}, ['left']),
        'FakeGizmo': _gizmo({'node': {'$ref': '#/$defs/FakeNode'}, 'left': {'$ref': '#/$defs/FakeLeft'}},
                            ['node', 'left']),
    }
    source = _render(_manifest(defs))
    lines = source.splitlines()
    assert [line for line in lines if line.endswith('.model_rebuild()')] == [
        'FakeLeft.model_rebuild()', 'FakeNode.model_rebuild()', 'FakeRight.model_rebuild()']
    with _loaded(source, 'objects_fakeAcctRecursive') as module:
        message = module.MakeWidgetMessage.model_validate({
            'node': {'children': [{'children': [{}]}]}, 'left': {'right': {'left': {'right': None}}}})
        assert message.node.children[0].children[0].children == []
        assert message.left.right.left.right is None


def test_onlyTheClosureOfConsumedTopicsIsEmitted():
    defs = {'FakeSub': SUB, 'FakeGizmo': _gizmo({'sub': {'$ref': '#/$defs/FakeSub'}}),
            'FakeOther': _gizmo({'n': {'type': 'integer'}}), 'FakeThing': _gizmo({'o': {'$ref': '#/$defs/FakeOther'}})}
    topics = {'makeWidget_fakeAcct': _topic('makeWidget', 'FakeGizmo', ('fakeShop', 'fakeSales')),
              'makeThing_fakeAcct': _topic('makeThing', 'FakeThing', ('fakeSales',))}
    source = _render(_manifest(defs, topics))
    assert 'class FakeSub(' in source and 'class FakeGizmo(' in source
    assert 'FakeOther' not in source and 'FakeThing' not in source and 'makeThing' not in source
    with _loaded(source, 'objects_fakeAcctClosure') as module:
        assert module.TOPIC_MESSAGES == {'makeWidget_fakeAcct': 'MakeWidgetMessage'}
    assert 'class FakeThing(' in _render(_manifest(defs, topics), 'fakeSales')
    assert pubSubModels.renderModule(_manifest(defs, topics), 'fakeNobody') is None


def test_messageClassNameCollisions():
    """A def named like a topic's <Fn>Message becomes <Name>Payload; a
    reserved or keyword name becomes <Name>Model; a taken name gets _2."""
    defs = {
        'MakeWidgetMessage': _gizmo({'thing': {'$ref': '#/$defs/MakeThingMessage'},
                                     'field': {'$ref': '#/$defs/Field'},
                                     'odd': {'$ref': '#/$defs/class'},
                                     'msg': {'$ref': '#/$defs/_Message'},
                                     'taken': {'$ref': '#/$defs/FieldModel'}}),
        'MakeThingMessage': _gizmo({'n': {'type': 'integer'}}),
        'Field': _gizmo({'n': {'type': 'integer'}}),
        'FieldModel': _gizmo({'n': {'type': 'integer'}}),
        'class': _gizmo({'n': {'type': 'integer'}}),
        '_Message': _gizmo({'n': {'type': 'integer'}}),
    }
    topics = {'makeWidget_fakeAcct': _topic('makeWidget', 'MakeWidgetMessage'),
              'makeThing_fakeAcct': _topic('makeThing', 'MakeThingMessage')}
    source = _render(_manifest(defs, topics))
    assert _classBody(source, 'MakeWidgetMessagePayload')[:5] == [
        '    thing: MakeThingMessagePayload | None = None', '    field: FieldModel_2 | None = None',
        '    odd: classModel | None = None',
        '    msg: _MessageModel | None = None', '    taken: FieldModel | None = None']
    with _loaded(source, 'objects_fakeAcctNames') as module:
        assert issubclass(module.MakeWidgetMessage, module.MakeWidgetMessagePayload)
        assert issubclass(module.MakeThingMessage, module.MakeThingMessagePayload)
        assert module.MakeWidgetMessage.model_validate({'field': {'n': 1}}).field.n == 1


def test_descriptionsDefaultsAndLiteralsAreEscapedAscii():
    """Every string reaches the source as an ascii() literal: quotes,
    backslashes and newlines cannot end it, and the text is the same under
    every interpreter (_render checks it is ASCII)."""
    text = 'Say """hi""" \\ it\'s ' + "'''" + '\nnext line \u2014 dash\\n'
    defs = {'FakeGizmo': _gizmo({'x': {'description': text, 'type': 'string'},
                                 'y': {'default': text, 'type': 'string'},
                                 'z': {'default': 'caf\u00e9', 'enum': ['caf\u00e9', text], 'type': 'string'}},
                                ['x'], description=text)}
    source = _render(_manifest(defs))
    with _loaded(source, 'objects_fakeAcctDocs') as module:
        assert module.FakeGizmo.__doc__ == text
        assert module.FakeGizmo.model_fields['x'].description == text
        message = module.MakeWidgetMessage.model_validate({'x': 'a', 'z': text})
        assert message.y == text and message.z == text
        assert module.MakeWidgetMessage.model_validate({'x': 'a'}).z == 'caf\u00e9'


def test_injectedTextIsOnlyEverAStringLiteral(capsys):
    """Keys, descriptions, defaults and anchors from a sibling's manifest
    reach the module only as ascii() literals: none of them runs."""
    evil = "x'); print('INJECTED'); ('\n\"\"\"\\"
    defs = {'FakeGizmo': _gizmo({evil: {'default': {evil: [evil]}, 'description': evil, 'type': 'object'}},
                                description=evil)}
    topics = {'makeWidget_fakeAcct': dict(_topic('makeWidget', 'FakeGizmo'), anchor=evil)}
    manifest = pubSubModels.parseManifest(json.dumps(_manifest(defs, topics)), 'fakeAcct')
    source = _render(manifest)
    with _loaded(source, 'objects_fakeAcctEvil') as module:
        message = module.MakeWidgetMessage.model_validate({evil: {'k': 1}})
        assert message.model_dump(by_alias=True) == {evil: {'k': 1}}
        assert module.MakeWidgetMessage.model_validate({}).model_dump(by_alias=True) == {evil: {evil: [evil]}}
    assert 'INJECTED' not in capsys.readouterr().out


def _deterministicManifest() -> dict:
    defs = {'FakeSub': SUB, 'FakeKind': SHARED_DEFS['FakeKind'],
            'FakeGizmo': _gizmo({'sub': {'$ref': '#/$defs/FakeSub'}, 'kind': {'$ref': '#/$defs/FakeKind'}}),
            'FakeThing': _gizmo({'gizmo': {'$ref': '#/$defs/FakeGizmo'}, 'n': {'type': 'integer'}}, ['n'])}
    topics = {'makeWidget_fakeAcct': _topic('makeWidget', 'FakeGizmo', ('fakeShop', 'fakeSales')),
              'makeThing_fakeAcct': _topic('makeThing', 'FakeThing', ('fakeSales', 'fakeShop')),
              'makeAgain_fakeAcct': _topic('makeAgain', 'FakeGizmo')}
    return _manifest(defs, topics)


def test_renderIsDeterministic():
    manifest = _deterministicManifest()
    expected = _render(manifest)
    rng = random.Random(7)
    for _ in range(5):
        shuffled = dict(manifest)
        for part in ('topics', '$defs'):
            items = list(manifest[part].items())
            rng.shuffle(items)
            shuffled[part] = dict(items)
        shuffled['topics'] = {t: dict(e, consumers=list(reversed(e['consumers'])))
                              for t, e in shuffled['topics'].items()}
        assert _render(shuffled) == expected


def _hostile(change):
    manifest = json.loads(json.dumps(_deterministicManifest()))
    result = change(manifest)
    return result if isinstance(result, str) else json.dumps(manifest)


def _renameDef(old: str, new: str):
    def change(m):
        text = json.dumps(m).replace(f'#/$defs/{old}"', f'#/$defs/{new}"').replace(f'"{old}"', f'"{new}"')
        return text
    return change


def _deep(m):
    schema = {'type': 'string'}
    for _ in range(70):
        schema = {'items': schema, 'type': 'array'}
    m['$defs']['FakeGizmo']['properties']['deep'] = schema


HOSTILE = [
    (_renameDef('FakeSub', 'Fake-Sub'), "def name 'Fake-Sub' is not an ASCII identifier"),
    (_renameDef('FakeSub', 'Fak\u00e9Sub'), 'is not an ASCII identifier'),
    (_renameDef('FakeSub', '__builtins__'), "def name '__builtins__' is a dunder name"),
    (lambda m: m['$defs']['FakeGizmo']['properties'].update({'\x1b[2J': {'type': 'decimal'}}),
     "$defs.FakeGizmo.properties.'\\x1b[2J': type 'decimal' is not a JSON schema type"),
    (lambda m: m.update(projectName='fakeOther'), "projectName 'fakeOther' is not 'fakeAcct'"),
    (lambda m: m.update(projectName='fake-acct'), "projectName 'fake-acct' is not an identifier"),
    (lambda m: m.update(formatVersion=2), 'formatVersion 2 is not 1'),
    (lambda m: m.update(extra=1), 'expected exactly formatVersion'),
    (lambda m: m['topics']['makeWidget_fakeAcct'].update(fn='makeThing'), 'is not <fn>_fakeAcct'),
    (lambda m: m['topics'].update(_hidden_fakeAcct=_topic('_hidden', 'FakeGizmo')), 'is not <fn>_fakeAcct'),
    (lambda m: m['topics'].update(MakeWidget_fakeAcct=_topic('MakeWidget', 'FakeGizmo')),
     'would both generate MakeWidgetMessage'),
    (lambda m: m['topics']['makeWidget_fakeAcct'].update(consumers=['../fakeShop']), 'plain project names'),
    (lambda m: m['topics']['makeWidget_fakeAcct']['payload'].update(model='FakeKind'), 'is not an object def'),
    (lambda m: m['topics']['makeWidget_fakeAcct']['payload'].update(source='guessed'), 'source one of'),
    (lambda m: m['$defs']['FakeGizmo']['properties']['sub'].update({'$ref': '#/definitions/FakeSub'}),
     'is not a def of this manifest'),
    (lambda m: m['$defs']['FakeGizmo']['properties']['sub'].update({'$ref': 'http://fake.invalid/x'}),
     'is not a def of this manifest'),
    (lambda m: m['$defs']['FakeSub'].update(pattern='^a'), "unexpected keys ['pattern']"),
    (lambda m: m['$defs']['FakeSub'].update(type='decimal'), 'is not a JSON schema type'),
    (lambda m: m['$defs']['FakeSub'].update(required=['missing']), 'required must list properties'),
    (lambda m: json.dumps(m).replace('"type": "integer"}', '"type": "integer", "default": NaN}', 1),
     'NaN is not JSON'),
    (lambda m: json.dumps(m).replace('"type": "integer"}', '"type": "integer", "default": 1e999}', 1),
     'default is not a JSON value with finite numbers'),
    (lambda m: json.dumps(m).replace('"formatVersion": 1', '"formatVersion": 1, "formatVersion": 1'),
     "duplicate key 'formatVersion'"),
    (lambda m: m['$defs']['FakeKind'].update(enum=[{'a': 1}]), 'enum must be a non-empty list'),
    (_deep, 'nested deeper than 64'),
    (lambda m: '{"formatVersion": 1, ', 'not a valid manifest'),
]


@pytest.mark.parametrize('change, match', HOSTILE)
def test_hostileManifestsAreRefused(change, match):
    """A sibling's manifest is untrusted: anything renderModule could not
    turn into safe source is a ModelsError, never a partial render."""
    with pytest.raises(ModelsError) as caught:
        pubSubModels.parseManifest(_hostile(change), 'fakeAcct', 'fake-acct@main:server/' + MANIFEST)
    assert match in str(caught.value) and str(caught.value).startswith('fake-acct@main:server/')


def test_theManifestBuilderOutputPassesTheChecks():
    manifest = _deterministicManifest()
    assert pubSubModels.parseManifest(pubSubModels.renderManifest(manifest), 'fakeAcct') == manifest


# ----------------------------------------------------------------------------
# The map reads a generated module, and the handler rule
# ----------------------------------------------------------------------------

# The publisher's model, as source (for the map) and as a class (for the manifest).
PUBLISHER_MODELS = _src('''
    from pydantic import BaseModel, ConfigDict, Field


    class FakeLine(BaseModel):
        sku: str
        qty: int = 1


    class FakeMaker(BaseModel):
        label: str | None = None


    class FakeGizmo(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        id: str = Field(alias='_id')
        name: str
        size: float | None = None
        lines: list[FakeLine] = Field(default_factory=list)
        maker: FakeMaker
        spare: FakeMaker | None = None
        extras: list[FakeLine] | None = None
    ''')

PUBLISHER_ACTIONS = _src('''
    from pubSub import pubSubDecorator
    from route_config import route_config
    from models import FakeGizmo, FakeMaker
    from services.shopService import shopService


    class WidgetActions():

        @pubSubDecorator(projectNameConsumers=[shopService])
        @route_config(httpMethod='POST', jwtRequired=True, responseModel=FakeGizmo)
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            return FakeGizmo(_id='a', name='b', maker=FakeMaker()).model_dump(by_alias=True)
    ''')


@pytest.fixture(scope='module')
def handlerManifest() -> dict:
    with _loaded(PUBLISHER_MODELS, 'fakeAcctPublisherModels') as module:
        spec = pubSubModels.PublisherSpec('makeWidget', 'makeWidget_fakeAcct',
                                          'actions/WidgetActions.py::WidgetActions.makeWidget', ['fakeShop'],
                                          True, module.FakeGizmo, 'responseModel')
        return pubSubModels.manifestFrom('fakeAcct', [spec])


def _consumerSection(handlerManifest, body: str, head: str = 'from objects_fakeAcct import MakeWidgetMessage\n',
                     generated: str | None = None):
    psr = (head + 'from pydantic import ValidationError\nfrom route_config import route_config\n'
           'from fakeShopModels import FakeShopKit, FakeShopMaker, FakeShopRow, FakeShopWidget, parseFakeWidget\n\n\nclass FakeActions():\n\n'
           "    @route_config(httpMethod='POST', jwtRequired=False)\n"
           + textwrap.indent(_src(body), '    ') + '\n\nclass PubSubRequests(FakeActions):\n    pass\n')
    models = _src('''
        from pydantic import BaseModel
        from objects_fakeAcct import MakeWidgetMessage


        class FakeShopWidget(BaseModel):
            name: str


        class FakeShopRow(BaseModel):
            label: str


        class FakeShopMaker(BaseModel):
            label: str


        class FakeShopKit(BaseModel):
            spare: FakeShopMaker


        def parseFakeWidget(payload):
            return MakeWidgetMessage.model_validate(payload)
        ''')
    files = {'AppConfig.py': APPCONFIG.format(name='fakeShop'), 'services/acctService.py': _service('acctService'),
             'objects_fakeAcct.py': generated or pubSubModels.renderModule(handlerManifest, 'fakeShop'),
             'fakeShopModels.py': models, 'PubSubRequests.py': psr}
    return pubSubMapSection.buildSection(files)


def test_theMapReadsAGeneratedModule(handlerManifest):
    """The map's AST scanner understands a generated module: the handler's
    parse is one root model row anchored at it, the aliased _id (forwarded
    into a consumer model) is read by either key, no name is resolved by
    heuristic, and the flow is ok."""
    con = _consumerSection(handlerManifest, '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            return {'n': FakeShopWidget(name=message.id).name, 'k': [line.sku for line in message.lines]}
        ''')
    rows = con.data['handlers']['makeWidget_fakeAcct']['requirements']
    assert [r for r in rows if r['path'] == '(root)'] == [
        {'path': '(root)', 'kind': 'model', 'model': 'objects_fakeAcct.py::MakeWidgetMessage', 'nonNull': True}]
    assert next(r for r in rows if r['path'] == '_id')['altKeys'] == [['id']]
    assert not con.data.get('heuristicNames') and not con.data.get('ambiguousNames')
    compat = _flowCompat(con)
    assert compat['verdict'] == 'ok' and 'issue' not in compat['counts'], compat
    assert pubSubModels.handlerProblems(con, 'makeWidget_fakeAcct') == []


def _flowCompat(con) -> dict:
    """The compat of makeWidget (PUBLISHER_MODELS' FakeGizmo) against the
    fake consumer section `con`."""
    pub = pubSubMapSection.buildSection({
        'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'models.py': PUBLISHER_MODELS,
        'actions/WidgetActions.py': PUBLISHER_ACTIONS, 'services/shopService.py': _service('shopService')})
    pubSrc, conSrc = pubSubMap.RepoSource('fakeAcct', 'fake-acct'), pubSubMap.RepoSource('fakeShop', 'fake-shop')
    pubSrc.section, conSrc.section = pub, con
    return pubSubMap._evaluateFlow(pubSrc, 'makeWidget', conSrc, 'makeWidget_fakeAcct', None)[0]


def test_aGeneratedModelParseIsOneRequirementInTheMap(handlerManifest):
    """The map expands no field of a generated model: it is the publisher's
    own schema (its freshness tests guard drift), so its parse is the root
    row alone. A hand-written model of the same message still expands."""
    generated = _consumerSection(handlerManifest, '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            return {'n': message.name, 'k': [line.sku for line in message.lines]}
        ''')
    assert generated.data['handlers']['makeWidget_fakeAcct']['requirements'] == [
        {'path': '(root)', 'kind': 'model', 'model': 'objects_fakeAcct.py::MakeWidgetMessage', 'nonNull': True}]
    handWritten = _consumerSection(handlerManifest, '''
        def makeWidget_fakeAcct(self, data):
            return {'n': FakeShopWidget.model_validate(data).name}
        ''')
    rows = handWritten.data['handlers']['makeWidget_fakeAcct']['requirements']
    assert [(r['path'], r.get('kind', 'key')) for r in rows] == [('(root)', 'model'), ('name', 'key')]


def test_whatAHandlerForwardsOffAGeneratedModelIsStillChecked(handlerManifest):
    """A value read off the generated message and forwarded into the
    consumer's own model keeps its row (forwardedTo, nonNull), so the
    publisher's nullable maker.label against a non-null field is an issue."""
    con = _consumerSection(handlerManifest, '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            return FakeShopRow(label=message.maker.label).model_dump()
        ''')
    rows = con.data['handlers']['makeWidget_fakeAcct']['requirements']
    assert [r for r in rows if r['path'] != '(root)'] == [
        {'path': 'maker.label', 'forwardedTo': 'fakeShopModels.py::FakeShopRow.label', 'nonNull': True,
         'consumerType': 'str'}]
    compat = _flowCompat(con)
    assert compat['verdict'] == 'issues' and [(r['path'], r['issue']) for r in compat['rows']] == [
        ('maker.label', 'nullable')]


# Ways a handler reaches the publisher's nullable `spare` (FakeMaker | None)
# through the generated model: each needs it non-null, so each is a nullable
# issue against the publisher.
NULLABLE_SUB_MODEL_READS = {
    'attribute': "return {'n': message.spare.label}",
    'subModelDump': 'return FakeShopMaker.model_validate(message.spare.model_dump(by_alias=True)).model_dump()',
    'wholeMessageDump': 'return FakeShopKit(**message.model_dump(by_alias=True)).model_dump()',
    'forwardedSubModel': 'return FakeShopKit(spare=message.spare).model_dump()',
}


@pytest.mark.parametrize('case', sorted(NULLABLE_SUB_MODEL_READS))
def test_aNullableSubModelReadOffAGeneratedModelIsChecked(handlerManifest, case):
    con = _consumerSection(handlerManifest, 'def makeWidget_fakeAcct(self, data):\n'
                           '    message = MakeWidgetMessage.model_validate(data)\n'
                           f'    {NULLABLE_SUB_MODEL_READS[case]}\n')
    compat = _flowCompat(con)
    assert compat['verdict'] == 'issues'
    assert ('spare', 'nullable') in [(r['path'], r.get('issue')) for r in compat['rows']], compat


@pytest.mark.parametrize('body', ["return {'k': [line.sku for line in message.extras]}",
                                  "return {'k': message.extras[0].sku}"])
def test_aNullableListOfSubModelsReadOffAGeneratedModelIsChecked(handlerManifest, body):
    con = _consumerSection(handlerManifest, 'def makeWidget_fakeAcct(self, data):\n'
                           '    message = MakeWidgetMessage.model_validate(data)\n'
                           f'    {body}\n')
    compat = _flowCompat(con)
    assert ('extras', 'nullable') in [(r['path'], r.get('issue')) for r in compat['rows']], compat


@pytest.mark.parametrize('guard', ['message.spare', 'message.spare is not None'])
def test_aGuardedNullableSubModelIsNoIssue(handlerManifest, guard):
    con = _consumerSection(handlerManifest, 'def makeWidget_fakeAcct(self, data):\n'
                           '    message = MakeWidgetMessage.model_validate(data)\n'
                           f"    return {{'n': message.spare.label if {guard} else None}}\n")
    assert _flowCompat(con)['verdict'] == 'ok'


PASSING_HANDLERS = {
    'modelValidate': ('', '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            return {'n': message.name}
        '''),
    'starStar': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': MakeWidgetMessage(**data).name}
        '''),
    'aliasedModule': ('import objects_fakeAcct as acct\n', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': acct.MakeWidgetMessage.model_validate(data).name}
        '''),
    'aliasedClass': ('from objects_fakeAcct import MakeWidgetMessage as FakeMessage\n', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': FakeMessage.model_validate(data).name}
        '''),
    'strictParse': ('', '''
        def makeWidget_fakeAcct(self, data):
            try:
                message = MakeWidgetMessage.model_validate(data, strict=True)
            except ValidationError:
                return {'skipped': True}
            return {'n': message.name}
        '''),
    'readsInExceptValidationError': ('', '''
        def makeWidget_fakeAcct(self, message):
            try:
                parsed = MakeWidgetMessage.model_validate(message)
            except ValidationError:
                return {'skipped': isinstance(message, dict) and message.get('_id')}
            return {'n': parsed.name}
        '''),
}

FAILING_HANDLERS = {
    'handWrittenClass': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': FakeShopWidget.model_validate(data).name}
        ''', 'one parse of the whole message with objects_fakeAcct.py::MakeWidgetMessage'),
    'subKeyParse': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': MakeWidgetMessage.model_validate(data['inner']).name}
        ''', 'must be read exactly once'),
    'parseWithStarStarKeywords': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': MakeWidgetMessage.model_validate(data, **FAKE_OPTIONS).name}
        ''', 'must be read exactly once'),
    'dataInAKeyword': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': MakeWidgetMessage.model_validate(data, context=data).name}
        ''', 'must be read exactly once'),
    'extraRawRead': ('', '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            return {'n': message.name, 'raw': data['name']}
        ''', 'at line(s)'),
    'delegation': ('', '''
        def makeWidget_fakeAcct(self, data):
            return self._fakeHelper(data)

        def _fakeHelper(self, payload):
            return {'n': MakeWidgetMessage.model_validate(payload).name}
        ''', 'must be read exactly once'),
    'lenientExceptException': ('', '''
        def makeWidget_fakeAcct(self, data):
            try:
                message = MakeWidgetMessage.model_validate(data)
            except Exception:
                return {}
            return {'n': message.name}
        ''', '(lenient)'),
    'helperParse': ('', '''
        def makeWidget_fakeAcct(self, data):
            return {'n': parseFakeWidget(data).name}
        ''', 'must be read exactly once'),
    'reboundData': ('', '''
        def makeWidget_fakeAcct(self, data):
            data = dict(data)
            return {'n': MakeWidgetMessage.model_validate(data).name}
        ''', 'rebinds data'),
    'readsInExceptValueError': ('', '''
        def makeWidget_fakeAcct(self, data):
            try:
                message = MakeWidgetMessage.model_validate(data)
            except ValueError:
                return {'skipped': data.get('_id')}
            return {'n': message.name}
        ''', 'must be read exactly once'),
    'readsInExceptKeyErrorTuple': ('', '''
        def makeWidget_fakeAcct(self, data):
            try:
                message = MakeWidgetMessage.model_validate(data)
            except (KeyError, TypeError):
                return {'skipped': data.get('_id')}
            return {'n': message.name}
        ''', 'must be read exactly once'),
    'readsInExceptValidationErrorTuple': ('', '''
        def makeWidget_fakeAcct(self, data):
            try:
                message = MakeWidgetMessage.model_validate(data)
            except (ValidationError, KeyError):
                return {'skipped': data.get('_id')}
            return {'n': message.name}
        ''', 'a tuple holding ValidationError, does not count'),
    'fallbackOfAnotherTry': ('', '''
        def makeWidget_fakeAcct(self, data):
            message = MakeWidgetMessage.model_validate(data)
            try:
                count = int(message.name)
            except ValidationError:
                return {'raw': data}
            return {'n': count}
        ''', 'does not hold the MakeWidgetMessage parse'),
}


@pytest.mark.parametrize('case', sorted(PASSING_HANDLERS))
def test_handlerRuleAcceptsAParseOfTheWholeMessage(handlerManifest, case):
    head, body = PASSING_HANDLERS[case]
    section = _consumerSection(handlerManifest, body, head=head + 'from objects_fakeAcct import MakeWidgetMessage\n')
    assert pubSubModels.handlerProblems(section, 'makeWidget_fakeAcct') == []


@pytest.mark.parametrize('case', sorted(FAILING_HANDLERS))
def test_handlerRuleRefusesAnyOtherUseOfTheMessage(handlerManifest, case):
    head, body, match = FAILING_HANDLERS[case]
    problems = pubSubModels.handlerProblems(_consumerSection(handlerManifest, body, head=head + (
        'from objects_fakeAcct import MakeWidgetMessage\n')), 'makeWidget_fakeAcct')
    assert problems and any(match in p for p in problems), problems
    assert all(p.startswith('makeWidget_fakeAcct: ') and p.endswith(
        '`message = MakeWidgetMessage.model_validate(data)` (from objects_fakeAcct import MakeWidgetMessage), '
        'read everything from message, and move consumer logic to helpers.') for p in problems)


def test_handlerRuleNeedsTheGeneratedModule(handlerManifest):
    handWritten = pubSubModels.renderModule(handlerManifest, 'fakeShop').replace(GENERATED_HEADER, '# a copy', 1)
    section = _consumerSection(handlerManifest, PASSING_HANDLERS['modelValidate'][1], generated=handWritten)
    problems = pubSubModels.handlerProblems(section, 'makeWidget_fakeAcct')
    assert len(problems) == 1 and 'server/objects_fakeAcct.py is not a generated module' in problems[0]
    renamed = pubSubModels.renderModule(handlerManifest, 'fakeShop').replace(
        "'makeWidget_fakeAcct': 'MakeWidgetMessage'", "'makeWidget_fakeAcct': 'FakeGizmo'")
    section = _consumerSection(handlerManifest, PASSING_HANDLERS['modelValidate'][1], generated=renamed)
    assert ['TOPIC_MESSAGES does not map makeWidget_fakeAcct to MakeWidgetMessage' in p
            for p in pubSubModels.handlerProblems(section, 'makeWidget_fakeAcct')] == [True]


# ----------------------------------------------------------------------------
# The manifest builder (pydantic, fake modules; never this repo's app)
# ----------------------------------------------------------------------------

def _specs(*pairs, consumers=('fakeShop',)):
    return [pubSubModels.PublisherSpec(fn, f'{fn}_fakeAcct', f'fakeAcctWidgets.py::{fn}', consumers, True, model,
                                       'responseModel') for fn, model in pairs]


def test_manifestCombinesDefsAndReduces():
    import datetime
    from typing import Any
    from pydantic import BaseModel, Field

    class FakeSub(BaseModel):
        """A fake sub model."""
        n: int = Field(ge=0, title='Count', description='How many')

    class FakeGizmo(BaseModel):
        sub: FakeSub
        subs: list[FakeSub] = Field(default_factory=list)
        bag: dict[str, Any] = {}
        when: datetime.datetime | None = None
        code: str = Field('a', min_length=1, pattern='^[a-z]+$', examples=['abc'])

    class FakeOther(BaseModel):
        sub: FakeSub | None = None

    manifest = pubSubModels.manifestFrom('fakeAcct', _specs(('makeWidget', FakeGizmo), ('makeThing', FakeOther),
                                                            ('makeAgain', FakeGizmo)))
    assert sorted(manifest['$defs']) == ['FakeGizmo', 'FakeOther', 'FakeSub'], 'one combined $defs'
    assert list(manifest['topics']) == ['makeAgain_fakeAcct', 'makeThing_fakeAcct', 'makeWidget_fakeAcct']
    assert manifest['topics']['makeAgain_fakeAcct']['payload'] == {'model': 'FakeGizmo', 'source': 'responseModel'}
    text = pubSubModels.renderManifest(manifest)
    for noise in ('"title"', 'minimum', 'minLength', 'pattern', 'examples', 'additionalProperties'):
        assert noise not in text, noise
    gizmo = manifest['$defs']['FakeGizmo']
    assert list(gizmo['properties']) == ['sub', 'subs', 'bag', 'when', 'code'], 'declaration order kept'
    assert gizmo['properties']['when']['anyOf'][0] == {'format': 'date-time', 'type': 'string'}
    assert manifest['$defs']['FakeSub']['description'] == 'A fake sub model.'
    assert text.endswith('}\n') and '\r' not in text and text.isascii()
    assert pubSubModels.parseManifest(text, 'fakeAcct') == manifest


def test_secretKeysAreStrippedAtEveryDepth():
    from pydantic import BaseModel

    class FakeInner(BaseModel):
        fakeSecret: str
        keep: int

    class FakeVault(BaseModel):
        fakeSecret: str
        inner: FakeInner
        rows: list[FakeInner]

    manifest = pubSubModels.manifestFrom('fakeAcct', _specs(('makeWidget', FakeVault)), frozenset({'fakeSecret'}))
    assert 'fakeSecret' not in pubSubModels.renderManifest(manifest)
    assert manifest['$defs']['FakeInner']['properties'] == {'keep': {'type': 'integer'}}
    assert manifest['$defs']['FakeInner']['required'] == ['keep']


def test_nonIdentifierDefNamesAreRenamed():
    from pydantic import BaseModel, create_model
    odd = create_model('Fake-Odd', n=(int, ...))
    clash = create_model('Fake_Odd', n=(str, ...))
    gizmo = create_model('FakeGizmo', odd=(odd, ...), clash=(clash, ...), __base__=BaseModel)
    manifest = pubSubModels.manifestFrom('fakeAcct', _specs(('makeWidget', gizmo)))
    assert sorted(manifest['$defs']) == ['FakeGizmo', 'Fake_Odd', 'Fake_Odd_2']
    assert manifest['$defs']['FakeGizmo']['properties']['odd'] == {'$ref': '#/$defs/Fake_Odd_2'}
    assert manifest['$defs']['FakeGizmo']['properties']['clash'] == {'$ref': '#/$defs/Fake_Odd'}
    assert pubSubModels._identifierNames({'a.b': {}, '1x': {}, 'a_b': {}}) == {
        'a_b': 'a_b', '1x': 'M_1x', 'a.b': 'a_b_2'}


def _serializedModels(kind: str, annotated: bool):
    """A FakeGizmo whose FakePaint (one level down) serializes its colour
    to a str through `kind`, with or without a return type."""
    from typing import Annotated
    from pydantic import BaseModel, PlainSerializer, WrapSerializer, field_serializer, model_serializer

    def paint(v):
        return v.get('shade') if isinstance(v, dict) else v

    def paintModel(self):
        return {'shade': self.shade}

    def wrapPaint(v, handler):
        return paint(v)
    if annotated:
        paint.__annotations__['return'] = 'str | None'
        paintModel.__annotations__['return'] = 'dict[str, str]'
        wrapPaint.__annotations__['return'] = 'str | None'
    if kind == 'field_serializer':
        class FakePaint(BaseModel):
            shade: dict[str, str] | None = None
            _paint = field_serializer('shade')(paint)
    elif kind == 'model_serializer':
        class FakePaint(BaseModel):
            shade: dict[str, str] | None = None
            _paint = model_serializer(paintModel)
    elif kind == 'PlainSerializer':
        class FakePaint(BaseModel):
            shade: Annotated[dict[str, str] | None, PlainSerializer(paint)] = None
    else:
        class FakePaint(BaseModel):
            shade: Annotated[dict[str, str] | None, WrapSerializer(wrapPaint)] = None

    class FakeGizmo(BaseModel):
        paint: FakePaint | None = None
    return FakeGizmo


@pytest.mark.parametrize('kind, where', [('field_serializer', 'FakePaint.shade: field_serializer _paint'),
                                         ('model_serializer', 'FakePaint: model_serializer _paint'),
                                         ('PlainSerializer', 'FakePaint.shade: PlainSerializer'),
                                         ('WrapSerializer', 'FakePaint.shade: WrapSerializer')])
def test_aSerializerWithoutAReturnTypeIsRefused(kind, where):
    """Without a return type pydantic's serialization schema keeps the
    declared type, so the manifest would promise consumers a shape the
    publisher never sends. Found at any depth, and named."""
    with pytest.raises(ModelsError) as caught:
        pubSubModels.manifestFrom('fakeAcct', _specs(('makeWidget', _serializedModels(kind, annotated=False))))
    assert (f'fakeAcctWidgets.py::makeWidget: its payload model FakeGizmo has a serializer without a return '
            f'annotation ({where}): the manifest would state the declared type') in str(caught.value)
    manifest = pubSubModels.manifestFrom('fakeAcct', _specs(('makeWidget', _serializedModels(kind, annotated=True))))
    assert 'FakeGizmo' in manifest['$defs']
    if kind != 'model_serializer':
        assert manifest['$defs']['FakePaint']['properties']['shade']['anyOf'][0] == {'type': 'string'}


FAKE_ROUTES = _src('''
    def route_config(**kwargs):
        def wrap(func):
            func.responseModel = kwargs.get('responseModel')
            return func
        return wrap
    ''')

FAKE_PUBSUB = _src('''
    _pubsubTopicRegistry = []


    def pubSubDecorator(projectNameConsumers, publishToProdPubSub=True):
        def wrap(func):
            _pubsubTopicRegistry.append((func.__name__, publishToProdPubSub))
            return func
        return wrap
    ''')

FAKE_MODELS = _src('''
    from pydantic import BaseModel, RootModel


    class FakeGizmo(BaseModel):
        name: str


    class FakeRow(BaseModel):
        n: int


    class FakeRows(RootModel[list[FakeRow]]):
        pass


    class FakeTree(RootModel[list['FakeTree']]):
        pass


    class FakeForest(BaseModel):
        tree: FakeTree
    ''')

FAKE_WIDGETS_HEAD = _src('''
    from fakeAcctPubSub import pubSubDecorator
    from fakeAcctPubSub import pubSubDecorator as publishes
    from fakeAcctRoutes import route_config
    from fakeAcctModels import FakeForest, FakeGizmo, FakeRow, FakeRows

    shopService = None
    ''')


@pytest.fixture
def fakeApp(tmp_path, monkeypatch):
    """A fake publisher repo whose modules import under unique names; every
    module it loads leaves sys.modules after the test."""
    root = tmp_path / 'fake-acct'
    (root / 'server').mkdir(parents=True)

    def write(widgets: str, extra: dict | None = None) -> Path:
        return _writeRepo(root, {
            'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'fakeAcctRoutes.py': FAKE_ROUTES,
            'fakeAcctPubSub.py': FAKE_PUBSUB, 'fakeAcctModels.py': FAKE_MODELS,
            'fakeAcctWidgets.py': FAKE_WIDGETS_HEAD + _src(widgets),
            'services/shopService.py': _service('shopService'), **(extra or {})})

    monkeypatch.syspath_prepend(str(root / 'server'))
    before = set(sys.modules)
    yield write
    for name in set(sys.modules) - before:
        if name.startswith('fakeAcct'):
            del sys.modules[name]


def _collect(root: Path) -> list:
    return pubSubModels.collectPublishers(root, registryModule='fakeAcctPubSub')


def test_publishersAreReadFromTheAppByTheirMapAnchors(fakeApp):
    root = fakeApp('''

        class WidgetActions:

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config(responseModel=FakeGizmo)
            def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {'name': 'x'}


        @pubSubDecorator(projectNameConsumers=[shopService], publishToProdPubSub=False)
        def makeThing(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        PUBSUB_PAYLOAD_MODELS = {'makeThing': 'FakeRow'}
        ''')
    specs = {s.fn: s for s in _collect(root)}
    assert sorted(specs) == ['makeThing', 'makeWidget']
    assert (specs['makeWidget'].model.__name__, specs['makeWidget'].source) == ('FakeGizmo', 'responseModel')
    assert (specs['makeThing'].model.__name__, specs['makeThing'].source) == ('FakeRow', 'PUBSUB_PAYLOAD_MODELS')
    assert specs['makeThing'].anchor == 'fakeAcctWidgets.py::makeThing' and not specs['makeThing'].prodPublish
    assert specs['makeWidget'].consumers == ['fakeShop'] and specs['makeWidget'].topic == 'makeWidget_fakeAcct'


@pytest.mark.parametrize('flag, registered', [
    ('publishToProdPubSub=DEV_ONLY', False),
    ('publishToProdPubSub=0', 0),
    ('publishToProdPubSub=False', None),
    ('', None),
])
def test_aProdFlagTheMapReadsOtherwiseIsRefused(fakeApp, flag, registered):
    """The manifest and the startup topic check take prodPublish from the
    map, which reads only a literal False as off: a falsy value it cannot
    see would make the start check a topic the publisher never uses."""
    root = fakeApp(f'''

        DEV_ONLY = False


        @pubSubDecorator(projectNameConsumers=[shopService], {flag})
        def makeThing(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        PUBSUB_PAYLOAD_MODELS = {{'makeThing': 'FakeRow'}}
        ''')
    if registered is None:
        assert [s.prodPublish for s in _collect(root)] == [flag == '']
        return
    with pytest.raises(ModelsError) as caught:
        _collect(root)
    assert (f'makeThing: @pubSubDecorator registers publishToProdPubSub={registered!r} but the Pub/Sub map '
            'reads it otherwise') in str(caught.value)


def test_aPublisherWithoutAModelIsRefused(fakeApp):
    """One ModelsError lists every offender: no model at all, a model that is
    not a BaseModel, and a RootModel."""
    root = fakeApp('''

        class WidgetActions:

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config()
            def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {'name': 'x'}

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config(responseModel=dict)
            def makeDict(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {}

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config(responseModel=FakeRows)
            def makeRows(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {}
        ''')
    with pytest.raises(ModelsError) as caught:
        _collect(root)
    lines = str(caught.value).splitlines()
    assert lines[0] == 'Pub/Sub publishers without a usable payload model:'
    assert lines[1:] == [
        '  - fakeAcctWidgets.py::WidgetActions.makeDict: its payload model dict is not a pydantic BaseModel subclass',
        '  - fakeAcctWidgets.py::WidgetActions.makeRows: its payload model FakeRows is a RootModel: a payload is a '
        'dict with explicit keys',
        "  - fakeAcctWidgets.py::WidgetActions.makeWidget: no payload model. Give its @route_config a responseModel, "
        "or (a publisher that is no route) add {'makeWidget': '<Model>'} to PUBSUB_PAYLOAD_MODELS in "
        'server/fakeAcctWidgets.py, and build the return value through that model']


def test_aRedundantHatchIsRefused(fakeApp):
    root = fakeApp('''

        class WidgetActions:

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config(responseModel=FakeGizmo)
            def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {'name': 'x'}


        PUBSUB_PAYLOAD_MODELS = {'makeWidget': 'FakeGizmo'}
        ''')
    with pytest.raises(ModelsError, match=r'makeWidget: it has a route_config responseModel \(FakeGizmo\) and a '
                                          'PUBSUB_PAYLOAD_MODELS entry; delete the PUBSUB_PAYLOAD_MODELS entry'):
        _collect(root)


def test_aRegisteredPublisherTheMapMissedIsRefused(fakeApp):
    """A decorator the map's AST does not recognise (here an aliased import)
    still registers the topic: the registry catches it."""
    root = fakeApp('''

        @pubSubDecorator(projectNameConsumers=[shopService])
        def makeThing(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        @publishes(projectNameConsumers=[shopService])
        def makeHidden(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        PUBSUB_PAYLOAD_MODELS = {'makeThing': 'FakeRow'}
        ''')
    with pytest.raises(ModelsError, match='makeHidden is registered by @pubSubDecorator but the Pub/Sub map does '
                                          'not see it'):
        _collect(root)


def test_aModuleImportedFromElsewhereIsRefused(fakeApp, tmp_path, monkeypatch):
    root = fakeApp('''

        @pubSubDecorator(projectNameConsumers=[shopService])
        def makeThing(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        PUBSUB_PAYLOAD_MODELS = {'makeThing': 'FakeRow'}
        ''')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'fakeAcctWidgets.py').write_text('makeThing = None\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(elsewhere))
    with pytest.raises(ModelsError, match="loaded .*elsewhere.*; run this from that repo's server/ folder"):
        _collect(root)


def test_aModuleThatFailsToImportEndsTheScanWithWhatWasFound(fakeApp):
    """A publisher missing after its import is reported; a module that fails
    to import ends the scan there (with the database down, every later
    import would retry its connection), in the same ModelsError."""
    root = fakeApp('''

        @pubSubDecorator(projectNameConsumers=[shopService])
        def makeGone(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload


        del makeGone
        PUBSUB_PAYLOAD_MODELS = {'makeGone': 'FakeRow'}
        ''', extra={'fakeAcctBroken.py': FAKE_WIDGETS_HEAD + _src('''

        raise RuntimeError('fake import failure')


        @pubSubDecorator(projectNameConsumers=[shopService])
        def makeThing(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload
        '''), 'fakeAcctZed.py': FAKE_WIDGETS_HEAD + _src('''

        @pubSubDecorator(projectNameConsumers=[shopService])
        def makeZed(payload, publishToProdPubSub, publishToPubSubMockDb=False):
            return payload
        ''')})
    with pytest.raises(ModelsError) as caught:
        _collect(root)
    assert 'fakeAcctZed' not in sys.modules, 'nothing is imported after the failure'
    assert str(caught.value).splitlines()[1:] == [
        '  - fakeAcctWidgets.py::makeGone: not found after importing fakeAcctWidgets',
        '  - fakeAcctBroken.py::makeThing: importing fakeAcctBroken failed (RuntimeError: fake import failure); '
        'nothing after it was checked']


def test_aPayloadTheRendererCannotLoadIsRefusedAtThePublisher(fakeApp):
    """buildManifest renders and loads every consumer's module, so a payload
    shape no consumer could render (a recursive list RootModel) stops the
    publisher's AppCreator instead of every consumer's pull."""
    root = fakeApp('''

        class WidgetActions:

            @pubSubDecorator(projectNameConsumers=[shopService])
            @route_config(responseModel=FakeForest)
            def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
                return {'tree': []}
        ''')
    with pytest.raises(ModelsError, match='a cycle of defs that are no classes cannot be rendered: '
                                          'FakeTree -> FakeTree'):
        pubSubModels.buildManifest(root, registryModule='fakeAcctPubSub')


@pytest.mark.parametrize('environment', [None, 'cloudprod'])
def test_writeManifestNeedsALocalEnvironmentBeforeAnyImport(tmp_path, monkeypatch, capsys, environment):
    """--write-manifest imports the app, so it refuses to start without a
    local ENVIRONMENT, before anything is imported."""
    if environment is None:
        monkeypatch.delenv('ENVIRONMENT', raising=False)
    else:
        monkeypatch.setenv('ENVIRONMENT', environment)
    monkeypatch.setattr(pubSubModels, 'buildManifest', lambda *args, **kwargs: pytest.fail('imported the app'))
    assert pubSubModels.main(['--write-manifest'], tmp_path) == 2
    assert 'ENVIRONMENT=localdev python pubSubModels.py --write-manifest' in capsys.readouterr().err


def test_manifestWritesOnlyOnChangeAndRefusesASymlink(tmp_path, monkeypatch):
    server = tmp_path / 'server'
    server.mkdir()
    manifest = _deterministicManifest()
    assert pubSubModels.writeManifest(tmp_path, manifest) == [f'wrote server/{MANIFEST}']
    (server / MANIFEST).write_bytes(pubSubModels.renderManifest(manifest).replace('\n', '\r\n').encode('utf-8'))
    assert pubSubModels.writeManifest(tmp_path, manifest) == [f'server/{MANIFEST} unchanged'], 'CRLF is the same'
    isSymlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self.name == MANIFEST or isSymlink(self))
    with pytest.raises(pubSubMapScan.MapError, match='it or its folder is a symlink'):
        pubSubModels.writeManifest(tmp_path, dict(manifest, projectName='fakeOther'))


def test_generatedModelFilesAreTheOnesCarryingTheHeader(tmp_path):
    server = tmp_path / 'server'
    server.mkdir()
    body = 'TOPIC_MESSAGES = {}\n'
    files = {'objects_fakeA.py': GENERATED_HEADER + '\n' + body, 'objects_fakeB.py': '# hand-written\n' + body,
             'objects_fake-c.py': GENERATED_HEADER + '\n', 'objects_fakeD.py': (GENERATED_HEADER + '\n' + body).replace(
                 '\n', '\r\n'), 'objects_fakeE.py': chr(0xfeff) + GENERATED_HEADER + '\n', 'fakeF.py': GENERATED_HEADER + '\n',
             'objects_fakeG.py': GENERATED_HEADER + '\n' + body + 'def helper():\n    return 1\n'}
    for name, text in files.items():
        (server / name).write_bytes(text.encode('utf-8'))
    assert pubSubModels.generatedModelFiles(tmp_path) == [
        'server/objects_fakeA.py', 'server/objects_fakeD.py', 'server/objects_fakeE.py']


SHAPES = {
    'aDef': 'def helper():\n    return 1\n',
    'aLambda': "TOPIC_MESSAGES = {'a': (lambda: 1)}\n",
    'anImport': 'import os\n',
    'aMethod': 'class FakeGizmo(_Message):\n    def run(self):\n        return 1\n',
    'aCallInAField': "class FakeGizmo(_Message):\n    x: int = __import__('os').getpid()\n",
    'aCallInAnAnnotation': 'class FakeGizmo(_Message):\n    x: type(1)\n',
    'aDecoratedClass': '@staticmethod\nclass FakeGizmo(_Message):\n    pass\n',
    'aConfigOutsideMessage': "class FakeGizmo(_Message):\n    model_config = ConfigDict(extra='allow')\n",
    'aStringAnnotation': "class FakeGizmo(_Message):\n    x: \"print('RAN') or int\" = 0\n",
    'aStringInsideAnAnnotation': "class FakeGizmo(_Message):\n    x: list[\"print('RAN') or int\"] = []\n",
    'anotherStatement': "print('x')\n",
}


@pytest.mark.parametrize('case', sorted(SHAPES))
def test_aHeaderCarryingFileHoldsOnlyRendererShapes(tmp_path, case):
    """A file carrying the header counts as generated (exempt from the size
    limit) only when it holds what the renderer writes, so hand-written code
    cannot borrow the header."""
    source = GENERATED_HEADER + '\n' + SHAPES[case]
    assert pubSubModels.generatedShapeProblems(source), case
    (tmp_path / 'server').mkdir()
    (tmp_path / 'server' / 'objects_fakeAcct.py').write_text(source, encoding='utf-8')
    assert pubSubModels.generatedModelFiles(tmp_path) == []


# ----------------------------------------------------------------------------
# Pull, check and push on fake git fleets
# ----------------------------------------------------------------------------

EMPTY_PSR = 'class PubSubRequests():\n    pass\n'
FLEET_DEFS = {'FakeGizmo': _gizmo({'_id': {'type': 'string'}, 'name': {'type': 'string'}}, ['_id'])}


def _fleetManifest(consumers=('fakeShop', 'fakeSales'), name: dict | None = None) -> str:
    defs = json.loads(json.dumps(FLEET_DEFS))
    defs['FakeGizmo']['properties']['name'].update(name or {})
    return pubSubModels.renderManifest(_manifest(defs, {'makeWidget_fakeAcct': _topic('makeWidget', 'FakeGizmo',
                                                                                       consumers)}))


def _fleet(base: Path) -> dict:
    """fake-acct publishes makeWidget to fake-shop and fake-sales (its
    committed manifest says so); the consumers know fake-acct."""
    return _committedRepos(base, 'models', _buildFleet)


def _buildFleet(base: Path) -> dict:
    acctService = {'services/acctService.py': _service('acctService'), 'PubSubRequests.py': EMPTY_PSR}
    roots = {
        'fakeAcct': _writeRepo(base / 'fake-acct', {
            'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'PubSubRequests.py': EMPTY_PSR,
            'services/shopService.py': _service('shopService'), 'services/salesService.py': _service('salesService'),
            MANIFEST: _fleetManifest()}),
        'fakeShop': _writeRepo(base / 'fake-shop', dict(acctService, **{
            'AppConfig.py': APPCONFIG.format(name='fakeShop')})),
        'fakeSales': _writeRepo(base / 'fake-sales', dict(acctService, **{
            'AppConfig.py': APPCONFIG.format(name='fakeSales')})),
    }
    for root in roots.values():
        _commit(root)
    return roots


def _generated(root: Path, name: str = 'objects_fakeAcct.py') -> str | None:
    path = root / 'server' / name
    return path.read_text(encoding='utf-8') if path.is_file() else None


def _expected(manifestText: str, consumer: str = 'fakeShop') -> str:
    return pubSubModels.renderModule(pubSubModels.parseManifest(manifestText, 'fakeAcct'), consumer)


def test_pullReadsTheCommittedManifestOnly(tmp_path):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    committed = _fleetManifest()
    _writeRepo(acct, {MANIFEST: _fleetManifest(name={'description': 'uncommitted'})})
    lines = pubSubModels.pull(shop)
    assert lines[0] == 'wrote server/objects_fakeAcct.py'
    assert _generated(shop) == _expected(committed) and 'uncommitted' not in _generated(shop)
    _commit(acct, 'describe name')
    assert pubSubModels.pull(shop)[0] == 'wrote server/objects_fakeAcct.py'
    assert "description='uncommitted'" in _generated(shop)
    assert not [line for line in pubSubModels.pull(shop) if line.startswith('wrote')], 'unchanged: no write'


def test_pullKeepsTheFileOfAnAbsentSiblingAndSaysWhy(tmp_path):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    pubSubModels.pull(shop)
    before = _generated(shop)
    _git(acct, 'rm', '-q', f'server/{MANIFEST}')
    _commit(acct, 'not adopted any more')
    lines = pubSubModels.pull(shop)
    assert ('kept server/objects_fakeAcct.py (fakeAcct has no server/pubsub_models.json at refs/heads/main '
            '(not adopted yet))') in lines
    acct.rename(tmp_path / 'away')
    lines = pubSubModels.pull(shop)
    assert any(line.startswith('kept server/objects_fakeAcct.py (fakeAcct is absent here: no checkout at ')
               for line in lines), lines
    assert _generated(shop) == before and not [line for line in lines if line.startswith(('wrote', 'removed'))]


def test_pullDeletesOnlyStaleGeneratedFiles(tmp_path):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    pubSubModels.pull(shop)
    _writeRepo(shop, {'objects_fakeGone.py': GENERATED_HEADER + '\nX = 1\n', 'objects_fakeOld.py': 'X = 1\n'})
    _writeRepo(acct, {MANIFEST: _fleetManifest(consumers=('fakeSales',))})
    _commit(acct, 'drop fakeShop')
    lines = pubSubModels.pull(shop)
    assert lines[:2] == ['removed server/objects_fakeAcct.py (generated, and no publisher generates it any more)',
                         'removed server/objects_fakeGone.py (generated, and no publisher generates it any more)']
    assert _generated(shop) is None and _generated(shop, 'objects_fakeGone.py') is None
    assert _generated(shop, 'objects_fakeOld.py') == 'X = 1\n', 'a hand-written file is never deleted'


def test_pullRefusesToOverwriteAHandWrittenFile(tmp_path):
    roots = _fleet(tmp_path)
    shop = roots['fakeShop']
    _writeRepo(shop, {'objects_fakeAcct.py': 'X = 1\n', 'objects_fakeGone.py': GENERATED_HEADER + '\n'})
    with pytest.raises(ModelsError, match='server/objects_fakeAcct.py is hand-written, so pull will not overwrite '
                                          'it: move its non-message code'):
        pubSubModels.pull(shop)
    assert _generated(shop) == 'X = 1\n' and _generated(shop, 'objects_fakeGone.py') == GENERATED_HEADER + '\n'
    assert pubSubModels.main(['--pull'], shop) == 2


def test_pullWritesLfOnlyOnChangeAndNeverFollowsASymlink(tmp_path, monkeypatch):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    pubSubModels.pull(shop)
    path = shop / 'server' / 'objects_fakeAcct.py'
    assert b'\r\n' not in path.read_bytes()
    path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    assert not [line for line in pubSubModels.pull(shop) if line.startswith('wrote')], 'CRLF is the same text'
    assert b'\r\n' in path.read_bytes(), 'and the file is left alone'
    _writeRepo(acct, {MANIFEST: _fleetManifest(name={'description': 'changed'})})
    _commit(acct, 'change')
    before = path.read_bytes()
    isSymlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self.name == 'objects_fakeAcct.py' or isSymlink(self))
    with pytest.raises(pubSubMapScan.MapError, match='it or its folder is a symlink'):
        pubSubModels.pull(shop)
    assert path.read_bytes() == before


def test_pullReadsOnlyARegularManifestFile(tmp_path, monkeypatch):
    """A manifest committed as a symlink, or a symlinked one in a sibling's
    working tree, is refused, never followed."""
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    oid = _git(acct, 'hash-object', '-w', '--stdin', stdin='../elsewhere.json').strip()
    _git(acct, 'update-index', '--cacheinfo', f'120000,{oid},server/{MANIFEST}')
    _git(acct, 'commit', '-q', '-m', 'a symlinked manifest')
    with pytest.raises(ModelsError, match=f'server/{MANIFEST} in .* at refs/heads/main is not a regular file'):
        pubSubModels.pull(shop)
    isSymlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self.name == MANIFEST or isSymlink(self))
    with pytest.raises(ModelsError, match=f'{MANIFEST} is a symlink: not read'):
        pubSubModels.pull(shop, worktreeSiblings=True)
    assert _generated(shop) is None


def test_repoOverrideAndWorktreeSiblings(tmp_path, capsys):
    roots = _fleet(tmp_path)
    shop, moved = roots['fakeShop'], tmp_path / 'else' / 'acct@copy'
    moved.parent.mkdir()
    roots['fakeAcct'].rename(moved)
    assert pubSubModels.main(['--pull', '--repo', f'fakeAcct={moved}'], shop) == 0
    assert _generated(shop) == _expected(_fleetManifest())
    _writeRepo(moved, {MANIFEST: _fleetManifest(name={'description': 'in the working tree'})})
    pubSubModels.pull(shop, {'fakeAcct': (moved, None)})
    assert 'in the working tree' not in _generated(shop)
    pubSubModels.pull(shop, {'fakeAcct': (moved, None)}, worktreeSiblings=True)
    assert "description='in the working tree'" in _generated(shop)
    capsys.readouterr()
    assert pubSubModels.main(['--push', '--worktree-siblings'], shop) == 2
    assert '--worktree-siblings goes with --pull or the check only' in capsys.readouterr().err


def test_checkModeExitCodes(tmp_path, capsys):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    assert pubSubModels.main([], shop) == 1
    out = capsys.readouterr().out.splitlines()
    assert 'MISSING: server/objects_fakeAcct.py is not generated yet' in out
    assert 'source: fakeAcct: fake-acct@refs/heads/main, 1 topic(s), 1 for fakeShop' in out
    assert pubSubModels.main(['--write'], shop) == 0
    capsys.readouterr()
    assert pubSubModels.main([], shop) == 0
    path = shop / 'server' / 'objects_fakeAcct.py'
    path.write_text(path.read_text(encoding='utf-8') + '# edited\n', encoding='utf-8')
    assert pubSubModels.main([], shop) == 1
    assert 'STALE: server/objects_fakeAcct.py differs from a fresh render' in capsys.readouterr().out
    pubSubModels.pull(shop)
    _writeRepo(shop, {'objects_fakeGone.py': GENERATED_HEADER + '\n'})
    assert pubSubModels.main([], shop) == 1
    assert 'STRAY: server/objects_fakeGone.py is generated, but no publisher generates it any more' in \
        capsys.readouterr().out
    _writeRepo(acct, {MANIFEST: _fleetManifest().replace('"projectName": "fakeAcct"', '"projectName": "fakeX"')})
    _commit(acct, 'hostile')
    assert pubSubModels.main([], shop) == 2
    assert "ERROR: fake-acct@refs/heads/main:server/pubsub_models.json: projectName 'fakeX' is not 'fakeAcct'" in \
        capsys.readouterr().err


def _adopt(root: Path, marker: Path, tools=None) -> None:
    """Give a fake consumer this process's tools (so push may write it), and
    code that would leave `marker` if anything imported or ran it: its
    AppConfig.py, its services file and a server/json.py shadowing the
    stdlib module (MARKER_TOOL: push must only read a consumer)."""
    here = Path(pubSubModels.__file__).resolve().parent
    names = pubSubMapScan.TOOL_MODULES if tools is None else tools
    files = {name: (here / name).read_text(encoding='utf-8') for name in names}
    plant = MARKER_TOOL.format(marker=str(marker))
    for rel in ('AppConfig.py', 'services/acctService.py'):
        files[rel] = (root / 'server' / rel).read_text(encoding='utf-8').replace('\r\n', '\n') + plant
    files['json.py'] = plant
    _writeRepo(root, files)
    _commit(root, 'adopt the tools')


def test_pushWritesSameBranchConsumersAndSkipsTheRest(tmp_path, capsys):
    roots = _fleet(tmp_path / 'fleet')
    acct, shop, sales = roots['fakeAcct'], roots['fakeShop'], roots['fakeSales']
    marker = tmp_path / 'marker'
    _adopt(shop, marker)
    _adopt(sales, marker, tools=sorted(pubSubMapScan.TOOL_MODULES - {'pubSubModels.py'}))
    before = _gitState(roots.values())
    assert pubSubModels.main(['--push'], acct) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'fake-shop: wrote server/objects_fakeAcct.py' in out
    assert 'fake-sales: skipped (not adopted yet: no server/pubSubModels.py)' in out
    assert '  git -C ../fake-shop add server/objects_fakeAcct.py && git -C ../fake-shop commit' in out
    assert out[-1].startswith('Then run `cd server && python pubSubMap.py --write-all`')
    assert _generated(shop) == _expected(_fleetManifest()) and _generated(sales) is None
    assert _gitState(roots.values()) == before, 'HEADs unchanged and nothing staged'
    _adopt(sales, marker)
    _git(sales, 'checkout', '-q', '-b', 'elsewhere')
    assert pubSubModels.main(['--push'], acct) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'fake-shop: unchanged' in out and out[-1] == 'No generated model changed; nothing to commit.'
    assert ('fake-sales: skipped (fake-sales is on elsewhere, this repo is on main; switch it, or run '
            '`python pubSubMap.py --write` there)') in out
    _git(sales, 'checkout', '-q', 'main')
    assert pubSubModels.main(['--push'], acct) == 0
    assert 'fake-sales: wrote server/objects_fakeAcct.py' in capsys.readouterr().out.splitlines()
    assert not marker.exists(), 'no consumer code is run'


def test_pushWarnsAboutAnUncommittedManifest(tmp_path):
    roots = _fleet(tmp_path / 'fleet')
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    _adopt(shop, tmp_path / 'marker')
    _writeRepo(acct, {MANIFEST: _fleetManifest(name={'description': 'not committed'})})
    lines, code = pubSubModels.push(acct)
    assert code == 0 and lines[0] == ('WARNING: server/pubsub_models.json differs from HEAD; consumers read the '
                                      'committed one, so commit it and push again')
    assert _generated(shop) == _expected(_fleetManifest())
    _git(acct, 'checkout', '-q', '--detach')
    with pytest.raises(ModelsError, match='--push needs a branch'):
        pubSubModels.push(acct)


def test_pushReportsAConsumerErrorAndGoesOn(tmp_path):
    roots = _fleet(tmp_path / 'fleet')
    acct, shop, sales = roots['fakeAcct'], roots['fakeShop'], roots['fakeSales']
    for root in (shop, sales):
        _adopt(root, tmp_path / 'marker')
    _writeRepo(sales, {'objects_fakeAcct.py': '# hand-written\n'})
    lines, code = pubSubModels.push(acct)
    assert code == 2 and any(line.startswith('fake-sales: error (server/objects_fakeAcct.py is hand-written')
                             for line in lines)
    assert 'fake-shop: wrote server/objects_fakeAcct.py' in lines


def test_pushNeverRunsSiblingCode(tmp_path):
    roots = _fleet(tmp_path / 'fleet')
    marker = tmp_path / 'marker'
    for root in (roots['fakeShop'], roots['fakeSales']):
        _adopt(root, marker)
    _, code = pubSubModels.push(roots['fakeAcct'])
    assert code == 0 and not marker.exists()
    assert _generated(roots['fakeSales']) == _expected(_fleetManifest(), 'fakeSales')


def test_aFreshScaffoldFleetPasses(tmp_path):
    """Two new projects with no topics generate nothing and check clean;
    after the first topic, its manifest and a handler, a pull makes the
    handler pass the handler rule."""
    acct = _writeRepo(tmp_path / 'fake-acct', {
        'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'PubSubRequests.py': EMPTY_PSR,
        'services/shopService.py': _service('shopService'),
        MANIFEST: pubSubModels.renderManifest(_manifest({}, {}))})
    shop = _writeRepo(tmp_path / 'fake-shop', {
        'AppConfig.py': APPCONFIG.format(name='fakeShop'), 'PubSubRequests.py': EMPTY_PSR,
        'services/acctService.py': _service('acctService')})
    for root in (acct, shop):
        _commit(root)
        assert pubSubModels.check(root)[1] == 0
    assert not [line for line in pubSubModels.pull(shop) if line.startswith('wrote')]
    _writeRepo(acct, {MANIFEST: _fleetManifest(consumers=('fakeShop',))})
    _commit(acct, 'first topic')
    _writeRepo(shop, {'PubSubRequests.py': _src('''
        from objects_fakeAcct import MakeWidgetMessage
        from route_config import route_config


        class FakeActions():

            @route_config(httpMethod='POST', jwtRequired=False)
            def makeWidget_fakeAcct(self, data):
                message = MakeWidgetMessage.model_validate(data)
                return {'id': message.id}


        class PubSubRequests(FakeActions):
            pass
        ''')})
    assert pubSubModels.pull(shop)[0] == 'wrote server/objects_fakeAcct.py'
    assert pubSubModels.check(shop)[1] == 0
    section = pubSubMapSection.buildSection(pubSubMapScan.readWorkingTree(shop))
    assert pubSubModels.handlerProblems(section, 'makeWidget_fakeAcct') == []

"""Tests for the Pub/Sub map (pubSubMap.py, pubSubMapScan.py, pubSubMapContract.py,
pubSubMapSection.py).

The first group checks this repo against the fleet: the checked-in
pubsub_map.json and PUBSUB_MAP.md are fresh and laid out as pubSubMap.py
writes them, and no problem in the map (pubSubMap.problems) names this repo
as the one to act: every publisher has a handler in every consumer, every
handler has a publisher, every handler's message contract resolves,
pubSubAdmin.py can read every publisher, the escape hatches name real things,
and every payload satisfies its consumers' contracts (nothing suppresses a
compat issue). They skip when this file is not inside a Star App server/
folder.

The second group runs the tools on fake fleets written into tmp_path. Every
fixture name is fake on purpose: test_route_coverage.py counts '/'-prefixed
strings in test files as route coverage, and test_pubSubContracts.py counts
names referenced in test files as tested handlers.
"""
import ast
import io
import json
import os
import shutil
import subprocess
import textwrap
import tokenize
from pathlib import Path

import pytest

import pubSubMap
import pubSubMapContract
import pubSubMapScan
import pubSubMapSection

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
IN_STAR_APP = (SERVER_DIR / 'AppConfig.py').is_file() and (SERVER_DIR / 'PubSubRequests.py').is_file()
NOT_IN_STAR_APP = 'not inside a Star App server/ (no AppConfig.py and PubSubRequests.py beside this test)'
REGENERATE = pubSubMap.REGENERATE
TOOL_FILES = tuple(sorted(pubSubMapScan.TOOL_MODULES)) + ('test_pubSubMap.py', 'test_pubSubModels.py')
MAP_JSON, MAP_MD, NOTE = pubSubMap.MAP_JSON, pubSubMap.MAP_MD, pubSubMap.NOTE_SUFFIX
WROTE_MAP = [f'wrote server/{MAP_JSON}', f'wrote server/{MAP_MD}']


def _ownProjectName():
    if not IN_STAR_APP:
        return None
    return pubSubMapScan.readProjectName((SERVER_DIR / 'AppConfig.py').read_text(encoding='utf-8'))


OWN = _ownProjectName()
SNAPSHOT = pubSubMap.loadCheckedInMap(PROJECT_ROOT) if IN_STAR_APP else None
SIBLINGS = sorted(set((SNAPSHOT or {}).get('repos', {})) - {OWN}) or ['(none)']


@pytest.fixture(scope='module')
def fleet():
    if not IN_STAR_APP:
        pytest.skip(NOT_IN_STAR_APP)
    return pubSubMap.freshMap(PROJECT_ROOT)


def _fail(lines, header):
    assert not lines, header + '\n  ' + '\n  '.join(lines) + '\n' + REGENERATE


def _ownProblems(fleet, check: str) -> list:
    """The messages of the problems this repo must act on that fail the
    real-fleet test `check` (pubSubMap.FLEET_CHECKS; check mode fails on
    the same ones)."""
    return [p['message'] for p in pubSubMap.ownFailures(fleet.problems(), fleet.ownName, check)]


def _layoutErrors(projectRoot: Path) -> list:
    """Why the checked-in map files are not what their JSON renders to
    (line endings ignored: git may check them out with CRLF)."""
    snapshot = pubSubMap.loadCheckedInMap(projectRoot)
    if snapshot is None:
        return ['server/pubsub_map.json is missing or unreadable.']
    if snapshot.get('formatVersion') != pubSubMap.FORMAT_VERSION:
        return ['server/pubsub_map.json was written by another version of pubSubMap.py.']
    server, errors = pubSubMap.serverDir(projectRoot), []
    if pubSubMap.readLF(server / MAP_JSON) != pubSubMap.renderJson(snapshot):
        errors.append('server/pubsub_map.json is not laid out the way pubSubMap.py writes it (edited by hand?).')
    if pubSubMap.readLF(server / MAP_MD) != pubSubMap.renderMarkdown(snapshot):
        errors.append('server/PUBSUB_MAP.md was edited by hand or not regenerated with the JSON.')
    return errors


# ----------------------------------------------------------------------------
# This repo against the fleet
# ----------------------------------------------------------------------------

def test_mapIsFreshForThisRepo(fleet):
    """This repo's section, and every flow touching it, equal the checked-in map."""
    assert SNAPSHOT is not None, 'server/pubsub_map.json is missing or unreadable. ' + REGENERATE
    old = pubSubMap.incidentView(SNAPSHOT, fleet.ownName)
    new = pubSubMap.incidentView(fleet.data, fleet.ownName)
    assert old == new, (
        f'server/pubsub_map.json is stale for {fleet.ownName}; changed: '
        + ', '.join(pubSubMap.differingAnchors(old, new)) + '. ' + REGENERATE)


@pytest.mark.parametrize('sibling', SIBLINGS)
def test_mapIsFreshForSiblings(fleet, sibling):
    """A sibling's section and its other flows, read at its committed ref,
    equal the checked-in map. An absent sibling cannot be checked here."""
    src = fleet.sources.get(sibling)
    if src is None or src.section is None:
        pytest.skip(f'{sibling} not checked: {src.reason if src else "not in the fleet"}')
    old = pubSubMap.siblingView(SNAPSHOT or {'repos': {}, 'flows': []}, sibling, fleet.ownName)
    new = pubSubMap.siblingView(fleet.data, sibling, fleet.ownName)
    assert old == new, (
        f'server/pubsub_map.json is stale for {sibling} ({src.dirName} at {src.ref}); changed: '
        + ', '.join(pubSubMap.differingAnchors(old, new)) + '. ' + REGENERATE)


def test_markdownMatchesJson(fleet):
    """PUBSUB_MAP.md is exactly what pubsub_map.json renders to, and the JSON
    is laid out as pubSubMap.py writes it (CRLF checkouts are fine)."""
    _fail(_layoutErrors(PROJECT_ROOT), 'The checked-in map files:')


def test_ownPublishersHaveHandlersInEveryConsumer(fleet):
    _fail(_ownProblems(fleet, 'ownPublishers'), 'Publishers with a consumer that has no handler:')


def test_siblingPublishersAddressingThisRepoHaveHandlers(fleet):
    _fail(_ownProblems(fleet, 'addressedHere'), 'Messages addressed to this repo with no handler:')


def test_everyHandlerHasAKnownSourceAndAPublisher(fleet):
    _fail(_ownProblems(fleet, 'handlerSources'), 'Push handlers without a publisher:')


def test_everyHandlerMessageContractResolves(fleet):
    _fail(_ownProblems(fleet, 'contracts'), 'Unresolved handler contracts:')


def test_publishersAreWherePubSubAdminLooks(fleet):
    _fail(_ownProblems(fleet, 'pubSubAdmin'), 'Publishers pubSubAdmin.py would miss or misread:')


def test_handPublishSitesUseADecoratedTopic(fleet):
    _fail(_ownProblems(fleet, 'handPublish'), 'Hand-publish sites without a decorated topic:')


def test_escapeHatchesAreLive(fleet):
    _fail(_ownProblems(fleet, 'hatches'), 'Escape hatches that name nothing real:')


def test_payloadsSatisfyConsumerContracts(fleet):
    _fail(_ownProblems(fleet, 'compat'), 'Payloads that break a consumer contract:')


# ----------------------------------------------------------------------------
# Fake fleets
# ----------------------------------------------------------------------------

def test_everyProblemKindFailsARealFleetTest():
    covered = {kind for kinds, _ in pubSubMap.FLEET_CHECKS.values() for kind in kinds}
    assert covered == set(pubSubMap.PROBLEM_KINDS) - pubSubMap.INFO_KINDS


def _src(text: str) -> str:
    return textwrap.dedent(text).lstrip('\n')


APPCONFIG = "class Project:\n\n    def __init__(self):\n        self.projectName = {name!r}\n"

SERVICE = _src('''
    from .__abstractService import AbstractService, Path


    class {cls}(AbstractService):
        pass


    {var} = {cls}({project}cloudRunServiceName='x', localRepoDirName={dirName!r},
              path=Path(prod=None, dev='http://127.0.0.1:1'))
    ''')

MODELS = _src('''
    from typing import Literal, Optional
    from pydantic import BaseModel, ConfigDict, Field, RootModel, computed_field, field_validator


    class Keys(BaseModel):
        id: str = Field(..., alias='_id')


    class Base(BaseModel):
        id: str = Field(default_factory=lambda: 'x', alias='_id')
        version: int | None = Field(None, alias='_version')


    class Gizmo(Keys, Base):
        model_config = ConfigDict(populate_by_name=True)
        name: str
        colour: str = Field(None)
        size: Optional[float] = None
        kind: Literal['plain', 'fancy'] = 'plain'
        count: int = 0
        secret: str = Field('s', exclude=True)

        @computed_field
        @property
        def label(self) -> str:
            return self.name

        @field_validator('count', mode='before')
        @classmethod
        def _noneIsZero(cls, v):
            return 0 if v is None else v


    class Line(BaseModel):
        sku: str
        qty: int


    class Lines(RootModel[list[Line]]):
        pass


    class Kind(RootModel):
        root: Literal['a', 'b']


    class Strict(BaseModel):
        model_config = ConfigDict(extra='forbid')
        name: str
    ''')

# A publisher model that may send name=None, and a consumer that needs it.
SENT_WANTED = _src('''
    class Sent(BaseModel):
        skip: bool = False
        name: str | None = None


    class Wanted(BaseModel):
        name: str
    ''')

PSR_HEAD = _src('''
    from route_config import route_config
    from models import Gizmo, Line, Lines, Strict
    from helpers import readName, Wrapper
    ''')

HELPERS = _src('''
    from models import Gizmo


    def readName(payload):
        return payload['name']


    class Wrapper:

        def __init__(self, payload):
            self.payload = payload
            self._check()

        def _check(self):
            if not isinstance(self.payload.get('ref'), str):
                raise ValueError('bad ref')

        def run(self):
            return self.payload['body']['text']
    ''')

# var -> (projectName, localRepoDirName) of the fake fleet's services
SERVICES = {'shopService': ('fakeShop', 'fake-shop'), 'salesService': ('fakeSales', 'fake-sales'),
            'acctService': ('fakeAcct', 'fake-acct')}


def _service(var: str, project: str | None = None, dirName: str | None = None) -> str:
    known = SERVICES.get(var, (None, None))
    return SERVICE.format(cls=var[0].upper() + var[1:], var=var, project=f'projectName={project or known[0]!r}, ',
                          dirName=dirName or known[1])


def _methods(bodies, decorator: str) -> str:
    """Class-body source: each method in `bodies` under `decorator` ('' when
    the bodies carry their own)."""
    return ''.join(textwrap.indent(_src(decorator + _src(b)), '    ') + '\n' for b in bodies)


def _handlers(*bodies: str, className: str = 'FakeActions') -> str:
    """PubSubRequests.py whose `className` mixin holds the given handler methods."""
    return (PSR_HEAD + f'\n\nclass {className}():\n'
            + _methods(bodies, "@route_config(httpMethod='POST', jwtRequired=False)\n")
            + f'\nclass PubSubRequests({className}):\n    pass\n')


def _consumerFiles(*bodies: str, name: str = 'fakeShop', className: str = 'FakeActions',
                   extra: dict | None = None) -> dict:
    files = {'AppConfig.py': APPCONFIG.format(name=name), 'models.py': MODELS,
             'helpers.py': HELPERS, 'PubSubRequests.py': _handlers(*bodies, className=className)}
    files.update(extra or {})
    return files


def _contract(*bodies: str, handler: str = 'makeWidget_fakeAcct', extra: dict | None = None) -> list:
    """The handler's requirements as the map stores them, plus their live
    provenance (`via`, which the JSON leaves out)."""
    section = pubSubMapSection.buildSection(_consumerFiles(*bodies, extra=extra))
    rows = section.data['handlers'][handler]['requirements']
    assert rows == [r.toJson() for r in section.requirements[handler]]
    return [dict(r.toJson(), via=' '.join(sorted(r.via))) for r in section.requirements[handler]]


def _req(reqs: list, path: str, **expect) -> dict:
    """The requirement at `path` whose fields include `expect` (flags absent
    from a requirement are False)."""
    for r in reqs:
        if r['path'] == path and all(r.get(k, False) == v for k, v in expect.items()):
            return r
    raise AssertionError(f'no requirement {path} {expect} in:\n' + '\n'.join(json.dumps(r) for r in reqs))


def _paths(reqs: list) -> set:
    return {r['path'] for r in reqs}


# ---- handler patterns 1-9 --------------------------------------------------

def test_pattern1_modelParseOfTheWholeMessage():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            return Gizmo(**data).model_dump()
        ''')
    _req(reqs, '(root)', kind='model', nonNull=True)
    _req(reqs, '_id', required=True, nonNull=True, consumerType='str')
    _req(reqs, 'name', required=True, nonNull=True)
    colour = _req(reqs, 'colour', required=False, nonNull=True, hasDefault=True)
    assert colour['consumerType'] == 'str'
    assert 'size' not in _paths(reqs), 'an optional nullable float constrains nothing'
    assert 'label' not in _paths(reqs), 'a computed field is never read from the message'


# A model in a generated objects_<publisher>.py (pubSubModels.py writes it).
GENERATED_GIZMO = pubSubMapContract.GENERATED_MODELS_HEADER + '\n' + _src('''
    from pydantic import BaseModel, Field


    class FakeGen(BaseModel):
        id: str = Field(alias='_id')
        name: str
        count: int = 0
    ''')


@pytest.mark.parametrize('rel, header, paths', [
    ('objects_fakeAcct.py', True, ['(root)']),
    ('objects_fakeAcct.py', False, ['(root)', '_id', 'count', 'name']),
    ('fakeModels.py', True, ['(root)', '_id', 'count', 'name']),
])
def test_aGeneratedModelIsOneRequirementButWhatIsForwardedOffItIsKept(rel, header, paths):
    """A parse with a model of a generated objects_<publisher>.py is the root
    row alone (the publisher's own schema); a hand-written model, or the
    header anywhere else, still expands. A field read off it and forwarded
    into a consumer model keeps its forwardedTo row."""
    source = GENERATED_GIZMO if header else GENERATED_GIZMO.split('\n', 1)[1]
    module = rel[:-3]
    reqs = _contract(f'''
        def makeWidget_fakeAcct(self, data):
            from {module} import FakeGen
            return {{'n': FakeGen(**data).name}}
        ''', extra={rel: source})
    assert sorted(_paths(reqs)) == paths
    forwarded = _contract(f'''
        def makeWidget_fakeAcct(self, data):
            from {module} import FakeGen
            return Strict(name=FakeGen.model_validate(data).name).model_dump()
        ''', extra={rel: source})
    _req(forwarded, 'name', forwardedTo='models.py::Strict.name', nonNull=True)


def test_pattern2_modelParseOfASubKeyAndThroughALocal():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            Gizmo(**data['res'])
            child = data.get('child')
            return Gizmo(**child) if child else None
        ''')
    _req(reqs, 'res', required=True, nonNull=True)
    _req(reqs, 'res.name', required=True, nonNull=True)
    _req(reqs, 'child', kind='model', ifPresent=True)
    _req(reqs, 'child.name', required=True)


def test_pattern3_parameterRebound():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            data = data['inner']
            return {'name': data['name']}
        ''')
    _req(reqs, 'inner.name', required=True)


def test_pattern4_wrapperClassInitAndTheMethodsItCalls():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            return Wrapper(data['envelope']).run()
        ''')
    _req(reqs, 'envelope.ref')
    _req(reqs, 'envelope.body.text', required=True)
    assert any('wrapper:Wrapper' in r['via'] for r in reqs)


def test_pattern5_helperFunction():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            return {'name': readName(data)}
        ''')
    assert 'helper:helpers.py::readName' in _req(reqs, 'name', required=True)['via']


def test_pattern6_selfDelegationMergesTheOtherHandler():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            return self.makeGadget_fakeAcct(data)
        ''', '''
        def makeGadget_fakeAcct(self, data):
            return {'n': data['gadgetName']}
        ''')
    assert 'delegate:makeGadget_fakeAcct' in _req(reqs, 'gadgetName', required=True)['via']


def test_pattern7_dictFieldsReparsedInALiteralLoop():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            message = Envelope(**data)
            out = {}
            for key, slot in [('first', 'a'), ('second', 'b')]:
                raw = getattr(message, key)
                if raw is None:
                    continue
                out[slot] = Gizmo(**raw)
            return out
        ''', extra={'models.py': MODELS + _src('''
            class Envelope(BaseModel):
                first: dict | None = None
                second: dict | None = None
            ''')})
    _req(reqs, 'first', kind='model', ifPresent=True)
    _req(reqs, 'second.name', required=True, nonNull=True)


def test_pattern8_modelMethodsAndForwardedKwargs():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            holder = Holder(**data)
            return Gizmo(_id=data['ref'], name=holder.describe()).model_dump()
        ''', extra={'models.py': MODELS + _src('''
            class Holder(BaseModel):
                extra: dict

                def describe(self):
                    return self.extra['title']
            ''')})
    forwarded = _req(reqs, 'ref', required=True, nonNull=True)
    assert forwarded['forwardedTo'] == 'models.py::Gizmo.id'
    _req(reqs, 'extra.title', required=True)


def test_pattern9_rawKeyReadsProbesAndIteration():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            for line in data['lines']:
                line['sku']
            if 'note' in data:
                data['note']
            return {'c': data.get('count', 0)}
        ''')
    _req(reqs, 'lines', required=True, nonNull=True)
    _req(reqs, 'lines[].sku', required=True)
    _req(reqs, 'note', required=True, ifPresent=True)
    _req(reqs, 'count', hasDefault=True)



FAKE_CONSTS = "FAKE_KEYS = ('alpha', 'beta')\nFAKE_SET = frozenset({'gamma'})\nFAKE_MIXED = ('delta', 1)\n"


@pytest.mark.parametrize('body, paths', [
    ('for k in FAKE_KEYS:\n    data.get(k)\nreturn {}', ['alpha', 'beta']),
    ('return {k: data[k] for k in fakeConsts.FAKE_KEYS}', ['alpha', 'beta']),
    ("return {'n': all(data['box'].get(k) for k in FAKE_SET)}", ['box', 'box.gamma']),
    ('return {"n": [data[a][b] for a in FAKE_KEYS for b in LOCAL_KEYS]}', ['alpha', 'alpha.x', 'beta', 'beta.x']),
    ("return {'n': data['lines'][-1]['sku']}", ['lines', 'lines[]', 'lines[].sku'])])
def test_loopsOverModuleConstantsAndNegativeIndicesAreFollowed(body, paths):
    """A loop or comprehension over a module-level literal of strings (in
    this module, imported, or through the module; frozenset() counts) is
    unrolled, and x[-1] reads a list element: no such read may vanish."""
    psr = ("import fakeConsts\nfrom fakeConsts import FAKE_KEYS, FAKE_SET\n\nLOCAL_KEYS = ['x']\n"
           + _handlers('def makeWidget_fakeAcct(self, data):\n' + textwrap.indent(_src(body), '    ')))
    reqs = _contract(extra={'fakeConsts.py': FAKE_CONSTS, 'PubSubRequests.py': psr})
    assert sorted(_paths(reqs)) == paths
    assert not any(r.get('kind') == 'dynamicKey' for r in reqs)



def test_aConditionalWithAnEmptyFallbackIsFollowedLikeOr():
    """`(x if cond else {}).get('k')` reads k from x, as `(x or {}).get('k')` does."""
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            changes = data['changes']
            return {'who': (changes[-1] if isinstance(changes[-1], dict) else {}).get('who')}
        ''')
    assert _paths(reqs) == {'changes', 'changes[].who'}, 'the element itself may be anything: no non-null need'
    _req(reqs, 'changes[].who', required=False)


@pytest.mark.parametrize('test, coalesced', [
    ("data.get('contact')", True), ("data.get('contact') is not None", True), ("data.get('hasContact')", False)])
def test_onlyATestOfTheValueLetsAnEmptyFallbackReplaceANone(test, coalesced):
    """Under a test of something else, `contact` itself is used whenever the
    test holds, so it must not be None."""
    reqs = _contract("def makeWidget_fakeAcct(self, data):\n"
                     f"    return {{'p': (data.get('contact') if {test} else {{}}).get('phone')}}\n")
    assert any(r['path'] == 'contact' and r.get('nonNull') for r in reqs) is not coalesced
    _req(reqs, 'contact.phone', required=False)


def test_aDynamicKeyReadNeverHidesAKeyReadAtItsPath():
    reqs = _contract("def makeWidget_fakeAcct(self, data):\n    box = data.get('box') or {}\n"
                     "    return {'v': box.get(fakeKey)}\n")
    assert sorted(r.get('kind', 'key') for r in reqs if r['path'] == 'box') == ['dynamicKey', 'key']


def _constantLoop(consts: str, body: str, mutator: str = '') -> list:
    """The contract of a handler running `body`, with fakeConsts.py holding
    `consts` (FAKE_LIST, FAKE_INNER) and fakeMutator.py holding `mutator`."""
    psr = ('import fakeConsts\nfrom fakeConsts import FAKE_LIST, FAKE_INNER\n'
           + _handlers('def makeWidget_fakeAcct(self, data):\n' + textwrap.indent(_src(body), '    ')))
    extra = {'fakeConsts.py': consts, 'PubSubRequests.py': psr}
    if mutator:
        extra['fakeMutator.py'] = mutator
    return _contract(extra=extra)


def _fakeTuple(name: str, prefix: str, size: int) -> str:
    return f'{name} = (' + ''.join(f"'{prefix}{i}', " for i in range(size)) + ')\n'


@pytest.mark.parametrize('size, unrolled', [(pubSubMapContract.MAX_CONSTANT_ITEMS, True),
                                            (pubSubMapContract.MAX_CONSTANT_ITEMS + 1, False)])
def test_aConstantPastTheItemCapIsAReadWithAnUnknownKey(size, unrolled):
    reqs = _constantLoop(_fakeTuple('FAKE_LIST', 'k', size), 'for k in FAKE_LIST:\n    data.get(k)\nreturn {}')
    assert len([r for r in reqs if r['path'].startswith('k')]) == (size if unrolled else 0)
    assert any(r.get('kind') == 'dynamicKey' for r in reqs) is not unrolled


@pytest.mark.parametrize('inner, unrolled', [(16, True), (17, False)])
def test_aComprehensionPastTheElementCapStopsUnrolling(inner, unrolled):
    """16 x 16 = MAX_COMPREHENSION_ITEMS element evaluations unroll; with 17
    inner items the inner generator's key is unknown instead."""
    assert pubSubMapContract.MAX_COMPREHENSION_ITEMS == 16 * 16
    reqs = _constantLoop(_fakeTuple('FAKE_LIST', 'a', 16) + _fakeTuple('FAKE_INNER', 'b', inner),
                         "return {'n': [data[a].get(b) for a in FAKE_LIST for b in FAKE_INNER]}")
    assert len([r for r in reqs if '.b' in r['path']]) == (16 * inner if unrolled else 0)
    assert len([r for r in reqs if r.get('kind') == 'dynamicKey']) == (0 if unrolled else 16)



@pytest.mark.parametrize('use, listUnrolled, tupleUnrolled', [
    # an alias with a mutating method: caught for a tuple too (by name, no proof it is not a list)
    ("fakeAlias = FAKE_LIST\nfakeAlias.append('beta')\n", False, False),
    ('fakeRemember(FAKE_LIST)\n', False, True),
    ('def fakeDefault(keys=FAKE_LIST):\n    return keys\n', False, True),
    ('def fakeGive():\n    return FAKE_LIST\n', False, True),
    ("fakeHolder = {'keys': FAKE_LIST}\n", False, True),
    ("fakeCount = len(FAKE_LIST)\nfakeSorted = sorted(FAKE_LIST)\nfakeJoined = ', '.join(FAKE_LIST)\n"
     "fakeHas = 'alpha' in FAKE_LIST\nfakeFirst = FAKE_LIST[0]\nfakeLoop = [k for k in FAKE_LIST]\n", True, True)])
@pytest.mark.parametrize('literal', ["['alpha']", "('alpha',)"])
def test_aMutableConstantThatEscapesIsAReadWithAnUnknownKey(use, listUnrolled, tupleUnrolled, literal):
    """A list (or set or dict) bound to another name, used as an argument
    default, returned or passed to a call that is not a reading one may be
    changed under that name; a tuple cannot be."""
    reqs = _constantLoop(f'FAKE_LIST = {literal}\n', 'for k in FAKE_LIST:\n    data.get(k)\nreturn {}',
                         'from fakeConsts import FAKE_LIST\n\n' + use)
    unrolled = tupleUnrolled if literal.startswith('(') else listUnrolled
    assert ('alpha' in _paths(reqs)) is unrolled
    assert any(r.get('kind') == 'dynamicKey' for r in reqs) is not unrolled

@pytest.mark.parametrize('consts, mutator', [
    ("FAKE_LIST = ['alpha']\n", "from fakeConsts import FAKE_LIST\n\n\ndef fakeGrow():\n    FAKE_LIST.append('beta')\n"),
    ("FAKE_LIST = ['alpha']\n",
     "from fakeConsts import FAKE_LIST as fakeAlias\n\n\ndef fakeGrow():\n    fakeAlias.extend(['beta'])\n"),
    ("FAKE_LIST = ['alpha']\n", "import fakeConsts\n\nfakeConsts.FAKE_LIST = ['beta']\n"),
    ("FAKE_LIST = ['alpha']\n", "import fakeConsts\n\n\ndef fakeGrow():\n    fakeConsts.FAKE_LIST += ['beta']\n"),
    ("FAKE_LIST = ['alpha']\n", "from fakeConsts import FAKE_LIST\n\n\ndef fakeGrow():\n    FAKE_LIST[0] = 'beta'\n"),
    ("FAKE_LIST = ('alpha',)\nFAKE_LIST += ('beta',)\n", ''),
    ("FAKE_LIST = ['alpha']\n\n\ndef fakeReset():\n    global FAKE_LIST\n    FAKE_LIST = ['beta']\n", '')])
def test_aConstantThatCodeMayChangeIsAReadWithAnUnknownKey(consts, mutator):
    reqs = _constantLoop(consts, 'for k in FAKE_LIST:\n    data.get(k)\nreturn {}', mutator)
    assert 'alpha' not in _paths(reqs) and any(r.get('kind') == 'dynamicKey' for r in reqs)
    assert 'alpha' in _paths(_constantLoop("FAKE_LIST = ['alpha']\n", 'for k in FAKE_LIST:\n    data.get(k)\nreturn {}'))

@pytest.mark.parametrize('read', ['data[pickFakeKey()]', 'data.get(pickFakeKey())', 'data.pop(fakeKey)'])
def test_aReadWithAKeyTheMapCannotResolveIsKeptAndIsNotAContract(read):
    """It stays visible as a dynamicKey requirement at the parent path, but
    a handler that reads nothing else is still an unresolved contract."""
    psr = "from fakeConsts import FAKE_MIXED\n" + _handlers(
        f"def makeWidget_fakeAcct(self, data):\n    fakeKey = FAKE_MIXED[1]\n    return {{'v': {read}}}\n")
    handler = pubSubMapSection.buildSection(_consumerFiles(extra={
        'fakeConsts.py': FAKE_CONSTS, 'PubSubRequests.py': psr})).data['handlers']['makeWidget_fakeAcct']
    assert [(r['path'], r.get('kind')) for r in handler['requirements']] == [('(root)', 'dynamicKey')]
    assert handler['resolved'] is False

def test_modelsResolveThroughPackageReExportsAndStarImports():
    files = _consumerFiles('''
        def makeWidget_fakeAcct(self, data):
            return Thing(**data).model_dump()
        ''', extra={'objects/__init__.py': 'from objects.thing import Thing\n',
                    'objects/thing.py': 'from models import *\n\n\nclass Thing(Gizmo):\n    extraField: int\n'})
    files['PubSubRequests.py'] = 'from objects import Thing\n' + files['PubSubRequests.py']
    section = pubSubMapSection.buildSection(files)
    reqs = [r.toJson() for r in section.requirements['makeWidget_fakeAcct']]
    _req(reqs, 'extraField', required=True, consumerType='int')
    _req(reqs, 'name', required=True, nonNull=True)
    assert 'heuristicNames' not in section.data and 'ambiguousNames' not in section.data


def test_unimportedNamesResolveOnlyWhenUnique():
    """A name with no import resolves to the one class of that name in the
    repo (a heuristic, recorded); two candidates leave it unresolved (also
    recorded). This tool's own modules are never candidates."""
    model = 'from pydantic import BaseModel\n\n\nclass {0}(BaseModel):\n    {1}: str\n'
    section = pubSubMapSection.buildSection(_consumerFiles('''
        def makeWidget_fakeAcct(self, data):
            Unique(**data['a'])
            Twin(**data['b'])
            return {}
        ''', extra={'one.py': model.format('Unique', 'u') + '\n\n' + model.format('Twin', 't').split('\n\n\n')[1],
                    'two.py': model.format('Twin', 't'), 'pubSubMap.py': 'class Unique:\n    pass\n',
                    'pubSubMapSection.py': 'class Twin:\n    pass\n'}))
    reqs = [r.toJson() for r in section.requirements['makeWidget_fakeAcct']]
    _req(reqs, 'a.u', required=True)
    assert 'b.t' not in _paths(reqs)
    assert section.data['heuristicNames'] == ['Unique -> one.py::Unique']
    assert section.data['ambiguousNames'] == ['Twin: one.py::Twin, two.py::Twin']


@pytest.mark.parametrize('hops, read', [(pubSubMapScan.MAX_CALL_DEPTH, True),
                                        (pubSubMapScan.MAX_CALL_DEPTH + 1, False)])
def test_callChainsDeeperThanTheLimitAreTruncated(hops, read):
    chain = ''.join(f'def hop{i}(p):\n    return hop{i + 1}(p)\n\n\n' for i in range(1, hops))
    files = _consumerFiles('''
        def makeWidget_fakeAcct(self, data):
            return {'v': hop1(data)}
        ''', extra={'chain.py': chain + f"def hop{hops}(p):\n    return p['deep']\n"})
    files['PubSubRequests.py'] = 'from chain import hop1\n' + files['PubSubRequests.py']
    handler = pubSubMapSection.buildSection(files).data['handlers']['makeWidget_fakeAcct']
    assert ('deep' in _paths(handler['requirements'])) is read
    assert pubSubMapSection.flag(handler, 'truncated') is not read


# ---- guards ------------------------------------------------------------------

def test_guardA_readsAfterAValueCheckStayUnconditional():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            if data['type'] == 'services':
                return {}
            return {'n': data['name']}
        ''')
    name = _req(reqs, 'name', required=True)
    assert not name.get('ifPresent') and not name.get('deadBranch')


def test_guardB_presenceGuardMakesTheReadIfPresent():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            child = data.get('child')
            if child is None:
                return {}
            return Gizmo(**child).model_dump()
        ''')
    _req(reqs, 'child', kind='model', ifPresent=True)
    assert not any(r['path'] == 'child' and r.get('nonNull') and not r.get('ifPresent')
                   for r in reqs), 'a guarded value is never required non-null'


def test_guardC_discriminatorMarksTheOtherBranchDead():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            if 'res' in data:
                return Gizmo(**data['res']).model_dump()
            return Gizmo(**data).model_dump()
        ''')
    assert _req(reqs, 'name', required=True)['deadBranch'] == 'res'
    assert not _req(reqs, 'res.name', required=True).get('deadBranch')


def test_guardD_readsInsideExceptExceptionAreLenient():
    reqs = _contract('''
        def makeWidget_fakeAcct(self, data):
            try:
                data['risky']
            except Exception:
                pass
            return {}
        ''')
    _req(reqs, 'risky', required=True, lenient=True)


@pytest.mark.parametrize('guard, result', [
    ('if data.get("skip"):', 'issue'), ('if data["skip"]:', 'issue'),
    ('if Sent(**data).skip:', 'issue'), ('if "skip" in data:', 'deadBranch')])
def test_onlyAnInTestMakesTheRestADeadBranch(guard, result):
    """A truthiness early return is a value guard: what follows is read on
    every message that is not skipped. Only `'k' in data` splits on presence."""
    compat = _compat('Sent().model_dump()', guard + '\n    return {}\nWanted(**data)\n', models=SENT_WANTED)
    assert [(r['result'], r.get('issue')) for r in compat['byPath']['name']] == [
        (result, 'nullable' if result == 'issue' else None)]


@pytest.mark.parametrize('handler, lenient', [
    ('except Exception:\n    pass\n', True), ('except:\n    pass\n', True),
    ('except Exception as e:\n    print(e)\n    raise\n', False),
    ('except Exception as e:\n    raise ValueError("bad") from e\n', False)])
def test_onlyACatchAllThatSwallowsMakesReadsLenientAndTheirIssuesStillFail(handler, lenient):
    """A lenient read's issue is an issue all the same: the handler would
    drop the message silently."""
    compat = _compat('Sent().model_dump()', 'try:\n    Wanted(**data)\n' + handler, models=SENT_WANTED)
    assert [(r['result'], r.get('issue'), bool(r.get('lenient'))) for r in compat['byPath']['name']] == [
        ('issue', 'nullable', lenient)]
    assert compat['verdict'] == 'issues'


def _wantedWithValidators(*validators: str) -> str:
    """Sent (name may be None) and Wanted (name: str) with `validators`
    (decorator line, def line, body) on Wanted."""
    out = 'class Sent(BaseModel):\n    name: str | None = None\n\n\nclass Wanted(BaseModel):\n    name: str\n'
    for decorator, signature, body in validators:
        out += (f'\n    {decorator}\n    @classmethod\n    {signature}\n'
                + textwrap.indent(_src(body), ' ' * 8))
    return out


@pytest.mark.parametrize('body, accepted', [
    ("return '' if v is None else v", True),
    ("return v if v is not None else ''", True),
    ("if v is None:\n    return ''\nreturn v", True),
    ("if v is None:\n    v = ''\nreturn v", True),
    ("return v or ''", True),
    ("v = v or ''\nreturn v", True),
    ("if v in (None, ''):\n    return '-'\nreturn v", True),
    ("if not v:\n    return 'none'\nreturn str(v)", True),
    ("return v.strip() if v is not None else ''", True),
    ('"""Fake."""\nif isinstance(v, str):\n    v = v.strip()\nif v is None:\n    return ""\nreturn v', True),
    ('return v', False),
    ('if isinstance(v, str):\n    return v.strip()\nreturn v', False),
    ("if v is None:\n    raise ValueError('fake')\nreturn v", False),
    ('return int(v)', False),
    ('return cls._fakeFix(v)', False),
    ("v = v.strip()\nif v is None:\n    return ''\nreturn v", False),
    ('if v is None:\n    v = cls._fakeFix()\nreturn v', False),
    ('if v is None:\n    pass\nreturn v', False),
    ('if v == 0:\n    return 1\nreturn v', False),
    ('return 0 if v is None else v', False)])
def test_aBeforeValidatorAcceptsNullOnlyWhenItProvablyMapsNoneToAValue(body, accepted):
    models = _wantedWithValidators(("@field_validator('name', mode='before')", 'def _fakeFix(cls, v):', body))
    compat = _compat('Sent().model_dump()', 'Wanted(**data)\n', models=models)
    assert _issues(compat, 'name') == (set() if accepted else {'nullable'})



STR, INT, LIST, DICT = ('str',), ('int',), ('list', pubSubMapScan.ANY), ('dict', pubSubMapScan.ANY)


@pytest.mark.parametrize('body, ftype, accepted', [
    # a rebinding inside try/for/with/while is not followed, whatever v holds
    ("if v is None:\n    v = ''\ntry:\n    v = v.strip() or None\nexcept AttributeError:\n    pass\nreturn v",
     STR, False),
    ("if v is None:\n    v = ''\nfor fakeItem in FAKE_SOURCES:\n    v = None\nreturn v", STR, False),
    ("v = v or ''\nwith fakeLock:\n    v = fakeCache.get(v)\nreturn v", STR, False),
    ("if v is None:\n    v = ''\nwhile False:\n    v = None\nreturn v", STR, False),
    ("v = v or ''\nwith fakeLock:\n    fakeCache.get(v)\nreturn v", STR, True),
    # `{**v}` raises on None; a keyed entry does not
    ('return {**FAKE_DEFAULTS, **v}', DICT, False),
    ("return {**FAKE_DEFAULTS, 'value': v}", DICT, True),
    # a value it cannot type is unknown, and no test on it is decided
    ('v = fakeLookup()\nif v is not None:\n    return None\nreturn 0', INT, False),
    # an unknown isinstance target decides nothing
    ('if isinstance(v, FakeHashable):\n    return v\nreturn []', LIST, False),
    ('if isinstance(v, (str, bytes)):\n    return v\nreturn []', LIST, True),
    ('if isinstance(v, type(None)):\n    return []\nreturn v', LIST, True),
    # the value must fit the field type
    ('if v is None:\n    return []\nreturn v', STR, False),
    ('if v is None:\n    return []\nreturn v', LIST, True),
    ('return v or 0', ('union', (STR, INT)), True),
    ('return v or 0', ('float',), True),
    ("return v or 'a'", ('lit', ('a', 'b')), False),
    # only an empty literal proves a typed container
    ('return v or []', ('list', INT), True),
    ('return v or list()', ('list', INT), True),
    ("return v or ['a']", ('list', INT), False),
    ("return v or ['a']", LIST, True),
    ('return v or {}', ('dict', INT), True),
    ("return v or {'k': 'a'}", ('dict', INT), False)])
def test_mapsNoneToValueNeedsAProofOfAValueOfTheFieldType(body, ftype, accepted):
    fn = ast.parse('def fakeFix(cls, v):\n' + textwrap.indent(_src(body), '    ')).body[0]
    assert pubSubMapContract.mapsNoneToValue([fn], ftype, False, None) is accepted
    assert not pubSubMapContract.mapsNoneToValue([fn], ftype, True, None), 'never for a constrained field'


CONSTRAINED_MODELS = _src('''
    from pydantic import constr, model_validator


    class FakeOpen(BaseModel):
        note: str = ''


    class FakeNeedy(BaseModel):
        note: str


    class FakeChecked(BaseModel):
        note: str = ''

        @model_validator(mode='after')
        def _fakeCheck(self):
            return self


    class FakeFields(BaseModel):
        plain: str
        short: str = Field(..., min_length=1)
        typed: constr(pattern='^a')
        annotated: Annotated[int, Field(gt=0)]
        checked: str
        anyList: list
        intList: list[int]

        @field_validator('checked')
        @classmethod
        def _fakeAfter(cls, v):
            return v
    ''')


def test_aFieldIsConstrainedByFieldArgumentsTypesAndAfterOrModelValidators():
    _, info = _models('from typing import Annotated\n' + CONSTRAINED_MODELS)
    assert {n: f.constrained for n, f in info('FakeFields').fields.items()} == {
        'plain': False, 'short': True, 'typed': True, 'annotated': True, 'checked': True, 'anyList': False,
        'intList': False}
    assert info('FakeChecked').fields['note'].constrained and not info('FakeOpen').fields['note'].constrained


@pytest.mark.parametrize('body, model, accepted', [
    ('return v or {}', 'FakeOpen', True), ('return v or dict()', 'FakeOpen', True),
    ('return v or {}', 'FakeNeedy', False), ('return v or {}', 'FakeChecked', False),
    ("return v or {'note': ''}", 'FakeOpen', False)])
def test_onlyAnEmptyDictProvesAModelThatNeedsNothing(body, model, accepted):
    """A required field or a model validator may reject `{}`; the entries of
    a non-empty dict are not checked."""
    repo, info = _models('from typing import Annotated\n' + CONSTRAINED_MODELS)
    fn = ast.parse('def fakeFix(cls, v):\n' + textwrap.indent(_src(body), '    ')).body[0]
    assert pubSubMapContract.mapsNoneToValue([fn], repo.classType(info(model).ci), False, repo) is accepted


def test_aFallbackNeverProvesAConstrainedField():
    models = ("class Sent(BaseModel):\n    name: str | None = None\n\n\nclass Wanted(BaseModel):\n"
              "    name: str = Field(..., min_length=1)\n\n    @field_validator('name', mode='before')\n"
              "    @classmethod\n    def _fakeFix(cls, v):\n        return v or ''\n")
    compat = _compat('Sent().model_dump()', 'Wanted(**data)\n', models=models)
    assert _issues(compat, 'name') == {'nullable'}

@pytest.mark.parametrize('validators, accepted', [
    ([('wrap', 'def _fakeWrap(cls, v, handler):', "if v is None:\n    return 'x'\nreturn handler(v)")], True),
    ([('wrap', 'def _fakeWrap(cls, v, handler):', "if v is None:\n    return 'x'\nreturn handler(v)"),
      ('*', 'def _fakeAll(cls, v):', 'return v')], False),
    ([('pre', 'def _fakePre(cls, v):', "return v or ''")], True)])
def test_everyBeforeValidatorOfTheFieldMustMapNoneToAValue(validators, accepted):
    """Their order is not modelled, so all of them must; a wrap validator
    and a v1 pre validator count as before validators."""
    decorators = {'wrap': "@field_validator('name', mode='wrap')", '*': "@field_validator('*', mode='before')",
                  'pre': "@validator('name', pre=True)"}
    models = _wantedWithValidators(*[(decorators[kind], sig, body) for kind, sig, body in validators])
    compat = _compat('Sent().model_dump()', 'Wanted(**data)\n', models=models)
    assert _issues(compat, 'name') == (set() if accepted else {'nullable'})
    reqs = _contract('def makeWidget_fakeAcct(self, data):\n    Wanted(**data)\n    return {}\n',
                     extra={'models.py': MODELS + models})
    _req(reqs, 'name', nonNull=True, noneToValue=accepted)


# ---- models ------------------------------------------------------------------

def _models(extra: str = ''):
    repo = pubSubMapScan.Repo({'models.py': MODELS + extra})
    module = repo.module('models.py')
    return repo, lambda name: repo.modelInfo(repo.resolveClass(module, ast.Name(id=name)))


def test_modelFieldsFollowTheMro():
    _, info = _models()
    gizmo = info('Gizmo')
    assert gizmo.fields['id'].required, 'Keys is first in the MRO, so its required _id wins'
    assert gizmo.fields['id'].inKeys == ('_id', 'id'), 'alias first, then the name (populate_by_name)'


def test_fieldNoneOnANonOptionalTypeIsOptionalButNotNullable():
    _, info = _models()
    colour = info('Gizmo').fields['colour']
    assert (colour.required, colour.nullable) == (False, False)
    assert info('Gizmo').fields['size'].nullable


def test_rootModelsAndExcludedAndComputedFields():
    repo, info = _models()
    assert pubSubMapScan.printType(info('Lines').rootType) == 'list[Line]'
    assert pubSubMapScan.printType(info('Kind').rootType) == "Literal['a', 'b']"
    gizmo = info('Gizmo')
    assert gizmo.fields['secret'].exclude and gizmo.fields['label'].computed
    assert gizmo.fields['count'].validatorBefore
    dump = pubSubMapScan.PayloadResolver(repo).modelObj(gizmo.ci, ('dump', True, False))
    assert 'secret' not in dump.fields and 'label' in dump.fields and '_id' in dump.fields


def _typeForms():
    """Every type form printType prints: each scalar, Literal and Enum (with
    values of each kind, and with none known), a model, a RootModel label,
    and lists, dicts and unions of all of them, two levels deep."""
    S = pubSubMapScan
    base = [(k,) for k in ('str', 'int', 'float', 'bool', 'datetime', 'date')] + [S.ANY, S.NONE]
    base += [('lit', ('a', "it's", 'q"uote')), ('lit', (-1, 2.5, True, b'x')), ('lit', ()),
             ('enum', 'Color', ('red', 1, None)), ('enum', 'Color', ()),
             ('model', ('models.py', 'Gizmo'), 'Gizmo'), ('model', ('models.py', 'Lines'), 'list[Line]')]
    forms = list(base)
    for _ in range(2):
        forms += [('list', t) for t in base] + [('dict', t) for t in base]
        forms += [S.union([a, b]) for a in base for b in base if a != b]
        base = forms[len(base):]
    return forms


def test_everyPrintedTypeParsesBack():
    """printType -> parseType -> printType is the identity, and the parsed
    type gives the type checks what the original gives (a model's repo key
    is not printed, so it reads back as None)."""
    def unkeyed(t):
        if not isinstance(t, tuple) or not t:
            return t
        if t[0] == 'model':
            return pubSubMapContract.parseType(t[2]) if '[' in t[2] else ('model', None, t[2])
        return tuple(unkeyed(x) if isinstance(x, tuple) else x for x in t)
    forms = _typeForms()
    assert len(forms) > 2000
    for t in forms:
        text = pubSubMapScan.printType(t)
        parsed = pubSubMapContract.parseType(text)
        assert parsed is not None and pubSubMapScan.printType(parsed) == text, text
        assert parsed == unkeyed(t), text
        assert pubSubMapScan.literalValues(parsed) == pubSubMapScan.literalValues(unkeyed(t)), text


@pytest.mark.parametrize('text', ['Literal[{[1]}]', 'Literal[{1: 2}]', 'Color[[1]]',
                                  '(' * 5000, 'a.b', '', 'Literal[x]', '1 +', 'dict[str]', 'f(x)'])
def test_parseTypeRejectsAnythingElseWithoutRaising(text):
    """A hand-edited snapshot must not crash the tool: None, so the row is
    typeUnknown (unverified)."""
    assert pubSubMapContract.parseType(text) is None


def test_requirementsRoundTripThroughTheMap():
    """Requirement.fromJson(r.toJson()) rebuilds r (all but `via`), for
    every requirement of the pattern fixtures and one with every field set."""
    Requirement = pubSubMapContract.Requirement
    full = Requirement((('innerData', 'inner'), '[]', 'name'), kind='model', model='models.py::Gizmo',
                       forwardedTo='models.py::Gizmo.id', ctype=pubSubMapScan.union(
                           [('enum', 'Color', ()), ('lit', ('a',)), pubSubMapScan.NONE]),
                       forbidKeys=('_id', 'id', 'name'))
    for name in Requirement.FLAGS:
        setattr(full, name, True)
    full.deadBranch = 'res'
    section = pubSubMapSection.buildSection(_consumerFiles(
        'def makeWidget_fakeAcct(self, data):\n    Strict(**data)\n    Lines.model_validate(data["l"])\n'
        '    return Wrapper(data["e"]).run() if "x" in data else Gizmo(**data).model_dump()\n'))
    reqs = section.requirements['makeWidget_fakeAcct'] + [full]
    assert len(reqs) > 10
    for req in reqs:
        back = Requirement.fromJson(req.toJson())
        assert not back.typeUnknown and back.toJson() == req.toJson()
        same = [k for k in Requirement.__slots__ if k not in ('via', 'ctype')]  # ctype: in toJson
        assert [getattr(back, k) for k in same] == [getattr(req, k) for k in same], req.toJson()
    assert Requirement.fromJson({'path': 'x', 'extraForbid': True}).typeUnknown, 'an older map'


# ---- payloads ------------------------------------------------------------------

def _publisherFiles(*bodies: str, consumers=('shopService',), decorate: bool = True,
                    extra: dict | None = None) -> dict:
    """fake-acct's server/: actions/WidgetActions.py holds `bodies`, each
    under @pubSubDecorator(projectNameConsumers=[<consumers>]) unless
    `decorate` is False."""
    head = _src('''
        from pubSub import pubSubDecorator
        from route_config import route_config
        from models import Gizmo
        from mongoDb import db
        ''') + ''.join(f'from services.{c} import {c}\n' for c in consumers)
    decorator = '@pubSubDecorator(projectNameConsumers=[' + ', '.join(consumers) + '])\n' if decorate else ''
    files = {'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'models.py': MODELS,
             'actions/WidgetActions.py': head + '\n\nclass WidgetActions():\n' + _methods(bodies, decorator)}
    files.update({f'services/{c}.py': _service(c) for c in consumers})
    files.update(extra or {})
    return files


def _payload(*bodies: str, fn: str = 'makeWidget', extra: dict | None = None):
    section = pubSubMapSection.buildSection(_publisherFiles(*bodies, extra=extra))
    return section.data['publishers'][fn], section.shapes[fn]


def test_payloadModelDump():
    entry, shape = _payload('''
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            return Gizmo(_id='a', name='b').model_dump(by_alias=True)
        ''')
    assert entry['payload'] == {'source': 'modelDump', 'model': 'models.py::Gizmo'}
    assert entry['consumers'] == ['fakeShop'] and entry['topic'] == 'makeWidget_fakeAcct'
    assert shape.closed and '_id' in shape.fields and 'label' in shape.fields


def test_payloadDictLiteralIgnoresNestedDefs():
    entry, shape = _payload('''
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            def inner():
                return 'not the payload'
            return {'name': 'x', 'count': 1}
        ''')
    assert entry['payload']['source'] == 'dictLiteral'
    assert shape.closed and sorted(shape.fields) == ['count', 'name']
    assert pubSubMapScan.printShape(shape.fields['count'][0]) == 'int'


def test_payloadDictLiteralTakesUnknownValueTypesFromTheResponseModel():
    entry, shape = _payload('''
        @route_config(httpMethod='POST', jwtRequired=True, responseModel=Gizmo)
        def makeWidget(self, doc: dict, publishToProdPubSub, publishToPubSubMockDb=False):
            return {'_id': doc['ref'], 'name': 'x'}
        ''')
    assert entry['payload'] == {'source': 'dictLiteral', 'model': 'models.py::Gizmo'}
    assert shape.closed and pubSubMapScan.printShape(shape.fields['_id'][0]) == 'str'


def test_payloadPersistedDump():
    entry, shape = _payload('''
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            gizmo = Gizmo(_id='a', name='b')
            return db.create(gizmo.model_dump(by_alias=True), 'Gizmos')
        ''')
    assert entry['payload'] == {'source': 'persistedDump', 'model': 'models.py::Gizmo'}
    assert not shape.closed and shape.fields['_version'][0] == ('int',), 'stored docs always carry _version'


def test_payloadUnknownMakesAnEnforcedFlowUnverified():
    pub = pubSubMapSection.buildSection(_publisherFiles('''
        def makeWidget(self, doc: dict, publishToProdPubSub, publishToPubSubMockDb=False):
            return doc
        '''))
    assert pub.data['publishers']['makeWidget']['payload'] == {'source': 'unknown', 'model': None}
    con = pubSubMapSection.buildSection(_consumerFiles(extra={'PubSubRequests.py': _handlers('''
        def makeWidget_fakeAcct(self, data):
            return {'n': data['name']}
        ''')}))
    compat = _evaluate(pub, con)
    assert compat['verdict'] == 'unverified' and compat['rows'][0]['result'] == 'unverified'
    assert compat['counts'] == {'unverified': 1}


def test_aKeyMissingFromAnOpenPayloadIsUnverifiedNotUndeclared():
    compat = _compat('doc', 'return {"x": data["notInModel"], "n": data["name"]}\n',
                     publisherHead="@route_config(httpMethod='POST', jwtRequired=True, responseModel=Gizmo)\n",
                     params='doc: dict, ')
    assert compat['payload'] == {'source': 'responseModel', 'model': 'models.py::Gizmo'}
    assert {r['path']: r['result'] for r in compat['rows']} == {'notInModel': 'unverified'}


@pytest.mark.parametrize('change, read, result', [
    ("d['extra'] = 'x'", 'extra', 'ok'), ("d.pop('name')", 'name', 'issue'),
    ("del d['name']", 'name', 'issue'), ("if doc:\n    d['extra'] = 'x'", 'extra', 'maybe'),
    ('d.update(doc)', 'extra', 'unverified')])
def test_changesToADumpBeforeItsReturnAreApplied(change, read, result):
    compat = _compat('d', f"return {{'e': data['{read}']}}\n", params='doc: dict, ',
                     before="d = Gizmo(_id='a', name='b').model_dump(by_alias=True)\n" + change + '\n')
    assert [r['result'] for r in compat['byPath'].get(read, [])] == ([] if result == 'ok' else [result])
    assert compat['counts'].get(result)


def test_payloadHatchDeclaresAModelAndNamesOnlyRealPublishers():
    files = _publisherFiles('''
        def makeWidget(self, doc: dict, publishToProdPubSub, publishToPubSubMockDb=False):
            return doc
        ''', extra={'stray.py': "from models import Gizmo\n\nPUBSUB_PAYLOAD_MODELS = {'makeThing': 'Gizmo'}\n"})
    files['actions/WidgetActions.py'] += "\n\nPUBSUB_PAYLOAD_MODELS = {'makeWidget': 'Gizmo', 'makeGone': 'Gizmo'}\n"
    section = pubSubMapSection.buildSection(files)
    assert section.data['publishers']['makeWidget']['payload'] == {'source': 'declared', 'model': 'models.py::Gizmo'}
    assert section.data['hatchErrors'] == [
        "server/actions/WidgetActions.py: PUBSUB_PAYLOAD_MODELS['makeGone'] names no @pubSubDecorator "
        'function of this module',
        'server/stray.py: PUBSUB_PAYLOAD_MODELS sits in a module with no @pubSubDecorator function',
        "server/stray.py: PUBSUB_PAYLOAD_MODELS['makeThing'] names no @pubSubDecorator function of this module"]


@pytest.mark.parametrize('hatch', [True, False])
def test_aModulePublishersDictLiteralTakesUnknownValueTypesFromItsDeclaredModel(hatch):
    """PUBSUB_PAYLOAD_MODELS types the unknown values of a module publisher's
    returned dict literal, as a responseModel does a route's: a consumer that
    narrows one is flagged. Without the hatch the value stays unknown, which
    compat lets pass."""
    files = _publisherFiles('''
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            return {'a': 1}
        ''')
    files['actions/WidgetActions.py'] += (
        '\n\n@pubSubDecorator(projectNameConsumers=[shopService])\n'
        'def makeThing(doc: dict, publishToProdPubSub, publishToPubSubMockDb=False):\n'
        "    return {'_id': doc['ref'], 'name': 'x', 'kind': doc['kind']}\n"
        + ("\n\nPUBSUB_PAYLOAD_MODELS = {'makeThing': 'Gizmo'}\n" if hatch else ''))
    pub = pubSubMapSection.buildSection(files)
    narrow = "\n\nclass Narrow(BaseModel):\n    kind: Literal['plain']\n"
    con = pubSubMapSection.buildSection(_consumerFiles(extra={
        'models.py': MODELS + narrow,
        'PubSubRequests.py': _handlers('def makeThing_fakeAcct(self, data):\n    Narrow(**data)\n')}))
    entry = pub.data['publishers']['makeThing']
    assert entry['kind'] == 'module'
    assert entry['payload'] == {'source': 'dictLiteral', 'model': 'models.py::Gizmo' if hatch else None}
    assert pubSubMapScan.printShape(pub.shapes['makeThing'].fields['_id'][0]) == ('str' if hatch else 'unknown')
    compat = _evaluate(pub, con, 'makeThing', 'makeThing_fakeAcct')
    assert compat['verdict'] == ('issues' if hatch else 'ok')
    assert [(r['path'], r['result'], r.get('issue'), r.get('publisherField')) for r in compat.get('rows', [])] == (
        [('kind', 'issue', 'literal', 'models.py::Gizmo.kind')] if hatch else [])


def test_publishersPubSubAdminCannotReadAreFlagged():
    def widget(decorator, name, asyncDef=False):
        return (f'{decorator}\n{"async " if asyncDef else ""}def {name}(self, publishToProdPubSub, '
                "publishToPubSubMockDb=False):\n    return {'a': 1}\n")
    files = _publisherFiles(
        widget('@pubSubDecorator(projectNameConsumers=[shopService])', 'asyncWidget', asyncDef=True),
        widget('@pubSub.pubSubDecorator(projectNameConsumers=[shopService])', 'dottedWidget'),
        widget('@pubSubDecorator([shopService])', 'positionalWidget'),
        widget('@pubSubDecorator(projectNameConsumers=[shop])', 'aliasedWidget'),
        widget('@pubSubDecorator(projectNameConsumers=[shopService])', 'fineWidget'), decorate=False)
    files['actions/WidgetActions.py'] = ('import pubSub\nfrom services.shopService import shopService as shop\n'
                                         + files['actions/WidgetActions.py'])
    data = pubSubMapSection.buildSection(files).data
    assert {fn: pubSubMapSection.flag(p, 'pubSubAdminProblems') for fn, p in data['publishers'].items()} == {
        'aliasedWidget': ['consumer shop is not a services class name (getServiceByName)'],
        'asyncWidget': ['it is an async def'],
        'dottedWidget': ['the decorator is not written @pubSubDecorator(...)'],
        'fineWidget': [],
        'positionalWidget': ['consumers are not passed as projectNameConsumers=[...]']}
    found = pubSubMap.problems({'repos': {'fakeAcct': data}, 'flows': []})
    assert [p['kind'] for p in found] == ['pubSubAdminInvisible'] * 4


# ---- compat --------------------------------------------------------------------

def _evaluate(pub, con, fn='makeWidget', handler='makeWidget_fakeAcct'):
    pubSrc, conSrc = pubSubMap.RepoSource('fakeAcct', 'fake-acct'), pubSubMap.RepoSource('fakeShop', 'fake-shop')
    pubSrc.section, conSrc.section = pub, con
    return pubSubMap._evaluateFlow(pubSrc, fn, conSrc, handler, None)[0]


def _compat(publisherReturn: str, handlerBody: str, models: str = '', publisherHead: str = '',
            params: str = '', before: str = '', psrTail: str = '') -> dict:
    """The compat of makeWidget (returning `publisherReturn` after `before`)
    against makeWidget_fakeAcct (running `handlerBody`; `psrTail` ends its
    PubSubRequests.py), plus `byPath` (the non-ok rows by path) and the
    publisher's `payload`."""
    pub = pubSubMapSection.buildSection(_publisherFiles(
        publisherHead + f'def makeWidget(self, {params}publishToProdPubSub, publishToPubSubMockDb=False):\n'
        + textwrap.indent(before + f'return {publisherReturn}\n', '    '), extra={'models.py': MODELS + models}))
    psr = _handlers('def makeWidget_fakeAcct(self, data):\n' + textwrap.indent(_src(handlerBody), '    '))
    con = pubSubMapSection.buildSection(_consumerFiles(extra={
        'models.py': MODELS + models, 'PubSubRequests.py': psr + psrTail}))
    compat = _evaluate(pub, con)
    compat['byPath'], compat['payload'] = {}, pub.data['publishers']['makeWidget']['payload']
    for r in compat.get('rows', []):
        compat['byPath'].setdefault(r['path'], []).append(r)
    return compat


def _issues(compat: dict, path: str) -> set:
    return {r.get('issue') for r in compat['byPath'].get(path, []) if r['result'] == 'issue'}


def test_compatNullableUndeclaredLiteralAndNumeric():
    models = _src('''
        class Sent(BaseModel):
            name: str | None = None
            kind: Literal['a', 'b', 'c'] = 'a'
            weight: float = 0


        class Wanted(BaseModel):
            name: str
            kind: Literal['a', 'b']
            weight: int
        ''')
    compat = _compat("Sent().model_dump()", 'Wanted(**data)\nreturn {"x": data["missing"]}\n',
                     models=models)
    assert compat['verdict'] == 'issues'
    assert _issues(compat, 'name') == {'nullable'}
    assert _issues(compat, 'kind') == {'literal'}
    assert _issues(compat, 'weight') == {'numericNarrowing'}
    assert _issues(compat, 'missing') == {'undeclared'}


def test_compatForbiddenExtraAndBeforeValidatorAndIfPresent():
    compat = _compat("{'name': 'x', 'other': 1, 'count': None}",
                     'Strict(**data)\nGizmo(_id="i", count=data["count"])\nif data.get("opt"):\n'
                     '    data["opt"]["k"]\n')
    assert _issues(compat, '(root)') == {'forbiddenExtra'}
    assert _issues(compat, 'count') == set(), 'a before-validator handles None'
    assert all(r['result'] in ('deadRead', 'ok') for r in compat['byPath']['opt'])


def test_compatAliasesAreAcceptedAtEveryPathSegment():
    """populate_by_name: pydantic takes the alias or the name at any depth,
    so a publisher sending `inner` satisfies a consumer field aliased
    `innerData`, and the nested check still runs."""
    compat = _compat('SentOuter(inner=SentInner()).model_dump()', 'WantedOuter(**data)\n', models=_src('''
        class Inner(BaseModel):
            name: str


        class SentOuter(BaseModel):
            inner: 'SentInner'


        class SentInner(BaseModel):
            name: str | None = None


        class WantedOuter(BaseModel):
            model_config = ConfigDict(populate_by_name=True)
            inner: Inner = Field(..., alias='innerData')
        '''))
    assert {p: [(r['result'], r.get('issue')) for r in rows] for p, rows in compat['byPath'].items()} == {
        'innerData.name': [('issue', 'nullable')]}
    assert compat['byPath']['innerData.name'][0].get('altKeys') is None, 'flow rows store no altKeys'




def test_compatIssuesCannotBeSuppressed():
    """A PUBSUB_CONTRACT_GAPS entry naming the issue (the retired hatch)
    changes nothing: the issue stays an issue, the verdict stays issues."""
    models = _src('''
        class Sent(BaseModel):
            name: str | None = None


        class Wanted(BaseModel):
            name: str
        ''')
    plain = _compat('Sent().model_dump()', 'Wanted(**data)\n', models=models)
    gapped = _compat('Sent().model_dump()', 'Wanted(**data)\n', models=models, psrTail=(
        "PUBSUB_CONTRACT_GAPS = {'makeWidget_fakeAcct': {'name': {'nullable': 'publisher may omit it (fake)'}}}\n"))
    assert plain['verdict'] == gapped['verdict'] == 'issues'
    assert _issues(gapped, 'name') == {'nullable'} and gapped['rows'] == plain['rows']
    assert gapped['counts'] == plain['counts'] and 'staleGaps' not in gapped


RETIRED_MESSAGE_MODELS_ERROR = (
    'server/PubSubRequests.py: PUBSUB_MESSAGE_MODELS is no longer supported: each handler parses its message with '
    'its generated <Fn>Message (python pubSubModels.py --pull writes objects_<publisher>.py); then delete the dict')


@pytest.mark.parametrize('definition', [
    'PUBSUB_MESSAGE_MODELS = {}',
    'PUBSUB_MESSAGE_MODELS: dict[str, str | dict[str, str]] = {}',
    "PUBSUB_MESSAGE_MODELS = {'makeWidget_fakeAcct': 'Gizmo'}",
    "PUBSUB_MESSAGE_MODELS = {'makeWidget_fakeAcct': {'res': 'Gizmo'}}",
    'if True:\n    PUBSUB_MESSAGE_MODELS = dict()'])
def test_messageModelsInAnyFormIsAHatchError(definition):
    """The retired message-model hatch, defined in any form, is one hatch
    error (failing test_escapeHatchesAreLive) and is not read: a handler it
    names stays unresolved."""
    files = _consumerFiles('''
        def makeWidget_fakeAcct(self, data):
            return dispatch(data)
        ''')
    files['PubSubRequests.py'] += definition + '\n'
    data = pubSubMapSection.buildSection(files).data
    assert data['hatchErrors'] == [RETIRED_MESSAGE_MODELS_ERROR]
    assert not pubSubMapSection.flag(data['handlers']['makeWidget_fakeAcct'], 'resolved')
    assert 'declared' not in data['handlers']['makeWidget_fakeAcct']
    found = [p for p in pubSubMap.problems({'repos': {'fakeShop': data}, 'flows': []}) if p['kind'] == 'hatchError']
    assert found == [{'kind': 'hatchError', 'projects': ['fakeShop'],
                      'message': 'fakeShop: ' + RETIRED_MESSAGE_MODELS_ERROR}]


RETIRED_GAPS_ERROR = ('server/PubSubRequests.py: PUBSUB_CONTRACT_GAPS is no longer supported: fix the mismatch at '
                      'its source (python pubSubMap.py --explain <handler>), then delete the dict')


@pytest.mark.parametrize('definition', [
    'PUBSUB_CONTRACT_GAPS = {}',
    'PUBSUB_CONTRACT_GAPS: dict[str, dict[str, dict[str, str]]] = {}',
    "PUBSUB_CONTRACT_GAPS = {'makeWidget_fakeAcct': {'name': {'nullable': 'a reason long enough'}}}",
    "PUBSUB_CONTRACT_GAPS = {'makeWidget_fakeAcct': {'name': 'a reason, no issue'}}",
    'if True:\n    PUBSUB_CONTRACT_GAPS = dict()'])
def test_contractGapsInAnyFormIsAHatchError(definition):
    """The retired suppression hatch, defined in any form, is one hatch
    error (failing test_escapeHatchesAreLive) and is not read."""
    files = _consumerFiles("def makeWidget_fakeAcct(self, data):\n    return {'n': data['name']}\n")
    files['PubSubRequests.py'] += definition + '\n'
    data = pubSubMapSection.buildSection(files).data
    assert data['hatchErrors'] == [RETIRED_GAPS_ERROR]
    assert not {'gaps', 'compatEnforced'} & (set(data) | set(data['handlers']['makeWidget_fakeAcct']))
    found = pubSubMap.problems({'repos': {'fakeShop': data}, 'flows': []})
    assert found == [{'kind': 'hatchError', 'projects': ['fakeShop'], 'message': 'fakeShop: ' + RETIRED_GAPS_ERROR}]
    assert pubSubMap.ownFailures(found, 'fakeShop', 'hatches') == found

# ---- fake fleets on disk (git) ----------------------------------------------------

def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    """git in a fake repo, blind to GIT_DIR and friends (as pubSubMapScan's
    runGit is); stdin and stdout pass as bytes, so no newline is translated."""
    env = {k: v for k, v in os.environ.items() if k not in pubSubMapScan.GIT_REDIRECT_VARS}
    out = subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                          '-c', 'core.autocrlf=false', *args],
                         input=None if stdin is None else stdin.encode('utf-8'),
                         capture_output=True, check=True, env=env)
    return out.stdout.decode('utf-8', 'replace')


def _writeRepo(root: Path, files: dict, crlf: bool = False) -> Path:
    for rel, text in files.items():
        path = root / 'server' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))
    return root


def _commit(root: Path, message: str = 'fake') -> None:
    if not (root / '.git').exists():
        _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-q', '-m', message)


# Each kind of fake fleet is written and committed once per module, then
# copied into every test's own folder: no test shares a repo, and none pays
# the git spawns (20 ms or more each on Windows) of building it again.
_TEMPLATES = {}


@pytest.fixture(scope='module', autouse=True)
def _fakeRepoTemplates(tmp_path_factory):
    _TEMPLATES.clear()
    _TEMPLATES[None] = tmp_path_factory.mktemp('fakeRepoTemplates')
    yield
    _TEMPLATES.clear()


def _committedRepos(base: Path, key, build) -> dict:
    """build(folder) writes and commits fake repos in folder and returns
    {name: root}: here, a copy under `base` of this module's first build
    of `key` (built straight into `base` when a target folder exists)."""
    folder = _TEMPLATES.get(None)
    if folder is None:
        return build(base)
    if key not in _TEMPLATES:
        built = folder / str(len(_TEMPLATES))
        _TEMPLATES[key] = built, {name: root.relative_to(built) for name, root in build(built).items()}
    built, rels = _TEMPLATES[key]
    if any((base / rel).exists() for rel in rels.values()):
        return build(base)
    for rel in rels.values():
        shutil.copytree(built / rel, base / rel, symlinks=True)
    return {name: base / rel for name, rel in rels.items()}


ACCT_BODY = '''
    @route_config(httpMethod='POST', jwtRequired=True, responseModel=Gizmo)
    def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
        return Gizmo(_id='a', name='b').model_dump(by_alias=True)
    '''
SHOP_HANDLER = 'def makeWidget_fakeAcct(self, data):\n    return Gizmo(**data).model_dump()\n'
EMPTY_PSR = {'PubSubRequests.py': 'class PubSubRequests():\n    pass\n'}


def _fleet(base: Path, crlf: bool = False) -> dict:
    """fake-acct publishes makeWidget to fake-shop and fake-sales; fake-sales
    only knows fake-acct (so discovery has to be transitive)."""
    return _committedRepos(base, ('map', crlf), lambda folder: _buildFleet(folder, crlf))


def _buildFleet(base: Path, crlf: bool) -> dict:
    acctService = {'services/acctService.py': _service('acctService')}
    roots = {
        'fakeAcct': _writeRepo(base / 'fake-acct', _publisherFiles(
            ACCT_BODY, consumers=('shopService', 'salesService'), extra=EMPTY_PSR), crlf),
        'fakeShop': _writeRepo(base / 'fake-shop', _consumerFiles(SHOP_HANDLER, extra=acctService), crlf),
        'fakeSales': _writeRepo(base / 'fake-sales', _consumerFiles(
            "def makeWidget_fakeAcct(self, data):\n    return {'n': data['name']}\n", name='fakeSales',
            className='FakeSalesActions', extra=acctService), crlf)}
    for root in roots.values():
        _commit(root)
    return roots


def _flow(fleet, consumer: str, publisher: str = 'fakeAcct') -> dict:
    return [f for f in fleet.data['flows'] if f['consumer'] == consumer and f['publisher'] == publisher][0]


def _files(root: Path) -> dict:
    """{relative path: bytes} of every file under root outside .git."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*'))
            if p.is_file() and '.git' not in p.relative_to(root).parts}


def test_fleetJoinsPublishersToHandlersTransitively(tmp_path):
    roots = _fleet(tmp_path)
    fleet = pubSubMap.buildMap(roots['fakeSales'])
    assert sorted(fleet.data['repos']) == ['fakeAcct', 'fakeSales', 'fakeShop'], \
        'fake-shop is only reachable through fake-acct services'
    flows = {(f['topic'], f['consumer']): f for f in fleet.data['flows']}
    assert flows[('makeWidget_fakeAcct', 'fakeShop')]['status'] == 'ok'
    assert flows[('makeWidget_fakeAcct', 'fakeSales')]['handler'] == 'makeWidget_fakeAcct'
    assert fleet.data['repos']['fakeSales']['localRepoDirName'] == 'fake-sales'
    assert not fleet.problems()


def test_missingHandlerAndOrphanHandlerAreProblems(tmp_path):
    roots = _fleet(tmp_path)
    psr = roots['fakeSales'] / 'server' / 'PubSubRequests.py'
    psr.write_text(_handlers("def makeThing_fakeAcct(self, data):\n    return {'n': data['name']}\n",
                             "def other_fakeNobody(self, data):\n    return {'n': data['name']}\n",
                             className='FakeSalesActions'), encoding='utf-8')
    fleet = pubSubMap.buildMap(roots['fakeSales'])
    assert _flow(fleet, 'fakeSales')['status'] == 'missingHandler'
    assert [o['handler'] for o in fleet.data.get('orphanHandlers', [])] == ['makeThing_fakeAcct']
    assert [u['source'] for u in fleet.data.get('unknownSourceHandlers', [])] == ['fakeNobody']
    found = {p['kind']: p for p in fleet.problems()}
    assert sorted(found) == ['missingHandler', 'orphanHandler', 'unknownSource']
    assert 'has no makeWidget_fakeAcct' in found['missingHandler']['message']
    assert found['missingHandler']['projects'] == ['fakeAcct', 'fakeSales']
    assert found['orphanHandler']['projects'] == found['unknownSource']['projects'] == ['fakeSales']


def test_repoUnknownIsAProblemAndBlocksWrite(tmp_path, capsys):
    """A consumer with no checkout and no snapshot section: the flow is
    repoUnknown (one problem, for the publisher), and --write refuses."""
    roots = _fleet(tmp_path)
    roots['fakeShop'].rename(tmp_path / 'away')
    fleet = pubSubMap.buildMap(roots['fakeAcct'])
    message = ('makeWidget_fakeAcct: fakeShop is not in the map: no checkout at ../fake-shop and no '
               'snapshot section; clone it or pass --repo fakeShop=<path>, then run `python pubSubMap.py '
               '--write`.')
    assert fleet.problems() == [{'kind': 'repoUnknown', 'projects': ['fakeAcct'], 'message': message,
                                 'publisher': 'fakeAcct', 'consumer': 'fakeShop'}]
    assert '- ' + message in fleet.markdown.splitlines()
    assert pubSubMap.main([], roots['fakeAcct']) == 1
    out = capsys.readouterr().out
    assert 'FAIL: ' + message in out, "this repo's publisher addresses it: a failure here"
    assert out.rstrip().endswith('fix each as it says. ' + REGENERATE)
    assert '--write` and commit' not in out.replace(REGENERATE, ''), 'no plain "run --write" advice'
    assert pubSubMap.main(['--write'], roots['fakeAcct']) == 2
    assert 'refusing to write the map: fakeShop is not in the map' in capsys.readouterr().err
    assert not (roots['fakeAcct'] / 'server' / MAP_JSON).exists()


def test_checkModeFailsOnlyOnThisReposProblems(tmp_path, capsys):
    """`python pubSubMap.py` fails like the real-fleet tests: a missing
    handler in fake-sales fails the check of fake-acct (its publisher) and
    of fake-sales (exit 1), but is only a WARNING for fake-shop, which is
    neither end of that flow."""
    roots = _fleet(tmp_path)
    acct, sales, shop = roots['fakeAcct'], roots['fakeSales'], roots['fakeShop']
    psr = sales / 'server' / 'PubSubRequests.py'
    psr.write_text(psr.read_text(encoding='utf-8').replace('def makeWidget_fakeAcct', 'def makeThing_fakeAcct'),
                   encoding='utf-8')
    _commit(sales)
    for root in (acct, sales, shop):
        pubSubMap.writeMap(root)
    capsys.readouterr()
    assert pubSubMap.main([], acct) == 1, 'a missing handler for an own publisher'
    out = capsys.readouterr().out
    assert any(line.startswith('FAIL: ') and 'has no makeWidget_fakeAcct' in line for line in out.splitlines())
    assert out.rstrip().splitlines()[-1].startswith('FAILED: 1 problem(s) for fakeAcct')
    assert pubSubMap.main([], sales) == 1
    out = capsys.readouterr().out
    assert any(line.startswith('FAIL: ') and 'has no makeWidget_fakeAcct' in line for line in out.splitlines())
    assert out.rstrip().splitlines()[-1].startswith('FAILED: 2 problem(s) for fakeSales'), 'and the orphan'
    assert pubSubMap.main([], shop) == 0
    out = capsys.readouterr().out
    assert any(line.startswith('WARNING: ') and 'has no makeWidget_fakeAcct' in line for line in out.splitlines())
    assert 'FAIL' not in out and 'STALE' not in out


def test_problemLinesNameTheConsumerOnceAndSayWhyAFolderIsUnscanned(tmp_path):
    roots = _fleet(tmp_path)
    shop = roots['fakeShop']
    psr = shop / 'server' / 'PubSubRequests.py'
    psr.write_text(psr.read_text(encoding='utf-8').replace(
        'return Gizmo(**data).model_dump()', "data['missing']\n        Gizmo(**data['missing'])\n        return {}"),
        encoding='utf-8')
    _commit(shop)
    found = [p['message'] for p in pubSubMap.buildMap(shop).problems() if p['kind'] == 'compatIssue']
    assert len(found) == 1, 'a key read and a model parse of one path make one line'
    assert found[0].startswith('makeWidget_fakeAcct -> fakeShop.makeWidget_fakeAcct: missing is undeclared')
    files = _publisherFiles(ACCT_BODY)
    files['actions/sub/WidgetActions.py'] = files.pop('actions/WidgetActions.py')
    data = pubSubMapSection.buildSection(files).data
    assert [p['message'] for p in pubSubMap.problems({'repos': {'fakeAcct': data}, 'flows': []})] == [
        'fakeAcct actions/sub/WidgetActions.py::WidgetActions.makeWidget is not directly in server/ or '
        'server/actions/ (pubSubAdmin only scans those), so pubSubAdmin.py never creates topic '
        'makeWidget_fakeAcct. Move it.']



def test_aCompatIssueFailsBothEndsAndNothingSuppressesIt(tmp_path, capsys):
    """A compat issue fails the compat test and check mode of the publisher
    and of the consumer (a third repo only warns); a PUBSUB_CONTRACT_GAPS
    entry for it only adds a hatch error."""
    roots = _fleet(tmp_path)
    acct, sales, shop = roots['fakeAcct'], roots['fakeSales'], roots['fakeShop']
    psr = shop / 'server' / 'PubSubRequests.py'
    psr.write_text(psr.read_text(encoding='utf-8').replace(
        'return Gizmo(**data).model_dump()', "return {'m': data['missing']}")
        + "PUBSUB_CONTRACT_GAPS = {'makeWidget_fakeAcct': {'missing': {'undeclared': 'known for now (fake)'}}}\n",
        encoding='utf-8')
    _commit(shop)
    for root in (acct, sales, shop):
        pubSubMap.writeMap(root)
    fleet = pubSubMap.buildMap(shop)
    assert _flow(fleet, 'fakeShop')['compat']['verdict'] == 'issues'
    assert not any(word in fleet.json for word in ('knownGap', 'staleGap', '"gaps"', 'compatEnforced'))
    found = fleet.problems()
    assert [(p['kind'], p['projects']) for p in found] == [
        ('compatIssue', ['fakeAcct', 'fakeShop']), ('hatchError', ['fakeShop'])]
    issue = found[0]
    assert issue['message'].endswith('Fix the payload or the handler (python pubSubMap.py --explain '
                                     'makeWidget_fakeAcct).')
    assert pubSubMap.ownFailures(found, 'fakeAcct', 'compat') == pubSubMap.ownFailures(found, 'fakeShop', 'compat') \
        == [issue]
    assert pubSubMap.ownFailures(found, 'fakeSales') == []
    capsys.readouterr()
    for root, failed in ((acct, 1), (shop, 2)):
        assert pubSubMap.main([], root) == 1
        lines = capsys.readouterr().out.rstrip().splitlines()
        assert 'FAIL: ' + issue['message'] in lines
        assert 'note: 1 compat issue(s) in the fleet; each fails the tests of both repos of its flow' in lines
        assert lines[-1].startswith(f'FAILED: {failed} problem(s)'), 'the hatch error fails only the consumer'
    assert pubSubMap.main([], sales) == 0
    out = capsys.readouterr().out
    assert 'WARNING: ' + issue['message'] in out.splitlines() and 'FAIL' not in out


# The code of create-star-app's PubSubRequests.py.tmpl, with fake names: what
# a new project starts with (no escape hatch at all).
SCAFFOLD_PSR = _src('''
    from route_config import route_config


    class FakePingActions():

        @route_config(httpMethod='POST',
                      jwtRequired=False,
                      successMessage='Pong')
        def fakePing(self, data):
            return {'ping': 'pong'}


    class PubSubRequests(FakePingActions):

        def __init__(self):
            FakePingActions.__init__(self)
    ''')

# A first flow added to fresh scaffolds: a handler mixin in fake-shop.
SCAFFOLD_HANDLER = _src('''

    from models import Gizmo


    class FakeActions():

        @route_config(httpMethod='POST', jwtRequired=False)
        def makeWidget_fakeAcct(self, data):
            return Gizmo(**data).model_dump()


    class PubSubRequests(FakePingActions, FakeActions):
        pass
    ''')


def test_aFreshScaffoldFleetPasses(tmp_path, capsys):
    """Two new projects (the scaffold's PubSubRequests.py) pass the map's
    checks, before and after their first flow."""
    acct = _writeRepo(tmp_path / 'fake-acct', {
        'AppConfig.py': APPCONFIG.format(name='fakeAcct'), 'models.py': MODELS, 'PubSubRequests.py': SCAFFOLD_PSR,
        'services/shopService.py': _service('shopService')})
    shop = _writeRepo(tmp_path / 'fake-shop', {
        'AppConfig.py': APPCONFIG.format(name='fakeShop'), 'models.py': MODELS, 'PubSubRequests.py': SCAFFOLD_PSR,
        'services/acctService.py': _service('acctService')})

    def passes(flows: int):
        for root in (acct, shop):
            _commit(root)
        for root in (acct, shop):
            assert pubSubMap.main(['--write'], root) == 0
        for root in (acct, shop):
            fleet = pubSubMap.buildMap(root)
            assert fleet.problems() == [] and len(fleet.data['flows']) == flows
            assert fleet.data['repos']['fakeShop']['utilityHandlers'] == ['fakePing']
            assert pubSubMap.main([], root) == 0
            assert 'FAIL' not in capsys.readouterr().out
    passes(flows=0)
    _writeRepo(acct, {'actions/WidgetActions.py': _publisherFiles(ACCT_BODY)['actions/WidgetActions.py']})
    _writeRepo(shop, {'PubSubRequests.py': SCAFFOLD_PSR + SCAFFOLD_HANDLER})
    passes(flows=1)
    assert _flow(pubSubMap.buildMap(shop), 'fakeShop')['compat']['verdict'] == 'ok'


def test_aDynamicKeyReadIsAnInfoRowAndListedInTheMarkdown(tmp_path):
    roots = _fleet(tmp_path)
    shop = roots['fakeShop']
    psr = shop / 'server' / 'PubSubRequests.py'
    psr.write_text(psr.read_text(encoding='utf-8').replace(
        'return Gizmo(**data).model_dump()', "Gizmo(**data)\n        return {'v': data[pickFakeKey()]}"),
        encoding='utf-8')
    fleet = pubSubMap.buildMap(shop)
    compat = _flow(fleet, 'fakeShop')['compat']
    assert [(r['path'], r.get('kind'), r['result']) for r in compat['rows'] if r['result'] == 'dynamicKey'] == [
        ('(root)', 'dynamicKey', 'dynamicKey')]
    assert compat['verdict'] == 'ok' and not fleet.problems(), 'information, not a failure'
    assert '- fakeShop `makeWidget_fakeAcct`: a key of `(root)`' in fleet.markdown.splitlines()

# Sibling code that would leave a marker if anything ran it: --write-all
# must only parse a sibling, never import or run it.
MARKER_TOOL = "\nfrom pathlib import Path\nPath({marker!r}).write_text('ran')\n"


def _adopt(root: Path, marker: Path, crlf: bool = False) -> None:
    """Give a fake repo this process's pubSubMap tools (so --write-all may
    write it) and an AppConfig.py that would leave `marker` if run."""
    here = Path(pubSubMap.__file__).resolve().parent
    files = {name: (here / name).read_text(encoding='utf-8') for name in pubSubMapScan.TOOL_MODULES}
    files['AppConfig.py'] = (root / 'server' / 'AppConfig.py').read_text(encoding='utf-8').replace(
        '\r\n', '\n') + MARKER_TOOL.format(marker=str(marker))
    _writeRepo(root, files, crlf)
    _commit(root, 'adopt the map')


def _gitState(roots) -> dict:
    """{dir: (HEAD, staged names)} of each fake repo."""
    return {r.name: (_git(r, 'rev-parse', 'HEAD'), _git(r, 'diff', '--cached', '--name-only')) for r in roots}


def test_writeAllWritesThisRepoAndEverySiblingOnTheBranch(tmp_path, capsys):
    roots = _fleet(tmp_path / 'fleet')
    marker = tmp_path / 'marker'
    for name in ('fakeAcct', 'fakeSales'):
        _adopt(roots[name], marker)
    before = _gitState(roots.values())
    assert pubSubMap.main(['--write-all'], roots['fakeShop']) == 0
    out = capsys.readouterr().out.splitlines()
    wrote = 'wrote server/pubsub_map.json, server/PUBSUB_MAP.md'
    assert out[:3] == [f'fake-shop: {wrote}', f'fake-acct: {wrote}', f'fake-sales: {wrote}']
    assert out[3] == 'Commit the regenerated map in: fake-shop, fake-acct, fake-sales'
    assert '  git -C ../fake-acct add server/pubsub_map.json server/PUBSUB_MAP.md && git -C ../fake-acct commit' in out
    for root in roots.values():
        assert pubSubMap.main([], root) == 0, f'{root.name} is fresh'
    assert not marker.exists(), "no sibling code is run"
    assert _gitState(roots.values()) == before, 'HEADs unchanged and nothing staged'
    capsys.readouterr()
    assert pubSubMap.main(['--write-all'], roots['fakeShop']) == 0
    assert capsys.readouterr().out.splitlines() == [
        'fake-shop: unchanged', 'fake-acct: unchanged', 'fake-sales: unchanged', 'No map changed; nothing to commit.']


def test_writeAllSkipsSiblingsOnAnotherBranchDetachedOrNotAdopted(tmp_path, capsys):
    roots = _fleet(tmp_path / 'fleet')
    acct, sales, shop = roots['fakeAcct'], roots['fakeSales'], roots['fakeShop']
    _adopt(sales, tmp_path / 'marker')
    _git(sales, 'checkout', '-q', '-b', 'elsewhere')
    files = {r.name: _files(r) for r in (acct, sales)}
    assert pubSubMap.main(['--write-all'], shop) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith('fake-shop: wrote ')
    assert 'fake-acct: skipped (not adopted yet: no server/pubSubMap.py)' in out
    assert ('fake-sales: skipped (fake-sales is on elsewhere, this repo is on main; switch it, or run '
            '`python pubSubMap.py --write` there)') in out
    assert {r.name: _files(r) for r in (acct, sales)} == files, 'skipped siblings are untouched'
    _git(sales, 'checkout', '-q', '--detach', 'main')
    assert pubSubMap.main(['--write-all'], shop) == 0
    assert 'fake-sales: skipped (fake-sales is on a detached HEAD)' in capsys.readouterr().out.splitlines()
    assert _files(sales) == files['fake-sales']


def test_writeAllReportsASiblingErrorAndGoesOn(tmp_path, capsys):
    roots = _fleet(tmp_path / 'fleet')
    for name in ('fakeAcct', 'fakeSales'):
        _adopt(roots[name], tmp_path / 'marker')
    _writeRepo(roots['fakeSales'], {'broken.py': 'from pubSub import pubSubDecorator\ndef (:\n'})  # uncommitted
    assert pubSubMap.main(['--write-all'], roots['fakeShop']) == 2
    out = capsys.readouterr().out.splitlines()
    assert any(line.startswith('fake-sales: error (Could not parse server/broken.py') for line in out)
    assert out[0].startswith('fake-shop: wrote ') and any(line.startswith('fake-acct: wrote ') for line in out)
    assert (roots['fakeAcct'] / 'server' / MAP_JSON).is_file()
    assert not (roots['fakeSales'] / 'server' / MAP_JSON).exists()


def test_writeAllSkipsASiblingDiscoveryRejected(tmp_path):
    roots = _fleet(tmp_path / 'fleet')
    acct = roots['fakeAcct']
    _adopt(acct, tmp_path / 'marker')
    assert pubSubMap.writeMap(roots['fakeShop'])[:2] == WROTE_MAP, 'a snapshot keeps fake-acct in the map'
    _writeRepo(acct, {'AppConfig.py': APPCONFIG.format(name='fakeImpostor')})
    _commit(acct, 'another project now')
    lines, code = pubSubMap.writeAll(roots['fakeShop'])
    assert code == 0 and lines[0] == 'fake-shop: unchanged'
    assert any(line.startswith('fake-acct: skipped (') and "declares projectName 'fakeImpostor'" in line
               for line in lines)
    assert not (acct / 'server' / MAP_JSON).exists()


def test_writeAllKeepsGoingAfterAnUnexpectedError(tmp_path, monkeypatch):
    roots = _fleet(tmp_path / 'fleet')
    for name in ('fakeAcct', 'fakeSales'):
        _adopt(roots[name], tmp_path / 'marker')
    build, sales = pubSubMap.buildMap, roots['fakeSales'].resolve()

    def buildMap(root, *args, **kwargs):
        if Path(root).resolve() == sales:
            raise RuntimeError('boom')
        return build(root, *args, **kwargs)
    monkeypatch.setattr(pubSubMap, 'buildMap', buildMap)
    lines, code = pubSubMap.writeAll(roots['fakeShop'])
    assert code == 2 and 'fake-sales: error (RuntimeError: boom)' in lines
    assert lines[0].startswith('fake-shop: wrote ') and any(line.startswith('fake-acct: wrote ') for line in lines)


def test_writeAllSkipsASiblingWhoseToolsDiffer(tmp_path):
    """A half-done rollout: a sibling with other pubSubMap*.py would get a
    map its own tests call stale. Line endings do not count."""
    roots = _fleet(tmp_path / 'fleet')
    acct, sales = roots['fakeAcct'], roots['fakeSales']
    _adopt(acct, tmp_path / 'marker', crlf=True)
    _adopt(sales, tmp_path / 'marker')
    tool = sales / 'server' / 'pubSubMapSection.py'
    tool.write_text(tool.read_text(encoding='utf-8') + '# a local edit\n', encoding='utf-8')
    lines, _ = pubSubMap.writeAll(roots['fakeShop'])
    assert any(line.startswith('fake-acct: wrote ') for line in lines), 'CRLF copies are the same tools'
    assert ("fake-sales: skipped (fake-sales's pubSubMap tools differ from this repo's; run "
            '`python sharedFileCheck.py --write` there first)') in lines
    assert not (sales / 'server' / MAP_JSON).exists()


def test_writesNeverFollowASymlinkOrLeaveTheRepo(tmp_path, monkeypatch):
    roots = _fleet(tmp_path / 'fleet')
    shop = roots['fakeShop']
    roots['fakeAcct'].rename(tmp_path / 'away')  # so shop has a sibling note to write
    fleet = pubSubMap.buildMap(shop)
    isSymlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self.name == MAP_JSON or isSymlink(self))
    with pytest.raises(pubSubMapScan.MapError, match='it or its folder is a symlink'):
        pubSubMap._writeFleet(shop, pubSubMap.buildMap(shop, {'fakeAcct': (tmp_path / 'away', None)}))
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self.name == 'services' or isSymlink(self))
    assert pubSubMap.syncSiblingNotes(shop, fleet) == [
        f"skipped the sibling notes: refusing to write {shop / 'server' / 'services' / ('x' + NOTE)}: it or its "
        'folder is a symlink']
    monkeypatch.undo()
    outside = tmp_path / 'outside'
    outside.mkdir()
    services = shop / 'server' / 'services'
    for f in services.iterdir():
        f.unlink()
    services.rmdir()
    try:
        import _winapi
        _winapi.CreateJunction(str(outside), str(services))
    except (ImportError, OSError, AttributeError):
        pytest.skip('cannot create a junction here')
    assert pubSubMap.syncSiblingNotes(shop, fleet)[0].startswith('skipped the sibling notes: refusing to write')
    assert not list(outside.iterdir()), 'nothing written through the junction'


def test_writeAllRefusesWorktreeSiblings(tmp_path, capsys):
    shop = _fleet(tmp_path)['fakeShop']
    assert pubSubMap.main(['--write-all', '--worktree-siblings'], shop) == 2
    assert '--write-all cannot be combined with --worktree-siblings' in capsys.readouterr().err
    assert not (shop / 'server' / MAP_JSON).exists()


def test_writeRefusesOnlyWhileItWouldLoseASibling(tmp_path, capsys):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    _writeRepo(acct, {'services/exampleService.py': _service('exampleService', 'fakeExample', 'example-repo')})
    assert pubSubMap.main(['--write'], acct) == 0, 'a declared sibling nothing involves does not block'
    assert (acct / 'server' / 'services' / ('exampleService' + NOTE)).is_file()
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    jsonPath = shop / 'server' / MAP_JSON
    data = json.loads(jsonPath.read_text(encoding='utf-8'))
    jsonPath.write_text(json.dumps(dict(data, formatVersion=1)), encoding='utf-8')
    before = _files(shop)
    acct.rename(tmp_path / 'away')
    capsys.readouterr()
    assert pubSubMap.main(['--write'], shop) == 2, 'the old-format snapshot cannot stand in for fake-acct'
    assert 'refusing to write the map: fakeAcct is not in the map: no checkout at ../fake-acct' in \
        capsys.readouterr().err
    after = _files(shop)
    assert {k for k in after if before.get(k) != after[k]} == {'server/services/acctService' + NOTE}


def test_siblingsAreReadAtTheirCommittedRef(tmp_path):
    roots = _fleet(tmp_path)
    acct = roots['fakeAcct'] / 'server' / 'actions' / 'WidgetActions.py'
    acct.write_text(acct.read_text().replace('salesService])', 'salesService], publishToProdPubSub=False)'),
                    encoding='utf-8')
    before = pubSubMap.buildMap(roots['fakeShop']).data['repos']['fakeAcct']['publishers']['makeWidget']
    assert pubSubMapSection.flag(before, 'prodPublish'), 'an uncommitted sibling change is ignored'
    _commit(roots['fakeAcct'], 'mock only')
    after = pubSubMap.buildMap(roots['fakeShop']).data['repos']['fakeAcct']['publishers']['makeWidget']
    assert pubSubMapSection.flag(after, 'prodPublish') is False, 'a committed one is seen'


def test_repoOverridesAndWorktreeSiblings(tmp_path):
    roots = _fleet(tmp_path)
    shop, moved = roots['fakeShop'], tmp_path / 'else' / 'acct@copy'
    moved.parent.mkdir()
    roots['fakeAcct'].rename(moved)
    actions = moved / 'server' / 'actions' / 'WidgetActions.py'
    _git(moved, 'checkout', '-q', '-b', 'mockOnly')
    actions.write_text(actions.read_text().replace('salesService])', 'salesService], publishToProdPubSub=False)'),
                       encoding='utf-8')
    _commit(moved, 'mock only')
    _git(moved, 'checkout', '-q', 'main')

    def prod(repo=(), worktree=False):
        fleet = pubSubMap.buildMap(shop, pubSubMap._parseOverrides(list(repo)), worktree)
        return pubSubMapSection.flag(fleet.data['repos']['fakeAcct']['publishers']['makeWidget'], 'prodPublish')
    assert prod([f'fakeAcct={moved}']) is True, "a path holding '@' and no ref"
    assert prod([f'fakeAcct={moved}@refs/heads/mockOnly']) is False, 'rpartition: the last @ starts the ref'
    actions.write_text(actions.read_text().replace('salesService])', 'salesService], publishToProdPubSub=False)'),
                       encoding='utf-8')
    assert prod([f'fakeAcct={moved}']) is True, 'uncommitted'
    assert prod([f'fakeAcct={moved}'], worktree=True) is False, '--worktree-siblings reads it'
    assert pubSubMap._parseOverrides(['a=/x@refs/heads/b']) == {'a': (Path('/x'), 'refs/heads/b')}
    with pytest.raises(pubSubMapScan.MapError):
        pubSubMap._parseOverrides(['nameOnly'])
    warnings = pubSubMap.buildMap(shop, {'fakeShop': (shop, None), 'fakeNobody': (shop, None)}).warnings
    assert warnings == ['--repo fakeNobody=... ignored: no services file or snapshot names that project',
                        '--repo fakeShop=... ignored: that is this repo']


def test_absentSiblingFallsBackToTheSnapshot(tmp_path):
    roots = _fleet(tmp_path)
    assert pubSubMap.writeMap(roots['fakeShop']) == WROTE_MAP
    written = (roots['fakeShop'] / 'server' / MAP_JSON).read_bytes()
    roots['fakeAcct'].rename(tmp_path / 'fake-acct-moved')
    fleet = pubSubMap.buildMap(roots['fakeShop'])
    src = fleet.sources['fakeAcct']
    assert src.section is None and 'no checkout' in src.reason
    assert fleet.json.encode('utf-8') == written, 'the snapshot keeps the absent sibling and its flows'
    assert not fleet.problems()


def test_lonePublisherRechecksTheSnapshotConsumerAgainstItsLivePayload(tmp_path):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    assert pubSubMap.writeMap(acct) == WROTE_MAP
    written = (acct / 'server' / MAP_JSON).read_bytes()
    shop.rename(tmp_path / 'away')
    assert pubSubMap.buildMap(acct).json.encode('utf-8') == written, 'unchanged: the same map, recomputed'
    actions = acct / 'server' / 'actions' / 'WidgetActions.py'
    actions.write_text(actions.read_text(encoding='utf-8').replace(
        "Gizmo(_id='a', name='b').model_dump(by_alias=True)", "{'_id': 'a', 'name': None, 'count': 1.5}"),
        encoding='utf-8')
    compat = _flow(pubSubMap.buildMap(acct), 'fakeShop')['compat']
    assert compat['verdict'] == 'issues'
    assert sorted((r['path'], r['issue']) for r in compat['rows'] if r['result'] == 'issue') == [
        ('count', 'numericNarrowing'), ('name', 'nullable')], 'the printed consumer types are checked too'


def test_loneConsumerReusesSnapshotResultsOnlyForUnchangedRequirements(tmp_path):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    psr = shop / 'server' / 'PubSubRequests.py'
    psr.write_text(psr.read_text(encoding='utf-8').replace(
        '    return Gizmo(**data)', "    data.get('size')\n        return Gizmo(**data)"), encoding='utf-8')
    _commit(shop)
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    written = (shop / 'server' / MAP_JSON).read_bytes()
    acct.rename(tmp_path / 'away')
    assert pubSubMap.buildMap(shop).json.encode('utf-8') == written
    psr.write_text(psr.read_text(encoding='utf-8').replace("data.get('size')", "len(data['size'])"),
                   encoding='utf-8')
    compat = _flow(pubSubMap.buildMap(shop), 'fakeShop')['compat']
    assert compat['verdict'] == 'unverified'
    assert [(r['path'], r['result']) for r in compat['rows']] == [('secret', 'deadRead'), ('size', 'unverified')], \
        'an unchanged row keeps its recorded result; the tightened read does not get the old ok'


PAINT_MODELS = _src('''
    from enum import Enum, auto


    class Color(Enum):
        RED = auto()


    class StrictPaint(BaseModel):
        model_config = ConfigDict(extra='forbid')
        name: str
        color: Color
    ''')


@pytest.mark.parametrize('payload, issues', [("{'name': 'x', 'color': 'red'}", 0),
                                             ("{'name': 'x', 'color': 'red', 'other': 1}", 1)])
def test_loneCloneWithAForbidConsumerAndAValuelessEnumIsReproducible(tmp_path, payload, issues):
    roots = _fleet(tmp_path)
    acct, shop = roots['fakeAcct'], roots['fakeShop']
    actions = acct / 'server' / 'actions' / 'WidgetActions.py'
    actions.write_text(actions.read_text(encoding='utf-8').replace(
        "Gizmo(_id='a', name='b').model_dump(by_alias=True)", payload), encoding='utf-8')
    _writeRepo(shop, {'models.py': MODELS + PAINT_MODELS, 'PubSubRequests.py': 'from models import StrictPaint\n'
                      + _handlers('def makeWidget_fakeAcct(self, data):\n    return StrictPaint(**data).model_dump()\n')})
    _commit(shop)
    assert pubSubMap.writeMap(acct)[:2] == WROTE_MAP
    written = (acct / 'server' / MAP_JSON).read_bytes()
    data = json.loads(written)
    reqs = {r['path']: r for r in data['repos']['fakeShop']['handlers']['makeWidget_fakeAcct']['requirements']}
    assert reqs['(root)']['extraForbid'] == ['color', 'name'] and reqs['color']['consumerType'] == 'Color[...]'
    compat = _flow(pubSubMap.buildMap(acct), 'fakeShop')['compat']
    found = {(r['path'], r['issue']) for r in compat.get('rows', []) if r['result'] == 'issue'}
    assert found == ({('(root)', 'forbiddenExtra')} if issues else set()) and 'unverified' not in compat['counts']
    shop.rename(tmp_path / 'away')
    assert pubSubMap.buildMap(acct).json.encode('utf-8') == written, 'the snapshot reproduces every row'


def test_aCheckThatCannotRunOutranksMaybe():
    pub = pubSubMapSection.buildSection(_publisherFiles('''
        def makeWidget(self, doc: dict, publishToProdPubSub, publishToPubSubMockDb=False):
            d = {}
            if doc:
                d['name'] = 'x'
            return d
        '''))
    shape = pub.shapes['makeWidget']
    readable = {'path': 'name', 'required': True, 'nonNull': True, 'consumerType': 'str'}
    Requirement = pubSubMapContract.Requirement
    assert pubSubMap.checkRequirement(pub, shape, Requirement.fromJson(readable))['result'] == 'maybe'
    edited = Requirement.fromJson(dict(readable, consumerType='not a type ('))
    assert edited.typeUnknown and pubSubMap.checkRequirement(pub, shape, edited)['result'] == 'unverified'


def test_aSnapshotRepoNoOneDeclaresIsRetired(tmp_path):
    roots = _fleet(tmp_path)
    shop, acct = roots['fakeShop'], roots['fakeAcct']
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    _writeRepo(acct, _publisherFiles(ACCT_BODY, consumers=('shopService',)))
    (acct / 'server' / 'services' / 'salesService.py').unlink()
    _commit(acct, 'drop fake-sales')
    roots['fakeSales'].rename(tmp_path / 'away')
    fleet = pubSubMap.buildMap(shop)
    assert sorted(fleet.data['repos']) == ['fakeAcct', 'fakeShop'] and fleet.retired == ['fakeSales']
    assert [(p['kind'], p['projects']) for p in fleet.problems()] == [('retiredRepo', [])]
    assert pubSubMap.writeMap(shop) == WROTE_MAP, 'no blocker, no note, not a problem line'
    assert not list((shop / 'server' / 'services').glob('*' + NOTE))


def test_siblingThatIsAnotherProjectCountsAsAbsent(tmp_path):
    roots = _fleet(tmp_path)
    config = roots['fakeAcct'] / 'server' / 'AppConfig.py'
    config.write_text(APPCONFIG.format(name='fakeImpostor'), encoding='utf-8')
    _commit(roots['fakeAcct'])
    src = pubSubMap.buildMap(roots['fakeShop']).sources['fakeAcct']
    assert src.section is None and "'fakeImpostor'" in src.reason


def test_siblingParseErrorNamesTheCheckoutRefAndFile(tmp_path):
    roots = _fleet(tmp_path)
    _writeRepo(roots['fakeAcct'], {'broken.py': 'from pubSub import pubSubDecorator\ndef (:\n'})
    _commit(roots['fakeAcct'])
    with pytest.raises(pubSubMapScan.MapError) as e:
        pubSubMap.buildMap(roots['fakeShop'])
    assert str(e.value).startswith('fake-acct@refs/heads/main: Could not parse server/broken.py (line 2')


def test_conflictingDirNamesAreAHardError(tmp_path):
    roots = _fleet(tmp_path)
    (roots['fakeShop'] / 'server' / 'services' / 'salesService.py').write_text(
        _service('salesService', 'fakeSales', 'somewhere-else'), encoding='utf-8')
    with pytest.raises(pubSubMapScan.MapError) as e:
        pubSubMap.buildMap(roots['fakeShop'])
    assert 'somewhere-else' in str(e.value) and 'fake-sales' in str(e.value)
    assert pubSubMap.main([], roots['fakeShop']) == 2


def test_anUnexpectedErrorExitsTwo(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError('fake bug')
    monkeypatch.setattr(pubSubMap, 'buildMap', boom)
    assert pubSubMap.main([], tmp_path) == 2
    assert 'unexpected RuntimeError: fake bug' in capsys.readouterr().err


def test_checkModeAndWrite(tmp_path):
    roots = _fleet(tmp_path)
    shop = roots['fakeShop']
    assert pubSubMap.main([], shop) == 1, 'no map yet: stale'
    assert pubSubMap.main(['--write'], shop) == 0
    assert pubSubMap.main([], shop) == 0
    md = shop / 'server' / MAP_MD
    md.write_text(md.read_text(encoding='utf-8') + 'hand edit\n', encoding='utf-8')
    assert pubSubMap.main([], shop) == 1
    assert pubSubMap.main(['--explain', 'makeWidget_fakeAcct'], shop) == 0


def test_crlfMapFilesAreFreshAndNotRewritten(tmp_path):
    shop = _fleet(tmp_path)['fakeShop']
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    server = shop / 'server'
    for name in (MAP_JSON, MAP_MD):
        (server / name).write_bytes((server / name).read_bytes().replace(b'\n', b'\r\n'))
    before = _files(shop)
    assert pubSubMap.main([], shop) == 0, 'check mode ignores CRLF'
    assert pubSubMap.writeMap(shop) == [] and _files(shop) == before, 'and --write leaves them alone'
    assert _layoutErrors(shop) == []
    (server / MAP_JSON).write_text(json.dumps(json.loads((server / MAP_JSON).read_text(encoding='utf-8'))),
                                   encoding='utf-8')
    assert _layoutErrors(shop) == [
        'server/pubsub_map.json is not laid out the way pubSubMap.py writes it (edited by hand?).']


def test_outputIsDeterministicAcrossBuildsAndLineEndings(tmp_path):
    lf = _fleet(tmp_path / 'lf')
    crlf = _fleet(tmp_path / 'crlf', crlf=True)
    first = pubSubMap.buildMap(lf['fakeShop'])
    assert pubSubMap.buildMap(lf['fakeShop']).json == first.json
    assert pubSubMap.buildMap(crlf['fakeShop']).json == first.json
    assert first.markdown == pubSubMap.renderMarkdown(json.loads(first.json))
    assert pubSubMap.BLIND_SPOT in first.markdown
    anchors = [p['anchor'] for e in first.data['repos'].values() for p in e.get('publishers', {}).values()]
    anchors += [h['anchor'] for e in first.data['repos'].values() for h in e.get('handlers', {}).values()]
    assert anchors and not any('\\' in a for a in anchors)


def test_jsonRowsAreOneLineAndOkRowsStoreNothing(tmp_path):
    """Every row is one deterministic line; a flow stores counts and only
    its non-ok rows; provenance (`via`) is not stored."""
    roots = _fleet(tmp_path)
    shop = roots['fakeShop'] / 'server' / 'PubSubRequests.py'
    shop.write_text(shop.read_text(encoding='utf-8').replace(
        'return Gizmo(**data).model_dump()', 'data["missing"]\n        return Gizmo(**data).model_dump()'),
        encoding='utf-8')
    fleet = pubSubMap.buildMap(roots['fakeShop'])
    lines = [line.strip().rstrip(',') for line in fleet.json.splitlines()]
    reqs = fleet.data['repos']['fakeShop']['handlers']['makeWidget_fakeAcct']['requirements']
    publishers = list(fleet.data['repos']['fakeAcct']['publishers'].values())
    flow = _flow(fleet, 'fakeShop')
    for row in reqs + publishers + flow['compat']['rows']:
        oneLine = json.dumps(row, sort_keys=True, ensure_ascii=True)
        assert any(line == oneLine or line.endswith(': ' + oneLine) for line in lines), oneLine
    assert '"via"' not in fleet.json and 'prodPublish' not in fleet.json
    assert {r['path']: r['result'] for r in flow['compat']['rows']} == {
        'missing': 'issue', 'secret': 'deadRead'}, 'ok rows are not stored; an excluded field is a dead read'
    assert sum(flow['compat']['counts'].values()) == len(reqs) and flow['compat']['counts']['issue'] == 1
    explained = '\n'.join(pubSubMap.explain(fleet, 'makeWidget_fakeAcct'))
    assert 'via parse:Gizmo (server/models.py:' in explained


# ---- sibling notes and hostile names ------------------------------------------------

def test_missingSiblingNoteIsWrittenAndRemoved(tmp_path):
    roots = _fleet(tmp_path)
    shop, services = roots['fakeShop'], roots['fakeShop'] / 'server' / 'services'
    (services / 'keep.md').write_text('not a note', encoding='utf-8')
    (services / 'other.notFound.txt').write_text('not a note either', encoding='utf-8')
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    before = _files(shop)
    roots['fakeAcct'].rename(tmp_path / 'fake-acct-away')
    assert pubSubMap.main([], shop) == 0, 'check mode: the snapshot still matches'
    note = services / ('acctService' + NOTE)
    text = note.read_text(encoding='utf-8')
    assert text.startswith('# acctService (fakeAcct): local checkout not found\n')
    assert '`server/services/acctService.py` (variable `acctService`)' in text
    assert '`../fake-acct`' in text and 'Reason: no checkout at' in text
    assert 'handlers here): `makeWidget_fakeAcct`' in text, 'the topic this repo consumes from it'
    assert 'test_mapIsFreshForSiblings[fakeAcct]' in text and '--repo fakeAcct=<path>' in text
    assert 'servicePortCheck' in text and 'devPubSub' in text, 'the folder itself is missing'
    assert '- Clone the `fake-acct` repository (its remote is not recorded here) into `../fake-acct`' in text
    after = _files(shop)
    assert {k for k in after if before.get(k) != after[k]} == {'server/services/acctService' + NOTE}
    assert set(before) - set(after) == set(), 'nothing else is touched'
    fleet = pubSubMap.buildMap(shop)
    assert pubSubMap.siblingNotes(shop, fleet)['acctService' + NOTE] == text, 'deterministic'
    assert pubSubMap.syncSiblingNotes(shop, fleet) == [], 'an unchanged note is not rewritten'
    (tmp_path / 'fake-acct-away').rename(roots['fakeAcct'])
    changes = pubSubMap.syncSiblingNotes(shop, pubSubMap.buildMap(shop))
    assert changes == ['removed server/services/acctService.notFound.md (the sibling is usable again)']
    assert _files(shop) == before, 'the note is gone and everything else is as it was'


def test_noteForAPresentFolderStatesOnlyWhatHolds(tmp_path):
    roots = _fleet(tmp_path)
    shop = roots['fakeShop']
    _git(roots['fakeAcct'], 'branch', '-q', '-m', 'main', 'elsewhere')
    pubSubMap.syncSiblingNotes(shop, pubSubMap.buildMap(shop))
    text = (shop / 'server' / 'services' / ('acctService' + NOTE)).read_text(encoding='utf-8')
    assert 'has none of refs/heads/main.' in text and '- Ref tried: `refs/heads/main`.' in text, 'no "main nor main"'
    assert 'servicePortCheck' not in text and 'devPubSub' not in text, 'the folder is there'
    assert 'has none of `refs/heads/main`: fetch that branch there (or check it out)' in text
    assert 'Clone' not in text, 'the folder exists: cloning into it is not the fix'


def test_transitiveSiblingNoteIsNamedAfterItsProject(tmp_path):
    roots = _fleet(tmp_path)
    sales = roots['fakeSales']
    assert pubSubMap.writeMap(sales) == WROTE_MAP
    roots['fakeShop'].rename(tmp_path / 'fake-shop-away')
    assert pubSubMap.writeMap(sales) == [
        'wrote server/services/fakeShop.notFound.md (a sibling checkout is missing; read it)']
    text = (sales / 'server' / 'services' / ('fakeShop' + NOTE)).read_text(encoding='utf-8')
    assert text.startswith('# fakeShop (fakeShop): local checkout not found\n')
    assert 'No services file here: this repo knows `fakeShop` only through fake-acct/server/services' in text
    acct = roots['fakeAcct']
    pubSubMap.syncSiblingNotes(acct, pubSubMap.buildMap(acct))
    shopNote = (acct / 'server' / 'services' / ('shopService' + NOTE)).read_text(encoding='utf-8')
    assert 'so `test_ifAllServicesMatchConsumerProjectName.py` depends on that generator' in shopNote, \
        'one test listed: the verb agrees'
    assert 'servicePortCheck' not in text and 'devPubSub' not in text, 'only true statements'


def test_hostileServiceNamesAreNeverUsedAsPaths(tmp_path, monkeypatch):
    roots = _fleet(tmp_path / 'fleet')
    outside = tmp_path / 'outside'
    outside.mkdir()
    hostile = {'evilService': ('../../../ESCAPED', 'no-such-dir'), 'absService': (str(outside / 'ABS'), 'x'),
               'uncService': ('fakeUnc', '//127.0.0.1/noSuchShare/repo'), 'upService': ('fakeUp', '..\\..\\Windows'),
               'dotService': ('fakeDot', 'trailing.')}
    _writeRepo(roots['fakeAcct'], {f'services/{v}.py': _service(v, *names) for v, names in hostile.items()})
    _commit(roots['fakeAcct'], 'hostile')
    touched, isDir = [], Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda self, *a, **k: touched.append(str(self)) or isDir(self, *a, **k))
    assert pubSubMap.main(['--write'], roots['fakeShop']) == 0
    fleet = pubSubMap.buildMap(roots['fakeShop'])
    monkeypatch.undo()
    assert sorted(fleet.sources) == ['fakeAcct', 'fakeSales', 'fakeShop']
    assert not [t for t in touched if t.startswith('\\\\') or 'Windows' in t or 'ESCAPED' in t]
    assert not list(tmp_path.rglob('*' + NOTE)) and not list(outside.iterdir())
    errors = [p for p in fleet.problems() if p['kind'] == 'hatchError']
    assert len(errors) == 5 and all(p['projects'] == ['fakeAcct'] and 'not a plain name' in p['message']
                                    for p in errors)


def test_hostileSnapshotNamesAreIgnored(tmp_path):
    shop = _fleet(tmp_path)['fakeShop']
    assert pubSubMap.writeMap(shop) == WROTE_MAP
    path = shop / 'server' / MAP_JSON
    data = json.loads(path.read_text(encoding='utf-8'))
    data['repos']['../evil'] = dict(data['repos']['fakeSales'], projectName='../evil')
    data['repos']['fakeGhost'] = dict(data['repos']['fakeSales'], projectName='fakeGhost',
                                      localRepoDirName='//host/share')
    path.write_text(pubSubMap.renderJson(data), encoding='utf-8')
    fleet = pubSubMap.buildMap(shop)
    assert fleet.warnings == [
        "the snapshot has a repo named '../evil', which is not a plain name; ignored",
        "the snapshot gives fakeGhost the localRepoDirName '//host/share', which is not a plain folder "
        'name; ignored']
    assert '../evil' not in fleet.sources and fleet.sources['fakeGhost'].root is None
    assert fleet.retired == ['fakeGhost'], 'and no repo declares it'


def test_siblingNotesOnlyLandInTheServicesFolder(tmp_path, monkeypatch):
    shop = _fleet(tmp_path)['fakeShop']
    names = ['..' + os.sep + 'escaped' + NOTE, str(tmp_path / ('abs' + NOTE)), '//host/share/x' + NOTE]
    monkeypatch.setattr(pubSubMap, 'siblingNotes', lambda root, fleet: {n: 'x\n' for n in names})
    changes = pubSubMap.syncSiblingNotes(shop, pubSubMap.buildMap(shop))
    assert len(changes) == 3 and all(c.startswith('skipped a sibling note named') for c in changes)
    assert not list(tmp_path.rglob('*' + NOTE))


def test_handPublishSitesAreAttributedAtAnyDepth():
    files = _publisherFiles('''
        def makeWidget(self, publishToProdPubSub, publishToPubSubMockDb=False):
            return {'a': 1}
        ''', extra={'closeOut.py': _src('''
            from pubSub import pubSub
            from actions.WidgetActions import WidgetActions


            def republish(doc):
                target = WidgetActions.makeWidget
                pubSub.publishMessage(doc, topicName=f'{target.__name__}_fakeAcct')


            def republishLater(doc):
                target = WidgetActions.makeWidget

                def updateFunction(session):
                    pubSub.publishMessage(doc, topicName=f'{target.__name__}_fakeAcct')
                return updateFunction


            def stray(doc):
                pubSub.publishMessage(doc, topicName='literalTopic_fakeAcct')


            def nested(doc):
                def updateFunction(session):
                    pubSub.publishMessage(doc, topicName='hiddenTopic_fakeAcct')
                return updateFunction


            class Handler:
                def run(self, doc):
                    return lambda: pubSub.publishMessage(doc, topicName='lambdaTopic_fakeAcct')
            ''')})
    section = pubSubMapSection.buildSection(files)
    assert section.data['publishers']['makeWidget']['extraPublishSites'] == [
        'closeOut.py::republish', 'closeOut.py::republishLater']
    assert [(s['anchor'], s['topic']) for s in section.data['unattributedPublishSites']] == [
        ('closeOut.py::Handler.run', "'lambdaTopic_fakeAcct'"), ('closeOut.py::nested', "'hiddenTopic_fakeAcct'"),
        ('closeOut.py::stray', "'literalTopic_fakeAcct'")]


# ---- git access (twin of sharedFileCheck.py's) -----------------------------------

def test_everyGitCallIsPinnedAndIgnoresRedirectVars(tmp_path, monkeypatch):
    roots = _fleet(tmp_path)
    calls, run = [], subprocess.run

    def spy(args, **kwargs):
        if args[0] == 'git':
            calls.append((args, kwargs))
        return run(args, **kwargs)
    monkeypatch.setenv('GIT_DIR', str(roots['fakeShop'] / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(roots['fakeShop']))
    monkeypatch.setattr(subprocess, 'run', spy)
    for worktree in (False, True):
        fleet = pubSubMap.buildMap(roots['fakeSales'], worktreeSiblings=worktree)
        assert all(src.section is not None for src in fleet.sources.values()), pubSubMap.describeSources(fleet)
    monkeypatch.undo()
    assert calls and all(args[3:7] == ['-c', 'core.fsmonitor=false', '-c', 'core.autocrlf=false']
                         and kwargs['timeout'] == pubSubMapScan.GIT_TIMEOUT for args, kwargs in calls)
    for _, kwargs in calls:
        env = kwargs['env']
        assert not set(env) & set(pubSubMapScan.GIT_REDIRECT_VARS)
        assert (env['GIT_OPTIONAL_LOCKS'], env['GIT_TERMINAL_PROMPT']) == ('0', '0')


def test_aFolderInsideAnotherCheckoutIsNotASibling(tmp_path):
    roots = _fleet(tmp_path)
    inner = roots['fakeAcct'] / 'server'
    assert pubSubMapScan.isRepoRoot(roots['fakeAcct']) and not pubSubMapScan.isRepoRoot(inner)
    fleet = pubSubMap.buildMap(roots['fakeSales'], {'fakeShop': (inner, None)})
    assert fleet.sources['fakeShop'].reason == f'{inner} is not the top of a git checkout'


def test_readGitRefIsNewlineSafeAndReadsRegularFilesOnly(tmp_path):
    root = tmp_path / 'r'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')

    def blob(text):
        return _git(root, 'hash-object', '-w', '--stdin', stdin=text).strip()
    a, b, odd = blob('A = 1\n'), blob('B = 2\n'), blob('E = 3\n')
    other = _git(root, 'mktree', stdin=f'100644 blob {b}\tb.py\n').strip()
    entries = [('100644', a, 'a.py'), ('100644', b, 'b.py'), ('100644', odd, f'0\n{other}:b.py'),
               ('120000', a, 'link.py')]
    server = _git(root, 'mktree', '-z', stdin=''.join(f'{m} blob {h}\t{n}\0' for m, h, n in entries)).strip()
    top = _git(root, 'mktree', stdin=f'040000 tree {server}\tserver\n').strip()
    _git(root, 'update-ref', 'refs/heads/main', _git(root, 'commit-tree', top, '-m', 'odd names').strip())
    assert pubSubMapScan.readGitRef(root, 'refs/heads/main') == {
        'a.py': 'A = 1\n', 'b.py': 'B = 2\n', f'0\n{other}:b.py': 'E = 3\n'}


@pytest.mark.parametrize('source, name', [
    ("class Project:\n    def __init__(self):\n        self.projectName = 'shop'\n", 'shop'),
    ("class Project:\n    projectName = 'flat'\n    def f(self):\n        self.projectName = 'no'\n", 'flat'),
    ("class Project:\n    def __init__(self):\n        if x:\n            self.projectName = 'first'\n"
     "        self.projectName = 'second'\n", 'first'),
    ("try:\n    class Project:\n        projectName = 'inTry'\nfinally:\n    pass\n", 'inTry'),
    ("def make():\n    class Project:\n        projectName = 'inDef'\n", None)])
def test_projectNameIsReadLikeSharedFileCheck(source, name):
    assert pubSubMapScan.readProjectName(source) == name


# ---- the tool sources ----------------------------------------------------------

# Twin of BACKSLASH and test_pep701CheckFindsOnlyNewSyntax in test_sharedFiles.py
# (copied verbatim, cases included); keep in step.
BACKSLASH = chr(92)


def pep701Problems(source: str) -> list[str]:
    """f-string syntax only Python 3.12+ parses (PEP 701), which
    ast.parse(feature_version=(3, 10)) does not reject on a 3.12 interpreter:
    a replacement field holding the outer quote, a backslash or a comment.
    [] before 3.12, where such source does not even tokenize."""
    # Twin of pep701Problems in test_sharedFiles.py; keep in step.
    if not hasattr(tokenize, 'FSTRING_START'):
        return []
    problems: list[str] = []
    frames: list[list] = []  # [quote, brace depth] per open f-string
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.FSTRING_END:
            enclosing = []  # the same quote as its start, checked there
        elif token.type == tokenize.FSTRING_MIDDLE:
            enclosing = frames[:-1]  # this f-string's own text
        else:
            enclosing = frames if frames and frames[-1][1] > 0 else []
        line = token.start[0]
        if any(quote in token.string for quote, _ in enclosing):
            problems.append(f'line {line}: the outer quote inside a replacement field')
        if enclosing and BACKSLASH in token.string:
            problems.append(f'line {line}: a backslash inside a replacement field')
        if enclosing and token.type == tokenize.COMMENT:
            problems.append(f'line {line}: a comment inside a replacement field')
        if token.type == tokenize.FSTRING_START:
            frames.append([token.string.lstrip('rRbBfFuU'), 0])
        elif token.type == tokenize.FSTRING_END:
            frames.pop()
        elif frames and token.type == tokenize.OP and token.string in ('{', '}'):
            frames[-1][1] += 1 if token.string == '{' else -1
    return problems


@pytest.mark.skipif(not hasattr(tokenize, 'FSTRING_START'),
                    reason='PEP 701 syntax only tokenizes on Python 3.12+')
@pytest.mark.parametrize('source, problems', [
    ("x = f'{'a'}'\n", 1),
    ('x = f"{f"{1}"}"\n', 1),
    ("x = f'{\"" + BACKSLASH + "n\"}'\n", 1),
    ("x = f'''{1 # note\n}'''\n", 1),
    ("x = f'''{'a'}'''\n", 0),
    ("x = f\"{f'{1}'}\"\n", 0),
    ("x = f'{\"a\"}' + '" + BACKSLASH + "n'\n", 0),
])
def test_pep701CheckFindsOnlyNewSyntax(source, problems):
    assert len(pep701Problems(source)) == problems


def test_toolSourcesParseAsPython310():
    for name in TOOL_FILES:
        source = (Path(__file__).resolve().parent / name).read_text(encoding='utf-8')
        ast.parse(source, filename=name, feature_version=(3, 10))
        assert pep701Problems(source) == [], name
        assert '\ufeff' not in source, f'{name}: write a BOM as an escape'

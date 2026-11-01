"""Keeps every tool's answer down to what its chat card shows or the AI reads.

A tool's result goes to the AI as its tool message and stays in the turn's
steps, which go back to the AI as history on every later turn: a field that
neither the chat card nor the AI needs costs tokens on every turn. The
route gate (test_unused_response_fields.py) covers routes only, so this one
covers the tools (every @mcp_tool member of ToolRequests). Pure Python: no
database, no node; it always runs.

How a field counts as used, per tool and per leaf path of its responseModel
(walked with responseModelCheck.models_in through lists and Optional, keys
as serialized; `products[].name`):
  - card-used: the tool has a chat card (chatCards.CARDS) and changing that
    one leaf in a full sample of the model (str + 'x', number + 1, bool
    flipped, another Literal member, value <-> None) changes the card
    (chatCards._validCard), turns it None or makes its builder raise. That
    follows `{**row}` copies and conditions (_withFormIf) the way the real
    card does;
  - AI-needed: its key is in the AI_READS of the model declaring the field
    (`AI_READS: ClassVar[dict[str, str]]`, key: why the AI needs it; read
    with ResponseModel.aiReads()). Every id the AI passes back to a tool
    (productId, orderId, returnId, a photo's _id) is AI-needed, and so is a
    nested model's own key (`items`) when the AI reads a field inside it;
  - route-shared: a route sends it too: the field's model is reachable
    from an ApiRequests route's responseModel, or a model reachable from
    one has the field, declared or inherited (ShopStoreResponse inherits
    StoreResponse's _id). The route gate owns it, so this gate never asks
    to trim it, but it passes only when it is listed in ROUTE_SHARED (the
    declaring class and key) with why the AI does not need it; aiCopy still
    hides it from the AI unless it is in AI_READS.
The full sample sets every optional field, gives every list one element and
every leaf a distinct, type-valid value, and is validated against the model
under the response check.

Checks:
  (a) every leaf path of every tool is card-used, AI-needed or a listed
      route-shared field; ROUTE_SHARED has no stale entry (one no tool's
      route-shared, unmarked path matches); and an AI-needed leaf is
      reachable by the AI copy: each nested key on its path is AI-needed
      too (or its model declares no AI_READS);
  (b) each AI_READS key is a field its class declares itself, with a
      non-empty reason of at most AI_READS_REASON_MAX characters;
  (c) AI_READS sits only on models reachable from a tool's responseModel
      (or a base of one);
  (d) every CARDS key is a tool, chatCards never dumps a whole result, and
      no tool model declares `error` or `warnings` (the chat adds those
      beside a tool's own keys, and aiCopy always keeps them);
  (e) the counts are pinned: the tools, and the models declaring AI_READS.
The walk and the sample are toolReading's: a field holding a dict of
models, or two or more models, fails loudly there (the sample would cover
only one), until it is taught to sample each one; no tool has one today.
The self-tests drive the same functions on small fixtures.
"""
import inspect
import re
import typing
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, NamedTuple
from unittest import mock

import pytest
from pydantic import Field

import chatCards
from objects import (AI_ALWAYS_KEPT, ResponseModel, StorePolicyResponse,
                     StoreResponse, validate_route_response)
from responseModelCheck import _where
from toolReading import (Leaf, changedValues, fieldAt, leaves, pathText,
                         reachable, routeModels, sample, tools, valueAt,
                         withValue)
from toolReading import key as fieldKey

SERVER_DIR = Path(__file__).resolve().parent
TOOL_COUNT = 42
AI_READS_MODEL_COUNT = 41
AI_READS_REASON_MAX = 120

# (the class declaring the field, its key) -> why a route sends it while the
# AI does not need it. A route-shared field is the route gate's to trim, so
# this gate never asks to; each one is listed here, by hand, with its
# reason. aiCopy hides it from the AI all the same.
ROUTE_SHARED: dict[tuple[type, str], str] = {
    (StoreResponse, '_id'):
        'the shop sends it (ShopStoreResponse); the AI names a store by '
        'its link, never its _id',
    (StorePolicyResponse, 'updatedAt'):
        'the admin policy screen shows it (the getMyStorePolicy route); '
        'the AI never needs when the policy was saved',
}


# ---- Where a class is declared ----

def declaringClass(model: type, key: str) -> type:
    """The class of `model`'s MRO that declares the field sent as `key`."""
    name = next(n for n, f in model.model_fields.items()
                if fieldKey(f, n) == key)
    for cls in model.__mro__:
        if name in inspect.get_annotations(cls):
            return cls
    return model


def ownKeys(cls: type) -> set:
    """The serialized keys of the fields `cls` declares itself."""
    own = inspect.get_annotations(cls)
    return {fieldKey(f, n) for n, f in cls.model_fields.items() if n in own}


@cache
def whereOf(cls: type) -> str:
    """`cls`'s file:line, relative to server/. objects/__init__.py stamps
    every re-exported class's __module__ 'objects', so inspect cannot find
    an objects class: its module file is searched for its def."""
    pattern = re.compile(rf'^class {re.escape(cls.__name__)}\b', re.M)
    for file in sorted((SERVER_DIR / 'objects').glob('*.py')):
        found = pattern.search(file.read_text(encoding='utf-8'))
        if found:
            line = found.string.count('\n', 0, found.start()) + 1
            return f'objects/{file.name}:{line}'
    file, _, line = _where(cls).rpartition(':')
    try:
        return f'{Path(file).resolve().relative_to(SERVER_DIR).as_posix()}:{line}'
    except ValueError:
        return _where(cls)


def toolModels(tools: dict) -> set:
    found: set = set()
    for tool in tools.values():
        reachable(tool.responseModel, found)
    return found


# ---- Card-used paths ----

class FakeContext:
    storeId = 'store-sample'

    def currency(self) -> str:
        return 'PHP'


_RAISED = object()


def cardOf(makeCard, tool: str, result: dict, validate):
    try:
        card = makeCard(result, FakeContext())
    except (KeyError, TypeError):
        return _RAISED
    if card is None:
        return None
    return validate(card, tool)


def cardUsedPaths(tool: str, model: type, makeCard, validate=None) -> set:
    """The leaf paths of `model` whose change changes `tool`'s card."""
    validate = validate or chatCards._validCard
    with mock.patch.object(chatCards, 'newFormId', lambda: 'form-sample'):
        base = sample(model)
        validate_route_response(model, base)
        baseCard = cardOf(makeCard, tool, base, validate)
        if baseCard in (None, _RAISED):
            raise AssertionError(f'{tool}: the full sample makes no card')
        used = set()
        for leaf in leaves(model):
            field = fieldAt(model, leaf.path)
            value = valueAt(base, leaf.path)
            for change in changedValues(field, value):
                changed = withValue(base, leaf.path, change)
                validate_route_response(model, changed)
                if cardOf(makeCard, tool, changed, validate) != baseCard:
                    used.add(leaf.path)
                    break
    return used


# ---- The checks ----

def isRouteShared(leaf: Leaf, routeModels: frozenset) -> bool:
    """Whether a route sends this field too: the leaf's model is a route
    model, or any route model has the field, declared or inherited (a route
    model subclassing the class that declares it, ShopStoreResponse sending
    StoreResponse's _id, say). Trimming it would trim that route."""
    if leaf.model in routeModels:
        return True
    declaring = declaringClass(leaf.model, leaf.key)
    return any(issubclass(model, declaring)
               and leaf.key in {fieldKey(f, n) for n, f in
                                model.model_fields.items()}
               for model in routeModels)


class FieldUse(NamedTuple):
    tool: str
    leaf: Leaf
    use: str             # 'card', 'ai', 'routeShared' or 'unmarked'


def fieldUses(tools: dict, cards: dict, routeModels: frozenset,
              validate=None) -> list:
    uses = []
    for name, tool in sorted(tools.items()):
        model = tool.responseModel
        used = (cardUsedPaths(name, model, cards[name], validate)
                if name in cards else set())
        for leaf in leaves(model):
            if leaf.path in used:
                use = 'card'
            elif leaf.key in (leaf.model.aiReads() or {}):
                use = 'ai'
            elif isRouteShared(leaf, routeModels):
                use = 'routeShared'
            else:
                use = 'unmarked'
            uses.append(FieldUse(name, leaf, use))
    return uses


def unusedProblems(uses: list, routeShared: dict) -> list:
    """Check (a): one line per unmarked path, per route-shared path not in
    `routeShared` (ROUTE_SHARED), per stale `routeShared` entry, and per
    AI-needed leaf the AI copy cannot reach (a nested key above it
    dropped)."""
    problems, listed = [], set()
    for use in uses:
        leaf = use.leaf
        declaring = declaringClass(leaf.model, leaf.key)
        if use.use == 'routeShared':
            if (declaring, leaf.key) in routeShared:
                listed.add((declaring, leaf.key))
            else:
                problems.append(
                    f'{use.tool}: {pathText(leaf.path)} '
                    f'({declaring.__name__}, {whereOf(declaring)}) is sent '
                    'by a route too: add it to AI_READS with why the AI '
                    f'needs it, or list ({declaring.__name__}, '
                    f"'{leaf.key}') in ROUTE_SHARED with why it does not")
        if use.use == 'unmarked':
            problems.append(
                f'{use.tool}: {pathText(leaf.path)} ({declaring.__name__}, '
                f'{whereOf(declaring)}): trim it, or add it to AI_READS with '
                'why the AI needs it')
        if use.use == 'ai':
            for model, key in leaf.containers:
                if not model.aiKeeps(key):
                    holder = declaringClass(model, key)
                    problems.append(
                        f'{use.tool}: {pathText(leaf.path)} is in AI_READS '
                        f'but {key} ({holder.__name__}, {whereOf(holder)}) '
                        'is not, so the AI never sees it: add it')
    for cls, key in sorted(set(routeShared) - listed,
                           key=lambda entry: (entry[0].__name__, entry[1])):
        problems.append(
            f'ROUTE_SHARED: ({cls.__name__}, {key!r}) matches no tool path '
            'that a route sends and the AI and the cards do not read: drop '
            'it')
    return sorted(set(problems))


def _ownAiReads(cls: type):
    return cls.__dict__.get('AI_READS')


def aiReadsProblems(classes) -> list:
    """Check (b) on the classes declaring AI_READS."""
    problems = []
    for cls in classes:
        reads = _ownAiReads(cls)
        if not isinstance(reads, dict):
            problems.append(f'{cls.__name__}.AI_READS must be a dict of '
                            'key: reason')
            continue
        own = ownKeys(cls)
        for key, reason in reads.items():
            if key not in own:
                problems.append(
                    f'{cls.__name__}.AI_READS: {key!r} is no field '
                    f'{cls.__name__} declares itself ({whereOf(cls)}): drop '
                    'it, or mark it on the class declaring it')
            if (not isinstance(reason, str) or not reason.strip()
                    or len(reason) > AI_READS_REASON_MAX):
                problems.append(
                    f'{cls.__name__}.AI_READS[{key!r}]: the reason is 1 to '
                    f'{AI_READS_REASON_MAX} characters of why the AI needs '
                    'it')
    return problems


def placementProblems(classes, tools: dict) -> list:
    """Check (c): AI_READS only on a tool's models or their bases."""
    allowed = set()
    for model in toolModels(tools):
        allowed.update(cls for cls in model.__mro__
                       if isinstance(cls, type) and issubclass(
                           cls, ResponseModel))
    return [f'{cls.__name__} ({whereOf(cls)}) declares AI_READS but no '
            'tool returns it: drop it' for cls in classes
            if cls not in allowed]


def _subclasses(cls: type) -> set:
    found = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _subclasses(sub)
    return found


def realAiReadsClasses() -> list:
    """The objects classes declaring AI_READS themselves."""
    return sorted((cls for cls in _subclasses(ResponseModel)
                   if cls.__module__.split('.')[0] == 'objects'
                   and _ownAiReads(cls) is not None),
                  key=lambda cls: cls.__name__)


def cardsProblems(tools: dict, cards: dict, cardsSource: str) -> list:
    """Check (d)."""
    problems = [f'chatCards.CARDS: {name} is no tool'
                for name in sorted(set(cards) - set(tools))]
    if 'json.dumps(result' in cardsSource.replace(' ', ''):
        problems.append('chatCards dumps a whole result: build a card from '
                        'named keys only')
    for model in toolModels(tools):
        for key in sorted(set(AI_ALWAYS_KEPT) & ownKeys(model)):
            problems.append(f'{model.__name__} declares {key!r}: the chat '
                            'adds it beside a tool\'s keys; rename it')
    return problems


@cache
def realUses() -> tuple:
    return tuple(fieldUses(tools(), chatCards.CARDS, routeModels()))


# ---- The real tests ----

def test_everyToolField_isCardUsed_orAiNeeded():
    problems = unusedProblems(realUses(), ROUTE_SHARED)
    assert not problems, '\n'.join(problems)


def test_aiReads_namesRealFields_withReasons():
    assert not aiReadsProblems(realAiReadsClasses())


def test_aiReads_onlyOnToolModels():
    assert not placementProblems(realAiReadsClasses(), tools())


def test_cards_areToolsAndNamedKeys():
    source = (SERVER_DIR / 'chatCards.py').read_text(encoding='utf-8')
    assert not cardsProblems(tools(), chatCards.CARDS, source)


def test_theRealCounts_arePinned():
    assert len(tools()) == TOOL_COUNT, (
        f'{len(tools())} tools, TOOL_COUNT says {TOOL_COUNT}: after adding '
        'or removing a tool on purpose, set TOOL_COUNT in '
        'test_unused_tool_fields.py to the new count')
    assert len(realAiReadsClasses()) == AI_READS_MODEL_COUNT, (
        f'{len(realAiReadsClasses())} classes declare AI_READS, '
        f'AI_READS_MODEL_COUNT says {AI_READS_MODEL_COUNT}: after adding or '
        'removing an AI_READS on purpose, set AI_READS_MODEL_COUNT in '
        'test_unused_tool_fields.py to the new count')


def test_everyCardedTool_hasAFullSampleCard():
    """The sample makes a card for every carded tool (else nothing would
    count as card-used), and every leaf has a type-valid change."""
    for name, tool in tools().items():
        model = tool.responseModel
        base = sample(model)
        validate_route_response(model, base)
        for leaf in leaves(model):
            assert changedValues(fieldAt(model, leaf.path),
                                 valueAt(base, leaf.path)), (name, leaf.path)
        if name in chatCards.CARDS:
            assert cardUsedPaths(name, model, chatCards.CARDS[name])


# ---- Self-tests on fixtures ----

class _FxPart(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {
        'label': 'the AI names the part'}
    label: str
    note: str | None = None


class _FxRow(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {
        '_id': 'the AI passes it back to fxUpdate',
        'parts': 'the AI reads the part labels'}
    id: str = Field(..., alias='_id')
    shown: str
    status: typing.Literal['open', 'done']
    unused: int
    parts: list[_FxPart]


class _FxRows(ResponseModel):
    rows: list[_FxRow]


def _fxCard(result: dict, context) -> dict:
    rows = result['rows']
    return {'kind': 'fx', 'shown': [r['shown'] for r in rows],
            'answerable': any(r['status'] == 'done' for r in rows),
            'notes': [p['note'] for r in rows for p in r['parts']]}


def _identity(card: dict, tool: str) -> dict:
    return card


def _fxUses(answer=_FxRows, routeModels=frozenset()) -> list:
    tools = {'fxList': SimpleNamespace(responseModel=answer)}
    return fieldUses(tools, {'fxList': _fxCard}, routeModels, _identity)


def test_fx_aUsedFieldPasses_andAnUnusedOneFails():
    uses = {pathText(u.leaf.path): u.use for u in _fxUses()}

    assert uses == {'rows[]._id': 'ai', 'rows[].shown': 'card',
                    'rows[].status': 'card', 'rows[].unused': 'unmarked',
                    'rows[].parts[].label': 'ai',
                    'rows[].parts[].note': 'card'}
    [problem] = unusedProblems(_fxUses(), {})
    assert problem.startswith('fxList: rows[].unused (_FxRow, '
                              'test_unused_tool_fields.py:')
    assert problem.endswith('trim it, or add it to AI_READS with why the AI '
                            'needs it')


def test_fx_aRouteSharedField_passesOnlyWhenListed():
    uses = _fxUses(routeModels=frozenset({_FxRow}))

    assert {pathText(u.leaf.path): u.use for u in uses}[
        'rows[].unused'] == 'routeShared'
    assert unusedProblems(uses, {(_FxRow, 'unused'): 'a route shows it'}) \
        == []
    [problem] = unusedProblems(uses, {})
    assert problem.startswith('fxList: rows[].unused (_FxRow, ')
    assert problem.endswith("is sent by a route too: add it to AI_READS with "
                            "why the AI needs it, or list (_FxRow, 'unused') "
                            "in ROUTE_SHARED with why it does not")


def test_fx_aStaleRouteSharedEntry_fails():
    """An entry whose field no route sends, or that the AI or a card now
    reads, is stale."""
    listed = {(_FxRow, 'unused'): 'a route shows it',
              (_FxRow, 'shown'): 'the card reads it',
              (_FxPart, 'gone'): 'renamed'}

    problems = unusedProblems(_fxUses(routeModels=frozenset({_FxRow})),
                              listed)

    assert problems == [
        "ROUTE_SHARED: (_FxPart, 'gone') matches no tool path that a route "
        'sends and the AI and the cards do not read: drop it',
        "ROUTE_SHARED: (_FxRow, 'shown') matches no tool path that a route "
        'sends and the AI and the cards do not read: drop it']


def test_fx_aFieldARouteModelInherits_isRouteShared():
    """A route model that subclasses the declaring class sends the field
    too, though neither the tool's model nor the declaring class is a
    route model."""
    class _FxRouteRow(_FxRow):
        extra: str

    uses = _fxUses(routeModels=frozenset({_FxRouteRow}))

    assert {pathText(u.leaf.path): u.use for u in uses}[
        'rows[].unused'] == 'routeShared'
    assert unusedProblems(uses, {(_FxRow, 'unused'): 'a route shows it'}) \
        == []
    # A route model that is no subclass shares nothing.
    class _FxOther(ResponseModel):
        unused: int

    assert {pathText(u.leaf.path): u.use for u in _fxUses(
        routeModels=frozenset({_FxOther}))}['rows[].unused'] == 'unmarked'


def test_fx_aiReads_mergeDownTheMro_aSubclassAddingNoKeyHidesNothing():
    class _FxHidden(_FxRow):
        AI_READS: ClassVar[dict[str, str]] = {}

    class _FxHiddenAnswer(ResponseModel):
        rows: list[_FxHidden]

    with mock.patch.dict(_FxRow.__dict__['AI_READS'],
                         {'unused': 'counted'}):
        # _FxHidden adds no key: AI_READS merge, so parts is still read.
        assert unusedProblems(_fxUses(_FxHiddenAnswer), {}) == []


def test_fx_anAiReadLeaf_underADroppedKey_fails():
    with mock.patch.object(_FxRow, 'AI_READS', {'_id': 'passed back',
                                                'unused': 'counted'}):
        problems = unusedProblems(_fxUses(), {})
    assert problems == [
        'fxList: rows[].parts[].label is in AI_READS but parts (_FxRow, '
        f'{whereOf(_FxRow)}) is not, so the AI never sees it: add it']


def test_fx_aFieldOfSeveralModels_orADictOfModels_failsLoudly():
    """The sample covers one model per field: a field that could hold
    another one is refused, never half-checked."""
    class _FxDict(ResponseModel):
        rows: dict[str, _FxPart]

    class _FxEither(ResponseModel):
        row: _FxPart | _FxRow

    class _FxEitherList(ResponseModel):
        rows: list[_FxPart | _FxRow] | None = None

    for model, shape in ((_FxDict, 'a dict of models'),
                         (_FxEither, 'holds 2 models'),
                         (_FxEitherList, 'holds 2 models')):
        for walk in (sample, leaves, lambda m: fieldAt(m, (next(iter(
                m.model_fields)), 0, 'label'))):
            with pytest.raises(TypeError, match=shape):
                walk(model)
    # The shapes it walks: alone, nullable, in a list, a nullable list.
    class _FxShapes(ResponseModel):
        a: _FxPart
        b: _FxPart | None = None
        c: list[_FxPart]
        d: list[_FxPart] | None = None

    assert [pathText(leaf.path) for leaf in leaves(_FxShapes)] == [
        'a.label', 'a.note', 'b.label', 'b.note', 'c[].label', 'c[].note',
        'd[].label', 'd[].note']
    validate_route_response(_FxShapes, sample(_FxShapes))


def test_fx_aStaleOrBadAiReadsKey_fails():
    class _FxStale(ResponseModel):
        AI_READS: ClassVar[dict[str, str]] = {'gone': 'it was renamed',
                                              'kept': '',
                                              'inherited': 'x' * 121}
        kept: str

    class _FxChild(_FxStale):
        AI_READS: ClassVar[dict[str, str]] = {'kept': 'marked on the child'}
        extra: str

    problems = aiReadsProblems([_FxStale, _FxChild])

    assert [p.split(' (')[0].split(':')[0] for p in problems] == [
        "_FxStale.AI_READS", "_FxStale.AI_READS['kept']",
        "_FxStale.AI_READS", "_FxStale.AI_READS['inherited']",
        "_FxChild.AI_READS"]
    assert aiReadsProblems([_FxPart, _FxRow]) == []


def test_fx_aiReads_onAModelNoToolReturns_fails():
    tools = {'fxList': SimpleNamespace(responseModel=_FxRows)}

    class _FxLonely(ResponseModel):
        AI_READS: ClassVar[dict[str, str]] = {'x': 'nobody returns it'}
        x: str

    assert placementProblems([_FxRow, _FxPart], tools) == []
    [problem] = placementProblems([_FxLonely], tools)
    assert problem.startswith('_FxLonely (')


def test_fx_cardsChecks():
    tools = {'fxList': SimpleNamespace(responseModel=_FxRows)}

    class _FxError(ResponseModel):
        error: str
        warnings: list[str]

    assert cardsProblems(tools, {'fxList': _fxCard}, 'x = 1') == []
    assert cardsProblems(tools, {'fxGone': _fxCard}, 'json.dumps( result)') \
        == ['chatCards.CARDS: fxGone is no tool',
            'chatCards dumps a whole result: build a card from named keys '
            'only']
    errorTools = {'fxBad': SimpleNamespace(responseModel=_FxError)}
    assert len(cardsProblems(errorTools, {}, '')) == 2


def test_fx_aiReads_isNoField_andMerges():
    assert 'AI_READS' not in _FxRow.model_fields
    assert 'AI_READS' not in str(_FxRow.model_json_schema())
    assert _FxRows.aiReads() is None

    class _FxChild(_FxRow):
        AI_READS: ClassVar[dict[str, str]] = {'extra': 'read too'}
        extra: str

    assert set(_FxChild.aiReads()) == {'_id', 'parts', 'extra'}

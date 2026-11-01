"""The AI gets only what it reads: objects.aiCopy and where it is used.

A tool's result goes to the AI cut to its models' AI_READS (aiCopy), while
the chat cards are built from the full result. Covers:
  - per tool, on a full sample (toolReading.sample): the AI copy
    keeps a leaf exactly when every key on its path is AI-kept (in its
    model's AI_READS, or the model declares none), and fits aiCopyModel;
  - nested lists, a dict of models, a field holding two models (cut by
    member, as aiCopyModel declares it), unmarked models, unknown keys,
    `error` and `warnings` passing through, and the input left alone;
  - chatModel.aiContent (the chat's tool message) and mcpRuntime.callTool
    (the MCP answer, with its published output schema), callToolFull
    keeping the full result for the chat's cards;
  - the docstring guard: no tool description names a field its AI copy
    leaves out (the AI would look for what it never gets).
The chat's stored steps and cards are covered in test_chat_components.py
("the AI copy"), the warnings read from the full result in
test_toolWarnings.py.
"""
import inspect
import json
import re
from typing import Annotated, ClassVar, Literal

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import chatModel
import mcpRuntime
import mcpServer
from objects import (AI_ALWAYS_KEPT, AdminProductListResponse,
                     ResponseModel, aiCopy, aiCopyModel,
                     validate_route_response)
from toolReading import leaves, pathText, reachable, sample, tools, valueAt


def _present(data, path: tuple) -> bool:
    for step in path:
        if step == 0:
            if not data:
                return False
            data = data[0]
        elif isinstance(data, dict) and step in data:
            data = data[step]
        else:
            return False
    return True


@pytest.mark.parametrize('name', sorted(tools()))
def test_eachTool_keepsExactlyWhatTheAiReads(name):
    model = tools()[name].responseModel
    full = sample(model)
    copy = aiCopy(model, full)

    for leaf in leaves(model):
        kept = (leaf.model.aiKeeps(leaf.key)
                and all(holder.aiKeeps(key)
                        for holder, key in leaf.containers))
        assert _present(copy, leaf.path) == kept, pathText(leaf.path)
        if kept:
            assert valueAt(copy, leaf.path) == valueAt(full, leaf.path)
    validate_route_response(aiCopyModel(model), copy)


def test_aToolModelWithAiReads_isCutForReal():
    """The marks are in place: a card-only field leaves the AI copy."""
    model = tools()['listMyStoreProducts'].responseModel
    [row] = aiCopy(model, sample(model))['products']
    assert 'photoUrl' not in row and 'hasPhoto' in row


# ---- Fixtures ----

class _FxLeaf(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {'kept': 'read'}
    kept: str
    cardOnly: str


class _FxUnmarked(ResponseModel):
    a: str
    b: list[_FxLeaf]


class _FxRoot(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {'rows': 'read', 'one': 'read',
                                          'unmarked': 'read'}
    rows: list[_FxLeaf]
    one: _FxLeaf | None
    unmarked: _FxUnmarked
    hidden: str
    id: str = Field(..., alias='_id')


FX = {'rows': [{'kept': 'k1', 'cardOnly': 'c1'},
               {'kept': 'k2', 'cardOnly': 'c2'}],
      'one': {'kept': 'k3', 'cardOnly': 'c3'},
      'unmarked': {'a': 'x', 'b': [{'kept': 'k4', 'cardOnly': 'c4'}]},
      'hidden': 'h', '_id': 'id1'}


def test_fx_nestedListsAndModels_areCut_unmarkedModelsKeptWhole():
    before = json.dumps(FX)

    copy = aiCopy(_FxRoot, FX)

    assert copy == {'rows': [{'kept': 'k1'}, {'kept': 'k2'}],
                    'one': {'kept': 'k3'},
                    'unmarked': {'a': 'x', 'b': [{'kept': 'k4'}]}}
    assert json.dumps(FX) == before
    assert aiCopy(_FxRoot, {**FX, 'one': None})['one'] is None
    assert aiCopy(_FxRoot, {'rows': []}) == {'rows': []}


def test_fx_errorAndWarnings_passThrough_unknownKeysDoNot():
    warnings = [{'code': 'noPhoto', 'count': 1, 'ids': ['p1'],
                 'message': 'm'}]
    assert set(AI_ALWAYS_KEPT) == {'error', 'warnings'}
    assert aiCopy(_FxRoot, {'error': 'Not found'}) == {'error': 'Not found'}
    assert aiCopy(_FxRoot, {**FX, 'warnings': warnings,
                            'stray': 1})['warnings'] == warnings
    assert 'stray' not in aiCopy(_FxRoot, {**FX, 'stray': 1})
    # A model that declares no AI_READS keeps every key, a stray one too.
    assert aiCopy(_FxUnmarked, {'a': 'x', 'b': [], 'stray': 1}) == {
        'a': 'x', 'b': [], 'stray': 1}
    for value in (None, [1], 'text', 5):
        assert aiCopy(_FxRoot, value) == value
    assert aiCopy(None, FX) == FX


class _FxOther(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {'kind': 'read', 'size': 'read'}
    kind: Literal['other']
    size: int
    url: str


class _FxTagged(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {'kind': 'read', 'kept': 'read'}
    kind: Literal['leaf']
    kept: str
    cardOnly: str


class _FxShapes(ResponseModel):
    AI_READS: ClassVar[dict[str, str]] = {'byName': 'read', 'either': 'read',
                                          'mixed': 'read'}
    byName: dict[str, _FxLeaf]
    either: Annotated[_FxTagged | _FxOther, Field(discriminator='kind')]
    mixed: list[Annotated[_FxTagged | _FxOther,
                          Field(discriminator='kind')]]


SHAPES = {'byName': {'m': {'kept': 'k1', 'cardOnly': 'c1'},
                     'n': {'kept': 'k2', 'cardOnly': 'c2'}},
          'either': {'kind': 'other', 'size': 3, 'url': 'u1'},
          'mixed': [{'kind': 'leaf', 'kept': 'k3', 'cardOnly': 'c3'},
                    {'kind': 'other', 'size': 4, 'url': 'u2'}]}


def test_fx_aDictOfModels_andAFieldOfTwoModels_areCutByMember():
    """The same walk as projectedCopy: each dict value cut as its model,
    each union value as the member its tag names, by that member's own
    AI_READS; the copy fits aiCopyModel, which swaps each member."""
    copy = aiCopy(_FxShapes, SHAPES)

    assert copy == {'byName': {'m': {'kept': 'k1'}, 'n': {'kept': 'k2'}},
                    'either': {'kind': 'other', 'size': 3},
                    'mixed': [{'kind': 'leaf', 'kept': 'k3'},
                              {'kind': 'other', 'size': 4}]}
    validate_route_response(aiCopyModel(_FxShapes), copy)
    schema = json.dumps(aiCopyModel(_FxShapes).model_json_schema())
    assert 'cardOnly' not in schema and '"url"' not in schema


def test_fx_aiCopyModel_declaresOnlyWhatTheCopyKeeps():
    published = aiCopyModel(_FxRoot)

    assert published.__name__ == '_FxRoot'
    assert set(published.model_fields) == {'rows', 'one', 'unmarked'}
    schema = json.dumps(published.model_json_schema())
    assert 'cardOnly' not in schema and 'hidden' not in schema
    validate_route_response(published, aiCopy(_FxRoot, FX))
    assert aiCopyModel(_FxUnmarked) is not _FxUnmarked  # b's rows are cut
    assert aiCopyModel(_FxLeaf) is aiCopyModel(_FxLeaf)

    class _FxPlain(ResponseModel):
        x: str

    assert aiCopyModel(_FxPlain) is _FxPlain


# ---- Where the AI copy is used ----

def test_aiContent_isTheChatsToolMessage():
    model = AdminProductListResponse
    full = sample(model)

    content = chatModel.aiContent('listMyStoreProducts', json.dumps(full))

    assert json.loads(content) == json.loads(json.dumps(aiCopy(model, full)))
    refusal = chatModel.toolError('Create your store first')
    assert chatModel.aiContent('listMyStoreProducts', refusal) == refusal
    assert chatModel.aiContent('noSuchTool', '{"a": 1}') == '{"a": 1}'
    assert chatModel.aiContent('listMyStoreProducts', 'not json') == \
        'not json'
    assert chatModel.aiContent('listMyStoreProducts', '[1]') == '[1]'


def test_mcpCallTool_answersTheAiCopy_callToolFullTheFullResult():
    def fxTool(**kwargs):
        return FX

    fxTool.responseModel = _FxRoot

    assert mcpRuntime.callTool(fxTool) == aiCopy(_FxRoot, FX)
    assert mcpRuntime.callToolFull(fxTool) == FX

    def fxRaising(**kwargs):
        raise ValueError('Bad thing')

    fxRaising.responseModel = _FxRoot
    with pytest.raises(ToolError, match='^Bad thing$'):
        mcpRuntime.callTool(fxRaising)


def test_theMcpServers_publishTheAiCopysSchema():
    tool = mcpServer.servers['admin']._tool_manager.get_tool(
        'listMyStoreProducts')
    row = tool.output_schema['$defs']['AdminProductListRow']['properties']

    assert {'hasPhoto', 'soldCount', '_id'} <= set(row)
    assert 'photoUrl' not in row
    assert tool.fn_metadata.output_model is aiCopyModel(
        AdminProductListResponse)


# ---- The docstring guard ----

def droppedKeys(model) -> set:
    """The keys a tool's AI copy leaves out, at any depth: dropped by a
    model of its tree and kept by none."""
    dropped, kept = set(), set()
    for each in reachable(model):
        for name, field in each.model_fields.items():
            key = field.alias or name
            (kept if each.aiKeeps(key) else dropped).add(key)
    return dropped - kept


def namedIn(text: str, keys: set) -> list:
    return sorted(key for key in keys if re.search(
        r'(?<![A-Za-z0-9_])' + re.escape(key) + r'(?![A-Za-z0-9_])', text))


def test_noToolDescription_namesAFieldItsAiCopyLeavesOut():
    problems = [
        f'{name}: its description names {namedIn(inspect.getdoc(tool) or "", droppedKeys(tool.responseModel))}, '
        'which its AI copy leaves out: reword it, or add the field to '
        'AI_READS'
        for name, tool in sorted(tools().items())
        if namedIn(inspect.getdoc(tool) or '',
                   droppedKeys(tool.responseModel))]
    assert not problems, '\n'.join(problems)


def test_fx_theGuard_findsANamedDroppedField():
    assert droppedKeys(_FxRoot) == {'cardOnly', 'hidden', '_id'}
    assert namedIn('Returns each row, its cardOnly and _id.',
                   droppedKeys(_FxRoot)) == ['_id', 'cardOnly']
    assert namedIn('the cardOnlyness, a hidden_x', droppedKeys(_FxRoot)) == []

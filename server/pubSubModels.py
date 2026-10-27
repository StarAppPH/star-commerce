"""Generated Pub/Sub message models: server/pubsub_models.json and server/objects_<publisher>.py.

A publisher repo commits server/pubsub_models.json, the payload model of every
topic it publishes: the serialization JSON schema of each @pubSubDecorator
function's route_config responseModel (or its PUBSUB_PAYLOAD_MODELS entry,
for a publisher that is no route). A consumer repo renders the topics it
consumes from each publisher into server/objects_<publisherProjectName>.py:
one pydantic class per object schema, fields and types only, and a
<Fn>Message class per topic. A handler parses its message with it:

    from objects_accounting import CreateUnitMessage
    message = CreateUnitMessage.model_validate(data)

    cd server && python pubSubModels.py                  # check: exit 1 when a generated file is stale
    cd server && python pubSubModels.py --pull           # (= --write) regenerate this repo's objects_<p>.py
    cd server && python pubSubModels.py --push           # pull in every same-branch consumer checkout
    cd server && ENVIRONMENT=localdev python pubSubModels.py --write-manifest  # imports the app, like AppCreator

AppCreator.py pulls before it imports the app (the handlers import the
generated files) and writes the manifest right before the Pub/Sub map, so
the usual "change a model, run AppCreator" step keeps both sides fresh.

- The manifest is built by importing the app (collectPublishers): the map's
  AST finds the publishers (module-level ones too), the import reads their
  models, and pubSub._pubsubTopicRegistry catches a decorator the AST missed.
  Publisher modules are imported, so keep them import-safe. Before it is
  written, the module of every consumer is rendered and loaded here, so a
  shape the renderer cannot handle stops the publisher, not its consumers.
- Pull reads each sibling's manifest at the refs the map reads it at
  (pubSubMap.fleetSources: this branch's name if the sibling has it, else
  main; `--repo name=path[@ref]` and `--worktree-siblings` as in pubSubMap.py),
  as a regular file only. A sibling's manifest is untrusted input
  (checkManifest validates it; its strings reach the source only through
  ascii() literals, and the render is bounded) and a sibling's code is never
  run. An absent sibling, or one without a manifest yet, keeps its file. A
  generated file (first line GENERATED_HEADER) nothing generates any more is
  deleted. A hand-written file at a target path is never overwritten: pull
  refuses.
- A generated file holds only the renderer's shapes (generatedShapeProblems):
  only such a file counts as generated (test_file_sizes.py), and
  test_pubSubModels.py fails on a header-carrying file that is not one.
- Push writes the generated files of every consumer checkout on this branch
  that has adopted the tools (pubSubMap's --write-all gates), with this
  process's pull. It reads this repo's manifest at HEAD, so commit it first.
  Nothing is staged or committed: it prints the git commands.
- handlerProblems(section, handler) is the handler rule, on the map's view of
  a push handler: it parses its whole message once with its generated
  <Fn>Message and reads the message nowhere else (outside an
  `except ValidationError` fallback).

Pure stdlib (pydantic and the app are imported only inside the manifest
functions), Python 3.10 compatible, byte-identical in every repo.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import heapq
import importlib
import json
import keyword
import os
import re
import sys
import types
import warnings
from pathlib import Path

from pubSubMap import (_branchName, _guardTarget, _parseOverrides, _write, _writeAllSkip, commitLines, errorLine,
                       fleetSources, readLF, serverDir)
from pubSubMapContract import GENERATED_MODELS_HEADER as GENERATED_HEADER
from pubSubMapContract import isGeneratedModels
from pubSubMapScan import (MapError, isRegularBlob, lsTreeRecords, moduleNameFor, normaliseSource,
                           memoizedGit, readWorkingTree, runGit)
from pubSubMapSection import HATCH_PAYLOAD_MODELS, buildSection, flag, isSafeProjectName

MANIFEST = 'pubsub_models.json'
FORMAT_VERSION = 1
PAYLOAD_SOURCES = ('responseModel', HATCH_PAYLOAD_MODELS)
REGENERATE = 'Then run `cd server && python pubSubModels.py --pull` and commit server/objects_*.py.'
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_NAMES = re.compile(r'[A-Za-z_]\w*')
_OID = re.compile(rb'[0-9a-f]{40}|[0-9a-f]{64}')
_DEFS = '#/$defs/'
# The schema keys the manifest keeps; the rest (titles, constraints, examples,
# discriminators) are dropped, which also drops what differs between pydantic
# versions.
SCHEMA_KEYS = ('$ref', 'additionalProperties', 'allOf', 'anyOf', 'const', 'default', 'description', 'enum',
               'format', 'items', 'oneOf', 'prefixItems', 'properties', 'readOnly', 'required', 'type')
_SCHEMA_ONE_KEYS = ('items', 'additionalProperties')  # each holds one sub-schema
_SCHEMA_LIST_KEYS = ('prefixItems', 'anyOf', 'oneOf', 'allOf')  # each holds a list of them
_FORMATS = {'date-time': '_dt.datetime', 'date': '_dt.date', 'time': '_dt.time'}
_JSON_TYPES = {'string': 'str', 'integer': 'int', 'number': 'float', 'boolean': 'bool', 'null': 'None',
               'array': None, 'object': None}
_MAX_DEPTH = 64  # nesting in one schema, and inlined defs in a chain
_MAX_ANNOTATION = 10000  # characters in one annotation (a field's, an inlined def's)
_MAX_MODULE = 2000000  # characters in one rendered module
# The import lines every generated module starts with, as render() writes
# them; the shape check (generatedShapeProblems) allows exactly these imports.
_HEADER_LINES = ('from __future__ import annotations', '', 'import datetime as _dt',
                 'from typing import Any, Literal', '', 'from pydantic import BaseModel, ConfigDict, Field')
# The names an annotation or a class body of a generated module evaluates: no
# field may shadow one (pydantic reads `date: date` back as the field's own
# default), and no class may take one.
_ANNOTATION_NAMES = frozenset({'Any', 'Literal', 'Field', 'BaseModel', 'ConfigDict', '_dt', 'list', 'dict',
                               'tuple', 'str', 'int', 'float', 'bool', 'None'})
RESERVED_CLASS_NAMES = _ANNOTATION_NAMES | {'_Message', 'TOPIC_MESSAGES', 'annotations', 'bytes', 'type',
                                            'object', 'True', 'False'}
# Public attributes of pydantic v2's BaseModel (every model_* name is out
# too), and Config, which pydantic still reads from a class namespace.
BASEMODEL_ATTRIBUTES = frozenset({
    'construct', 'copy', 'dict', 'json', 'schema', 'schema_json', 'validate', 'fields', 'from_orm',
    'parse_obj', 'parse_raw', 'parse_file', 'update_forward_refs', 'Config'})


class ModelsError(MapError):
    """A hard error: a manifest or a generated file cannot be built (exit 2)."""


def messageClassName(fn: str) -> str:
    """The generated class of topic `<fn>_<publisher>`: makeWidget -> MakeWidgetMessage."""
    return fn[0].upper() + fn[1:] + 'Message'


def generatedFileName(projectName: str) -> str:
    return f'objects_{projectName}.py'


def manifestConsumers(manifest: dict) -> list:
    """Every project a manifest's topics name as a consumer, sorted."""
    return sorted({c for entry in manifest['topics'].values() for c in entry['consumers']})


def _hasHeader(path: Path) -> bool:
    """True for a file server/objects_<identifier>.py whose first line is
    GENERATED_HEADER (pubSubMapContract.isGeneratedModels). A write to it still
    refuses a symlink (pubSubMap._guardTarget)."""
    try:
        if not path.is_file():
            return False
        with open(path, 'rb') as handle:
            first = handle.readline(len(GENERATED_HEADER) + 8)
    except OSError:
        return False
    return isGeneratedModels(path.name, normaliseSource(first))


def _generatedPaths(server: Path) -> list:
    """The header-carrying server/objects_<identifier>.py files, sorted."""
    return [path for path in sorted(server.glob('objects_*.py')) if _hasHeader(path)]


def generatedModelFiles(projectRoot: Path) -> list:
    """Repo-relative POSIX paths of this repo's generated server/objects_<p>.py
    files: the header-carrying ones that hold only the renderer's shapes
    (test_file_sizes.py counts them as generated)."""
    return [f'server/{path.name}' for path in _generatedPaths(serverDir(projectRoot))
            if not generatedShapeProblems(normaliseSource(path.read_bytes()))]


# ----------------------------------------------------------------------------
# The manifest (publisher side): imports the app
# ----------------------------------------------------------------------------

class PublisherSpec:
    """One topic this repo publishes and the class of its payload."""

    def __init__(self, fn: str, topic: str, anchor: str, consumers, prodPublish: bool, model, source: str):
        self.fn, self.topic, self.anchor = fn, topic, anchor
        self.consumers, self.prodPublish = sorted(consumers), prodPublish
        self.model, self.source = model, source


def _sameFile(a, b: Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b.resolve()))
    except (OSError, TypeError):
        return False


def _importOwn(name: str, expected: Path, problems: list, label: str):
    """Import module `name` and check it is this repo's file, else record why.
    An import that fails ends the scan with the problems found so far: with
    the database down, every later import would retry its connection."""
    try:
        module = importlib.import_module(name)
    except Exception as e:  # the app's import errors are reported with the publisher
        problems.append(f'{label}: importing {name} failed ({type(e).__name__}: {e}); nothing after it was '
                        'checked')
        raise _publisherError(problems) from None
    if not _sameFile(getattr(module, '__file__', None) or '', expected):
        problems.append(f'{label}: importing {name} loaded {getattr(module, "__file__", None)}, not '
                        f'{expected}; run this from that repo\'s server/ folder')
        return None
    return module


def collectPublishers(projectRoot: Path, section=None, registryModule: str = 'pubSub') -> list:
    """A PublisherSpec for every publisher the Pub/Sub map finds in this repo
    (the AST: route and module-level publishers alike), its model read from
    the imported function: func.responseModel, else the module's
    PUBSUB_PAYLOAD_MODELS entry. Every publisher module is imported, so it
    must be import-safe. Raises one ModelsError listing every publisher
    without exactly one model, and every name in
    `<registryModule>._pubsubTopicRegistry` the map does not see or whose
    publishToProdPubSub it reads otherwise."""
    server = serverDir(Path(projectRoot).resolve())
    section = section if section is not None else buildSection(readWorkingTree(Path(projectRoot)))
    publishers = flag(section.data, 'publishers')
    specs, problems = [], []
    for fn, p in sorted(publishers.items()):
        rel, _, qual = p['anchor'].partition('::')
        module = _importOwn(moduleNameFor(rel), server / rel, problems, p['anchor'])
        if module is None:
            continue
        func = module
        for part in qual.split('.'):
            func = getattr(func, part, None)
        if func is None:
            problems.append(f"{p['anchor']}: not found after importing {module.__name__}")
            continue
        responseModel = getattr(func, 'responseModel', None)
        hatch = getattr(module, HATCH_PAYLOAD_MODELS, None)
        declared = hatch.get(fn) if isinstance(hatch, dict) else None
        if responseModel is not None and declared is not None:
            problems.append(f"{p['anchor']}: it has a route_config responseModel "
                            f'({getattr(responseModel, "__name__", responseModel)}) and a '
                            f'{HATCH_PAYLOAD_MODELS} entry; delete the {HATCH_PAYLOAD_MODELS} entry')
            continue
        if responseModel is None and declared is None:
            problems.append(f"{p['anchor']}: no payload model. Give its @route_config a responseModel, or (a "
                            f"publisher that is no route) add {{{fn!r}: '<Model>'}} to {HATCH_PAYLOAD_MODELS} in "
                            f'server/{rel}, and build the return value through that model')
            continue
        model = responseModel if responseModel is not None else getattr(module, str(declared), None)
        source = PAYLOAD_SOURCES[0] if responseModel is not None else PAYLOAD_SOURCES[1]
        reason = _modelProblem(model)
        if reason:
            problems.append(f"{p['anchor']}: its payload model {getattr(model, '__name__', declared)!s} {reason}")
            continue
        specs.append(PublisherSpec(fn, p['topic'], p['anchor'], flag(p, 'consumers'), flag(p, 'prodPublish'),
                                   model, source))
    registry = _importOwn(registryModule, server / (registryModule.replace('.', '/') + '.py'), problems,
                          'the publisher registry')
    names = getattr(registry, '_pubsubTopicRegistry', None) if registry is not None else []
    if names is None:
        problems.append(f'server/{registryModule}.py has no _pubsubTopicRegistry')
    for name in sorted({entry[0] for entry in names or []} - set(publishers)):
        problems.append(f'{name} is registered by @pubSubDecorator but the Pub/Sub map does not see it (nor '
                        'does pubSubAdmin.py): decorate a def in server/*.py or server/actions/*.py with '
                        '@pubSubDecorator(projectNameConsumers=[...]) written exactly so')
    for name, prod in sorted({(entry[0], entry[1]) for entry in names or [] if entry[0] in publishers}):
        if prod != flag(publishers[name], 'prodPublish'):
            problems.append(f'{name}: @pubSubDecorator registers publishToProdPubSub={prod!r} but the Pub/Sub map '
                            'reads it otherwise (the manifest and the startup topic check follow the map): '
                            'write publishToProdPubSub=False literally, or leave it out')
    if problems:
        raise _publisherError(problems)
    return specs


def _publisherError(problems: list) -> ModelsError:
    return ModelsError('Pub/Sub publishers without a usable payload model:\n  - ' + '\n  - '.join(problems))


def _modelProblem(model) -> str | None:
    from pydantic import BaseModel, RootModel
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return 'is not a pydantic BaseModel subclass'
    if issubclass(model, RootModel):
        return 'is a RootModel: a payload is a dict with explicit keys'
    seen = {}
    _nestedModels(model, seen)
    untyped = [found for m in seen for found in _untypedSerializers(m)]
    if untyped:
        return (f'has a serializer without a return annotation ({"; ".join(untyped)}): the manifest would state the '
                'declared type, not what is sent. Annotate its return type')
    return None


def _nestedModels(annotation, seen: dict) -> None:
    """Every pydantic model `annotation` is or holds, at any depth, into `seen`."""
    from responseModelCheck import models_in
    for model in models_in(annotation):
        if model not in seen:
            seen[model] = None
            for field in model.model_fields.values():
                _nestedModels(field.annotation, seen)


def _untypedSerializers(model) -> list:
    """The serializers of `model` with no return type (field_serializer,
    model_serializer, PlainSerializer, WrapSerializer): pydantic's
    serialization schema then keeps the declared type, whatever they send."""
    import inspect
    from pydantic import PlainSerializer, WrapSerializer
    from pydantic_core import PydanticUndefined

    def untyped(returnType, func) -> bool:
        if returnType is not PydanticUndefined:
            return False
        try:
            return inspect.signature(getattr(func, '__func__', func)).return_annotation is inspect.Signature.empty
        except (TypeError, ValueError):
            return True
    decorators = model.__pydantic_decorators__
    out = [f'{model.__name__}.{", ".join(d.info.fields)}: field_serializer {d.cls_var_name}'
           for d in decorators.field_serializers.values() if untyped(d.info.return_type, d.func)]
    out += [f'{model.__name__}: model_serializer {d.cls_var_name}'
            for d in decorators.model_serializers.values() if untyped(d.info.return_type, d.func)]
    out += [f'{model.__name__}.{name}: {type(m).__name__}' for name, field in model.model_fields.items()
            for m in field.metadata
            if isinstance(m, (PlainSerializer, WrapSerializer)) and untyped(m.return_type, m.func)]
    return out


def manifestFrom(projectName: str, specs: list, secretKeys=frozenset()) -> dict:
    """The manifest of `specs`: one pydantic models_json_schema call in
    serialization mode (one combined $defs), reduced to SCHEMA_KEYS, secret
    keys stripped at every depth, def names made identifiers, and sorted.
    Raises ModelsError for a payload model without an object schema."""
    from pydantic.json_schema import PydanticJsonSchemaWarning, models_json_schema
    problems = []
    for s in specs:
        reason = _modelProblem(s.model)
        if reason:
            problems.append(f'{s.anchor}: its payload model {getattr(s.model, "__name__", s.model)!s} {reason}')
    if problems:
        raise _publisherError(problems)
    models = list(dict.fromkeys(s.model for s in specs))
    with warnings.catch_warnings():
        # a default pydantic cannot serialize is left out (the consumer then defaults to None)
        warnings.simplefilter('ignore', PydanticJsonSchemaWarning)
        try:
            keys, top = models_json_schema([(m, 'serialization') for m in models], ref_template=_DEFS + '{model}')
        except Exception as e:
            raise ModelsError(f'pydantic could not build the payload schemas ({type(e).__name__}: {e})') from None
    defs = {name: _reduce(schema) for name, schema in top.get('$defs', {}).items()}
    _stripSecrets(defs, frozenset(secretKeys))
    renamed = _identifierNames(defs)
    defs = {renamed[name]: _renameRefs(schema, renamed) for name, schema in defs.items()}
    topics = {}
    for s in sorted(specs, key=lambda s: s.topic):
        ref = keys[(s.model, 'serialization')].get('$ref', '')
        name = renamed.get(_refName(ref)) if ref.startswith(_DEFS) else None
        if name is None or not _isClassDef(defs.get(name)):
            problems.append(f'{s.anchor}: its payload model {s.model.__name__} has no object schema')
            continue
        topics[s.topic] = {'fn': s.fn, 'anchor': s.anchor, 'consumers': list(s.consumers),
                           'prodPublish': bool(s.prodPublish), 'payload': {'model': name, 'source': s.source}}
    if problems:
        raise _publisherError(problems)
    return {'formatVersion': FORMAT_VERSION, 'projectName': projectName, 'topics': topics,
            '$defs': {name: defs[name] for name in sorted(defs)}}


def _refName(ref: str) -> str:
    """The def a local `#/$defs/<name>` ref names."""
    return ref[len(_DEFS):]


def _subSchemaItems(schema: dict):
    """(where, sub-schema) for each schema directly inside `schema`, never a
    default value: where is `properties.<ascii(key)>`, `<key>` or `<key>[i]`."""
    for name, sub in schema.get('properties', {}).items():
        yield f'properties.{ascii(name)}', sub
    for key in _SCHEMA_ONE_KEYS:
        if key in schema:
            yield key, schema[key]
    for key in _SCHEMA_LIST_KEYS:
        for i, sub in enumerate(schema.get(key, [])):
            yield f'{key}[{i}]', sub


def _subSchemas(schema: dict):
    return (sub for _, sub in _subSchemaItems(schema))


def _mapSubSchemas(schema: dict, fn) -> dict:
    """`schema` with `fn` applied to each sub-schema, its keys in order."""
    out = {}
    for key, value in schema.items():
        if key == 'properties':
            out[key] = {name: fn(sub) for name, sub in value.items()}
        elif key in _SCHEMA_ONE_KEYS:
            out[key] = fn(value)
        elif key in _SCHEMA_LIST_KEYS:
            out[key] = [fn(sub) for sub in value]
        else:
            out[key] = value
    return out


def _reduce(schema) -> dict:
    """`schema` with only SCHEMA_KEYS, keys sorted, the order of properties
    kept. An additionalProperties that allows anything, an items that is no
    schema, and a format other than date/time are dropped."""
    if not isinstance(schema, dict):
        return {}
    kept = {}
    for key in sorted(k for k in schema if k in SCHEMA_KEYS):
        value = schema[key]
        if (key in _SCHEMA_ONE_KEYS and not isinstance(value, dict)) or (key == 'format' and value not in _FORMATS) \
                or (key == 'readOnly' and value is not True):
            continue
        kept[key] = value
    out = _mapSubSchemas(kept, _reduce)
    if out.get('additionalProperties') == {}:
        del out['additionalProperties']
    return out


def _stripSecrets(defs: dict, secretKeys: frozenset) -> None:
    stack = list(defs.values())
    while stack and secretKeys:
        schema = stack.pop()
        if 'properties' in schema:
            schema['properties'] = {k: v for k, v in schema['properties'].items() if k not in secretKeys}
        if 'required' in schema:
            schema['required'] = [k for k in schema['required'] if k not in secretKeys]
        stack.extend(_subSchemas(schema))


def _identifierBase(text: str, prefix: str) -> str:
    """`text` as an ASCII identifier: each \\W becomes _, leading _ go, and
    `prefix` goes before a leading digit (or stands alone for nothing)."""
    base = re.sub(r'\W', '_', text, flags=re.ASCII).lstrip('_')
    return prefix + base if not base or base[0].isdigit() else base


def _freeName(base: str, taken: set) -> str:
    """`base`, else base_2, base_3, ...: the first not in `taken` (now taken)."""
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f'{base}_{n}', n + 1
    taken.add(candidate)
    return candidate


def _identifierNames(defs: dict) -> dict:
    """{def name: an ASCII identifier}: names that are one stay; the others
    get _identifierBase (and _2, _3, ... on a collision), in sorted order."""
    out = {name: name for name in defs if _IDENT.fullmatch(name)}
    taken = set(out)
    for name in sorted(n for n in defs if n not in out):
        out[name] = _freeName(_identifierBase(name, 'M_'), taken)
    return out


def _renameRefs(schema: dict, renamed: dict) -> dict:
    out = _mapSubSchemas(schema, lambda sub: _renameRefs(sub, renamed))
    ref = out.get('$ref')
    if isinstance(ref, str) and ref.startswith(_DEFS):
        out['$ref'] = _DEFS + renamed.get(_refName(ref), _refName(ref))
    return out


def buildManifest(projectRoot: Path, section=None, registryModule: str = 'pubSub') -> dict:
    """This repo's manifest, from its working tree (or its map `section`,
    when one was built already) and its imported app (collectPublishers).
    Raises ModelsError unless consumers would accept it: it passes
    checkManifest, and each consumer's module renders and loads
    (checkRendered)."""
    projectRoot = Path(projectRoot).resolve()
    section = section if section is not None else buildSection(readWorkingTree(projectRoot))
    specs = collectPublishers(projectRoot, section, registryModule)
    manifest = manifestFrom(section.projectName, specs, section.secretKeys)
    parseManifest(renderManifest(manifest), section.projectName, f'server/{MANIFEST}')
    for consumer in manifestConsumers(manifest):
        checkRendered(manifest, consumer)
    return manifest


def renderManifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=True) + '\n'


def writeManifest(projectRoot: Path, manifest: dict) -> list:
    """Write server/pubsub_models.json when its content changed."""
    path = serverDir(projectRoot) / MANIFEST
    _guardTarget(projectRoot, path)
    if _write(path, renderManifest(manifest)):
        return [f'wrote server/{MANIFEST}']
    return [f'server/{MANIFEST} unchanged']


# ----------------------------------------------------------------------------
# Reading a manifest: untrusted input
# ----------------------------------------------------------------------------

def _noConstant(name):
    raise ValueError(f'{name} is not JSON')


def _noDuplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'duplicate key {key!r}')
        out[key] = value
    return out


def parseManifest(text: str, expectedProject: str | None = None, where: str = 'the manifest') -> dict:
    """A manifest read from `text`, checked (checkManifest). Raises
    ModelsError on anything else: it is a sibling's file."""
    try:
        data = json.loads(text, parse_constant=_noConstant, object_pairs_hook=_noDuplicates)
    except (ValueError, RecursionError) as e:
        raise ModelsError(f'{where}: not a valid manifest ({type(e).__name__}: {e})') from None
    checkManifest(data, expectedProject, where)
    return data


def checkManifest(data, expectedProject: str | None = None, where: str = 'the manifest') -> None:
    """Raise ModelsError unless `data` is a manifest renderModule can render
    safely: format version 1, an identifier projectName (it names a module),
    topics `<fn>_<projectName>` with plain consumer names and an object def
    as payload, local $refs only, def names that are ASCII identifiers and
    no dunder, and only SCHEMA_KEYS with JSON values (finite numbers)."""
    def fail(message):
        raise ModelsError(f'{where}: {message}')
    if not isinstance(data, dict) or set(data) != {'formatVersion', 'projectName', 'topics', '$defs'}:
        fail('expected exactly formatVersion, projectName, topics and $defs')
    if type(data['formatVersion']) is not int or data['formatVersion'] != FORMAT_VERSION:
        fail(f'formatVersion {data["formatVersion"]!r} is not {FORMAT_VERSION}; update pubSubModels.py '
             '(python sharedFileCheck.py --write)')
    project = data['projectName']
    if not (isinstance(project, str) and _IDENT.fullmatch(project) and not keyword.iskeyword(project)
            and isSafeProjectName(project)):
        fail(f'projectName {project!r} is not an identifier')
    if expectedProject is not None and project != expectedProject:
        fail(f'projectName {project!r} is not {expectedProject!r}')
    defs, topics = data['$defs'], data['topics']
    if not isinstance(defs, dict) or not isinstance(topics, dict):
        fail('topics and $defs must be objects')
    for name, schema in defs.items():
        if not _IDENT.fullmatch(name):
            fail(f'def name {name!r} is not an ASCII identifier')
        if name.startswith('__') and name.endswith('__'):
            fail(f'def name {name!r} is a dunder name, which a module cannot define as a class')
        _checkSchema(schema, defs, f'$defs.{name}', 0, fail)
    classes = {}
    for topic, entry in topics.items():
        if not isinstance(entry, dict) or set(entry) != {'fn', 'anchor', 'consumers', 'prodPublish', 'payload'}:
            fail(f'topic {topic!r} must have exactly fn, anchor, consumers, prodPublish and payload')
        fn = entry['fn']
        if not (isinstance(fn, str) and _IDENT.fullmatch(fn) and not fn.startswith('_')
                and not keyword.iskeyword(fn)) or topic != f'{fn}_{project}':
            fail(f'topic {topic!r} is not <fn>_{project} with fn a plain identifier')
        consumers = entry['consumers']
        if not (isinstance(consumers, list) and all(isSafeProjectName(c) for c in consumers)
                and len(set(consumers)) == len(consumers)):
            fail(f'topic {topic!r}: consumers must be distinct plain project names')
        if not isinstance(entry['anchor'], str) or not isinstance(entry['prodPublish'], bool):
            fail(f'topic {topic!r}: anchor must be a string and prodPublish a boolean')
        payload = entry['payload']
        if not (isinstance(payload, dict) and set(payload) == {'model', 'source'}
                and payload['source'] in PAYLOAD_SOURCES and isinstance(payload['model'], str)):
            fail(f'topic {topic!r}: payload must be {{model, source}} with source one of {PAYLOAD_SOURCES}')
        if not _isClassDef(defs.get(payload['model'])):
            fail(f'topic {topic!r}: payload model {payload["model"]!r} is not an object def')
        message = messageClassName(fn)
        if message in classes:
            fail(f'topics {classes[message]!r} and {topic!r} would both generate {message}')
        classes[message] = topic


def _checkSchema(schema, defs: dict, path: str, depth: int, fail) -> None:
    if depth > _MAX_DEPTH:
        fail(f'{path}: nested deeper than {_MAX_DEPTH}')
    if not isinstance(schema, dict):
        fail(f'{path}: a schema must be an object')
    unknown = sorted(set(schema) - set(SCHEMA_KEYS))
    if unknown:
        fail(f'{path}: unexpected keys {unknown}')
    t = schema.get('type')
    types_ = t if isinstance(t, list) else [t] if t is not None else []
    if not types_ and t is not None or not all(isinstance(x, str) and x in _JSON_TYPES for x in types_) \
            or len(set(types_)) != len(types_):
        fail(f'{path}: type {t!r} is not a JSON schema type')
    props = schema.get('properties', {})
    if not isinstance(props, dict) or not all(isinstance(k, str) for k in props):
        fail(f'{path}: properties must be an object')
    required = schema.get('required', [])
    if not isinstance(required, list) or not all(isinstance(k, str) and k in props for k in required):
        fail(f'{path}: required must list properties')
    for key in ('description', 'format'):
        if key in schema and not isinstance(schema[key], str):
            fail(f'{path}: {key} must be a string')
    if 'readOnly' in schema and not isinstance(schema['readOnly'], bool):
        fail(f'{path}: readOnly must be a boolean')
    ref = schema.get('$ref')
    if ref is not None and not (isinstance(ref, str) and ref.startswith(_DEFS) and _refName(ref) in defs):
        fail(f'{path}: $ref {ref!r} is not a def of this manifest')
    if 'enum' in schema and not (isinstance(schema['enum'], list) and schema['enum']
                                 and all(_isScalar(v) for v in schema['enum'])):
        fail(f'{path}: enum must be a non-empty list of strings, numbers, booleans or null')
    if 'const' in schema and not _isScalar(schema['const']):
        fail(f'{path}: const must be a string, number, boolean or null')
    if 'default' in schema and not _isJsonValue(schema['default'], 0):
        fail(f'{path}: default is not a JSON value with finite numbers')
    for key in _SCHEMA_LIST_KEYS:
        if key in schema and not (isinstance(schema[key], list) and schema[key]):
            fail(f'{path}: {key} must be a non-empty list')
    for where, sub in _subSchemaItems(schema):
        _checkSchema(sub, defs, f'{path}.{where}', depth + 1, fail)


def _isScalar(value) -> bool:
    if isinstance(value, float):
        return value == value and value not in (float('inf'), float('-inf'))
    return value is None or isinstance(value, (str, int, bool))


def _isJsonValue(value, depth: int) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, list):
        return all(_isJsonValue(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _isJsonValue(v, depth + 1) for k, v in value.items())
    return _isScalar(value)


def _isClassDef(schema) -> bool:
    """A def rendered as a class: an object schema with properties."""
    return isinstance(schema, dict) and schema.get('type') == 'object' and 'properties' in schema


# ----------------------------------------------------------------------------
# Rendering objects_<publisher>.py (consumer side): pure stdlib
# ----------------------------------------------------------------------------

def _literal(value) -> str:
    """Python source for a JSON value; every string through ascii(), so the
    text is the same on every interpreter and a string can never end it."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return ascii(value)
    if isinstance(value, list):
        return '[' + ', '.join(_literal(v) for v in value) + ']'
    return '{' + ', '.join(f'{ascii(k)}: {_literal(v)}' for k, v in value.items()) + '}'


class _Renderer:
    """Renders the closure of the consumed topics' payload defs."""

    def __init__(self, manifest: dict, topics: list):
        self.defs, self.topics = manifest['$defs'], topics
        self.project = manifest['projectName']
        self.closure = self._closure([entry['payload']['model'] for _, entry in topics])
        self.classes = sorted(n for n in self.closure if _isClassDef(self.defs[n]))
        self.messages = {topic: messageClassName(entry['fn']) for topic, entry in topics}
        self.names = self._classNames()
        self.classNames = set(self.names.values())
        self._inlined = {}  # def name -> the annotation members of an inlined def

    def _closure(self, roots: list) -> set:
        seen, stack = set(), list(roots)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            stack.extend(self._refs(self.defs[name]))
        return seen

    def _refs(self, schema: dict) -> list:
        out, stack = [], [schema]
        while stack:
            s = stack.pop()
            if '$ref' in s:
                out.append(_refName(s['$ref']))
            stack.extend(_subSchemas(s))
        return out

    def _classNames(self) -> dict:
        """{def: class name}. A def keeps its name unless it is reserved (a
        keyword too: <Name>Model) or a topic's <Fn>Message (<Name>Payload)."""
        messageNames = set(self.messages.values())
        out = {n: n for n in self.classes if n not in RESERVED_CLASS_NAMES and n not in messageNames
               and not keyword.iskeyword(n)}
        taken = set(out.values()) | RESERVED_CLASS_NAMES | messageNames
        for name in self.classes:
            if name not in out:
                out[name] = _freeName(name + ('Payload' if name in messageNames else 'Model'), taken)
        return out

    # ---- types -------------------------------------------------------------
    def ann(self, schema: dict, stack: tuple = ()) -> list:
        """The union members of a schema's annotation, in order, None last."""
        if '$ref' in schema:
            return self._refAnn(_refName(schema['$ref']), stack)
        for key in ('anyOf', 'oneOf'):
            if key in schema:
                return _union([m for sub in schema[key] for m in self.ann(sub, stack)])
        if 'allOf' in schema:
            if len(schema['allOf']) != 1:
                raise ModelsError(f'an allOf of {len(schema["allOf"])} schemas cannot be rendered')
            return self.ann(schema['allOf'][0], stack)
        if 'enum' in schema or 'const' in schema:
            values = schema['enum'] if 'enum' in schema else [schema['const']]
            return ['Literal[' + ', '.join(_literal(v) for v in values) + ']']
        t = schema.get('type')
        if isinstance(t, list):
            return _union([m for x in t for m in self.ann(dict(schema, type=x), stack)])
        if t == 'string':
            return [_FORMATS.get(schema.get('format'), 'str')]
        if t in ('integer', 'number', 'boolean', 'null'):
            return [_JSON_TYPES[t]]
        if t == 'array':
            if 'prefixItems' in schema:
                if 'items' in schema:
                    raise ModelsError('a tuple with a variadic tail cannot be rendered')
                return ['tuple[' + ', '.join(_text(self.ann(s, stack)) for s in schema['prefixItems']) + ']']
            return [f'list[{_text(self.ann(schema["items"], stack)) if "items" in schema else "Any"}]']
        if t == 'object' or 'properties' in schema:
            if 'properties' in schema:
                raise ModelsError('an inline object with properties cannot be rendered (only a def can)')
            if 'additionalProperties' in schema:
                return [f'dict[str, {_text(self.ann(schema["additionalProperties"], stack))}]']
            return ['dict[str, Any]']
        return ['Any']

    def _refAnn(self, name: str, stack: tuple) -> list:
        """A class's name, or an inlined def's annotation: each def rendered
        once, at most _MAX_DEPTH inlined defs deep and _MAX_ANNOTATION long."""
        if name in self.names:
            return [self.names[name]]
        if name in self._inlined:
            return list(self._inlined[name])
        if name in stack:
            raise ModelsError('a cycle of defs that are no classes cannot be rendered: '
                              + ' -> '.join(stack[stack.index(name):] + (name,)))
        if len(stack) >= _MAX_DEPTH:
            raise ModelsError(f'defs that are no classes nest deeper than {_MAX_DEPTH}: {stack[0]} -> ...')
        members = self.ann(self.defs[name], stack + (name,))
        if sum(len(m) for m in members) > _MAX_ANNOTATION:
            raise ModelsError(f'the annotation of def {name} is longer than {_MAX_ANNOTATION} characters')
        self._inlined[name] = members
        return list(members)

    def _target(self, schema: dict):
        """The def a property refers to directly ($ref, or allOf of one $ref)."""
        if '$ref' in schema:
            return _refName(schema['$ref'])
        if len(schema.get('allOf', [])) == 1 and '$ref' in schema['allOf'][0]:
            return _refName(schema['allOf'][0]['$ref'])
        return None

    def _dependencies(self, name: str) -> set:
        """The classes a class's fields name, through inlined defs too."""
        out, seen, stack = set(), set(), list(self._refs(self.defs[name]))
        while stack:
            ref = stack.pop()
            if ref in self.names:
                out.add(ref)
            elif ref not in seen:
                seen.add(ref)
                stack.extend(self._refs(self.defs[ref]))
        return out

    def _namesClass(self, member: str) -> bool:
        """True when an annotation member names a generated class (list[X])."""
        return not self.classNames.isdisjoint(_NAMES.findall(member))

    # ---- fields --------------------------------------------------------------
    def fieldNames(self, schema: dict) -> dict:
        """{property key: field name}; a key that cannot be a field name gets
        a derived one and an alias."""
        avoid = self.classNames | set(self.messages.values()) | _ANNOTATION_NAMES | BASEMODEL_ATTRIBUTES
        out = {}
        for key in schema['properties']:
            if (_IDENT.fullmatch(key) and not keyword.iskeyword(key) and not key.startswith('_')
                    and not key.startswith('model_') and key not in avoid):
                out[key] = key
        taken = set(out.values())
        for key in schema['properties']:
            if key in out:
                continue
            candidate = _identifierBase(key, 'f_')
            while (keyword.iskeyword(candidate) or candidate in avoid or candidate in taken
                   or (candidate.startswith('model_') and not candidate.endswith('_'))):
                candidate += '_'
            taken.add(candidate)
            out[key] = candidate
        return out

    def field(self, key: str, name: str, schema: dict, required: bool) -> str:
        members = self.ann(schema)
        target = self._target(schema)
        hasDefault, default = 'default' in schema, schema.get('default')
        if not hasDefault and target is not None and target not in self.names \
                and 'default' in self.defs[target]:
            hasDefault, default = True, self.defs[target]['default']
        args = []
        if schema.get('readOnly'):
            members, args = _union(members + ['None']), ['None']
        elif required:
            pass
        elif hasDefault:
            if default is None:
                members = _union(members + ['None'])
            args = [_literal(default)]
            # a JSON default of a class or a date type is validated into it
            if default is not None and any('_dt.' in m or self._namesClass(m) for m in members):
                args.append('validate_default=True')
        else:
            others = [m for m in members if m != 'None']
            if others and all(m.startswith('list[') for m in others):
                args = ['default_factory=list']
            elif others and all(m.startswith('dict[') for m in others):
                args = ['default_factory=dict']
            else:
                members, args = _union(members + ['None']), ['None']
        if name != key:
            args.append(f'alias={ascii(key)}')
        if 'description' in schema:
            args.append(f'description={ascii(schema["description"])}')
        annotation = _text(members)
        if len(annotation) > _MAX_ANNOTATION:
            raise ModelsError(f'the annotation of field {ascii(key)} is longer than {_MAX_ANNOTATION} characters')
        if not args:
            return f'    {name}: {annotation}'
        if len(args) == 1 and not args[0].startswith(('default_factory=', 'alias=', 'description=')):
            return f'    {name}: {annotation} = {args[0]}'
        return f'    {name}: {annotation} = Field({", ".join(args)})'

    # ---- the module ------------------------------------------------------------
    def order(self) -> tuple:
        """(classes in topological order, ties alphabetical; the classes in
        a reference cycle, which need model_rebuild())."""
        deps = {n: self._dependencies(n) for n in self.classes}
        sccs = _stronglyConnected(self.classes, deps)
        component = {n: i for i, comp in enumerate(sccs) for n in comp}
        cyclic = sorted(n for comp in sccs for n in comp if len(comp) > 1 or n in deps[n])
        needs = {i: {component[d] for n in comp for d in deps[n]} - {i} for i, comp in enumerate(sccs)}
        users = {i: [] for i in needs}
        for i, need in needs.items():
            for j in need:
                users[j].append(i)

        def ready(i):
            return min(self.names[n] for n in sccs[i]), i
        heap = [ready(i) for i, need in needs.items() if not need]
        waiting, out = {i: len(need) for i, need in needs.items()}, []
        heapq.heapify(heap)
        while heap:  # Kahn's algorithm: the first ready component by class name
            _, i = heapq.heappop(heap)
            out += sorted(sccs[i], key=lambda n: self.names[n])
            for user in users[i]:
                waiting[user] -= 1
                if not waiting[user]:
                    heapq.heappush(heap, ready(user))
        return out, cyclic

    def render(self, consumer: str) -> str:
        # Line 2 is constant: a comment on line 1 or 2 can be a PEP 263 coding
        # cookie, so no project name goes there.
        lines = [GENERATED_HEADER,
                 "# The Pub/Sub payload models one publisher sends this repo, from its server/pubsub_models.json",
                 f'# (serialization schema). Publisher {self.project}, consumer {consumer}: one class per object '
                 'schema, a <Fn>Message per topic.',
                 '# Regenerate: cd server && python pubSubModels.py --pull (AppCreator.py runs it first).',
                 '# Consumer-specific checks and conversions belong in helpers, never here.',
                 *_HEADER_LINES, '', '',
                 'class _Message(BaseModel):',
                 "    model_config = ConfigDict(extra='ignore', populate_by_name=True, protected_namespaces=())"]
        order, cyclic = self.order()
        size = sum(len(line) + 1 for line in lines)
        for name in order:
            schema = self.defs[name]
            body = [f'    {ascii(schema["description"])}'] if 'description' in schema else []
            required = set(schema.get('required', []))
            for key, fieldName in self.fieldNames(schema).items():
                body.append(self.field(key, fieldName, schema['properties'][key], key in required))
            block = ['', '', f'class {self.names[name]}(_Message):'] + (body or ['    pass'])
            size += sum(len(line) + 1 for line in block)
            if size > _MAX_MODULE:
                raise ModelsError(f'the module rendered from {self.project} is larger than {_MAX_MODULE} characters')
            lines += block
        if cyclic:
            lines += [''] + [f'{self.names[n]}.model_rebuild()' for n in sorted(cyclic, key=self.names.get)]
        for topic, entry in self.topics:
            doc = f'Payload of topic {topic} ({self.project} {entry["anchor"]}).'
            lines += ['', '', f'class {self.messages[topic]}({self.names[entry["payload"]["model"]]}):',
                      f'    {ascii(doc)}']
        lines += ['', '', 'TOPIC_MESSAGES = {']
        lines += [f'    {ascii(topic)}: {ascii(self.messages[topic])},' for topic, _ in self.topics]
        lines.append('}')
        return '\n'.join(lines) + '\n'


def _union(members: list) -> list:
    """Distinct members in order, None last; Any absorbs the rest."""
    unique = list(dict.fromkeys(members))
    if 'Any' in unique:
        return ['Any']
    return [m for m in unique if m != 'None'] + (['None'] if 'None' in unique else [])


def _text(members: list) -> str:
    return ' | '.join(members)


def _stronglyConnected(nodes: list, deps: dict) -> list:
    """Tarjan's strongly connected components, iteratively."""
    index, low, onStack, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(deps[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        onStack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    onStack.add(child)
                    work.append((child, iter(sorted(deps[child]))))
                elif child in onStack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    member = stack.pop()
                    onStack.discard(member)
                    comp.append(member)
                    if member == node:
                        break
                out.append(sorted(comp))
    return out


def renderModule(manifest: dict, consumer: str) -> str | None:
    """objects_<publisher>.py for `consumer`: the closure of the payload defs
    of the topics that list it (None when none does). Deterministic, LF."""
    checkManifest(manifest)
    topics = [(topic, entry) for topic, entry in sorted(manifest['topics'].items())
              if consumer in entry['consumers']]
    if not topics:
        return None
    try:
        return _Renderer(manifest, topics).render(consumer)
    except RecursionError:
        raise ModelsError(f'the payload models of {manifest["projectName"]} nest too deeply to render') from None


@contextlib.contextmanager
def loadedModule(source: str, name: str):
    """`source` executed as module `name`, with warnings as errors. It sits
    in sys.modules only while in use (pydantic resolves a model's
    annotations through it), so sys.modules is left as it was."""
    module = types.ModuleType(name)
    saved = sys.modules.get(name)
    sys.modules[name] = module
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            exec(compile(source, f'<{name}>', 'exec'), module.__dict__)
            yield module
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def checkRendered(manifest: dict, consumer: str) -> dict:
    """The TOPIC_MESSAGES of the module rendered for `consumer` (what its pull
    would write). Raises ModelsError unless it parses as Python 3.10, holds
    only the renderer's shapes and loads without a warning."""
    source = renderModule(manifest, consumer)
    label = f'the module rendered for {consumer}'
    problems = generatedShapeProblems(source) if source is not None else []
    if problems:
        raise ModelsError(f'{label} is not what the renderer writes: ' + '; '.join(problems))
    try:
        ast.parse(source, feature_version=(3, 10))
        with loadedModule(source, f'_pubSubModelsCheck_{consumer}') as module:
            for name in module.TOPIC_MESSAGES.values():
                getattr(module, name)
            return dict(module.TOPIC_MESSAGES)
    except Exception as e:
        raise ModelsError(f'{label} does not load ({type(e).__name__}: {e})') from None


# ----------------------------------------------------------------------------
# The shape of a generated module (what makes a file count as generated)
# ----------------------------------------------------------------------------

def _importKey(stmt) -> tuple:
    names = tuple((a.name, a.asname) for a in stmt.names)
    return ('from', stmt.module, stmt.level, names) if isinstance(stmt, ast.ImportFrom) else ('import', names)


_HEADER_IMPORTS = frozenset(_importKey(ast.parse(line).body[0]) for line in _HEADER_LINES if line)
_FIELD_KEYWORDS = frozenset({'alias', 'description', 'validate_default'})


def _isLiteral(node) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _isAnnotation(node) -> bool:
    """An annotation as the renderer writes it: names, attributes,
    subscripts, `|` and None. A string, which pydantic would evaluate as
    code under `from __future__ import annotations`, appears only as a value
    inside Literal[...]."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == 'Literal':
        return all(_isLiteral(e) for e in (node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]))
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _isAnnotation(node.value)
    if isinstance(node, ast.Subscript):
        return _isAnnotation(node.value) and _isAnnotation(node.slice)
    if isinstance(node, ast.Tuple):
        return all(_isAnnotation(e) for e in node.elts)
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and _isAnnotation(node.left) \
        and _isAnnotation(node.right)


def _isFieldValue(node) -> bool:
    """A field's value as the renderer writes it: a literal, or Field(...)
    with a literal default, default_factory=list/dict and literal keywords."""
    if _isLiteral(node):
        return True
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Field'):
        return False
    if len(node.args) > 1 or not all(_isLiteral(a) for a in node.args):
        return False
    for kw in node.keywords:
        factory = kw.arg == 'default_factory' and isinstance(kw.value, ast.Name) and kw.value.id in ('list', 'dict')
        if not factory and not (kw.arg in _FIELD_KEYWORDS and _isLiteral(kw.value)):
            return False
    return True


def _classBodyProblem(node: ast.ClassDef):
    for i, stmt in enumerate(node.body):
        docstring = i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
            and isinstance(stmt.value.value, str)
        if isinstance(stmt, ast.Pass) or docstring:
            continue
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.simple \
                and _isAnnotation(stmt.annotation) \
                and (stmt.value is None or _isFieldValue(stmt.value)):
            continue
        if node.name == '_Message' and isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == 'model_config' \
                and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name) \
                and stmt.value.func.id == 'ConfigDict' and not stmt.value.args \
                and all(kw.arg and _isLiteral(kw.value) for kw in stmt.value.keywords):
            continue
        return f'line {stmt.lineno}: class {node.name} holds something other than fields'
    return None


def _statementProblem(stmt):
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        if _importKey(stmt) in _HEADER_IMPORTS:
            return None
        return f'line {stmt.lineno}: an import the renderer never writes'
    if isinstance(stmt, ast.ClassDef):
        if stmt.decorator_list or stmt.keywords or len(stmt.bases) != 1 or not isinstance(stmt.bases[0], ast.Name):
            return f'line {stmt.lineno}: class {stmt.name} is not a plain subclass'
        return _classBodyProblem(stmt)
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and not stmt.value.args \
            and not stmt.value.keywords and isinstance(stmt.value.func, ast.Attribute) \
            and stmt.value.func.attr == 'model_rebuild' and isinstance(stmt.value.func.value, ast.Name):
        return None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
            and stmt.targets[0].id == 'TOPIC_MESSAGES' and isinstance(stmt.value, ast.Dict) \
            and _isLiteral(stmt.value):
        return None
    return f'line {stmt.lineno}: a statement the renderer never writes'


def generatedShapeProblems(text: str) -> list:
    """Why a header-carrying file is not something renderModule writes ([]
    when it is): only the fixed imports, classes holding fields (a
    docstring, annotated fields with literal or Field(...) values,
    _Message's model_config), X.model_rebuild() calls and the literal
    TOPIC_MESSAGES; no def or lambda anywhere."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f'it does not parse (line {e.lineno}: {e.msg})']
    return [p for p in map(_statementProblem, tree.body) if p]


# ----------------------------------------------------------------------------
# Pull (consumer side), check and push (publisher side)
# ----------------------------------------------------------------------------

def readManifestAt(root: Path, ref: str) -> str | None:
    """server/pubsub_models.json as committed at `ref` in `root`, or None
    when that commit has none. Only a regular blob is read (a symlink or a
    submodule there is an error), by object id."""
    path = f'server/{MANIFEST}'
    listing = runGit(root, ['ls-tree', '-z', ref, '--', path])
    if listing is None:
        raise ModelsError(f'could not list {path} in {root} at {ref}')
    records = [r for r in lsTreeRecords(listing) if r[3] == path]
    if not records:
        return None
    mode, kind, oid, _ = records[0]
    if not isRegularBlob(mode, kind) or not _OID.fullmatch(oid):
        raise ModelsError(f'{path} in {root} at {ref} is not a regular file')
    data = runGit(root, ['cat-file', 'blob', oid.decode('ascii')])
    if data is None:
        raise ModelsError(f'could not read {path} in {root} at {ref}')
    return normaliseSource(data)


def _readSiblingManifest(src) -> str | None:
    if src.origin == 'ref':
        return readManifestAt(Path(src.root), src.ref)
    path = Path(src.root) / 'server' / MANIFEST
    if path.is_symlink():
        raise ModelsError(f'{path} is a symlink: not read')
    return normaliseSource(path.read_bytes()) if path.is_file() else None


class PullPlan:
    """What a pull writes ({file name: text}), deletes and keeps ({file
    name: why}), the publishers it could not read ({projectName: why}:
    absent, or no manifest yet), and what it noticed (`notes`, `sources`)."""

    def __init__(self):
        self.writes, self.deletes, self.kept, self.missing = {}, [], {}, {}
        self.notes, self.sources = [], []


@memoizedGit()
def planPull(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False,
             sources: dict | None = None, ownName: str | None = None) -> PullPlan:
    """The generated files this repo should hold, from every sibling's
    manifest (read where the map reads that sibling; `sources` and `ownName`
    are a FleetMap's, to skip discovering again). Raises ModelsError for a
    bad manifest or a hand-written file at a target path, before anything
    is written."""
    root = Path(projectRoot).resolve()
    plan, server = PullPlan(), serverDir(root)
    if sources is None:
        found = []
        ownName, sources, _, _ = fleetSources(root, overrides, worktreeSiblings, found)
        plan.notes += [f'WARNING: {w}' for w in found]
    elif ownName is None:
        raise ValueError('planPull needs ownName with sources')
    for name, src in sorted(sources.items()):
        if name == ownName:
            continue
        fileName = generatedFileName(name)
        exists = (server / fileName).exists()
        if src.section is None:
            plan.sources.append(f'{name}: absent ({src.reason})')
            plan.missing[name] = f'absent here: {src.reason}'
            if exists:
                plan.kept[fileName] = f'{name} is absent here: {src.reason}'
            continue
        where = f'{src.dirName or name}@{src.ref or "working tree"}'
        text = _readSiblingManifest(src)
        if text is None:
            plan.sources.append(f'{name}: {where}, no server/{MANIFEST}')
            reason = f'{name} has no server/{MANIFEST} at {src.ref or "its working tree"} (not adopted yet)'
            plan.missing[name] = reason
            if exists:
                plan.kept[fileName] = reason
            else:
                plan.notes.append(f'{reason}; nothing generated from it')
            continue
        manifest = parseManifest(text, name, f'{where}:server/{MANIFEST}')
        module = renderModule(manifest, ownName)
        consumed = sum(ownName in e['consumers'] for e in manifest['topics'].values())
        plan.sources.append(f'{name}: {where}, {len(manifest["topics"])} topic(s), {consumed} for {ownName}')
        if module is None:
            continue
        if exists and not _hasHeader(server / fileName):
            raise ModelsError(
                f'server/{fileName} is hand-written, so pull will not overwrite it: move its non-message code '
                '(validators, conversions, constants) to helpers, then rename or delete it, and pull again')
        plan.writes[fileName] = module
    plan.deletes = [p.name for p in _generatedPaths(server) if p.name not in plan.writes and p.name not in plan.kept]
    return plan


def _apply(projectRoot: Path, plan: PullPlan) -> tuple:
    """Write and delete what `plan` says (each target guarded first, so a
    refusal writes nothing): (written, removed) file names."""
    server = serverDir(projectRoot)
    for name in list(plan.writes) + plan.deletes:
        _guardTarget(projectRoot, server / name)
    written = [name for name, text in sorted(plan.writes.items()) if _write(server / name, text)]
    for name in plan.deletes:
        (server / name).unlink()
    return written, list(plan.deletes)


def _pullLines(plan: PullPlan, written: list, removed: list) -> list:
    return ([f'wrote server/{name}' for name in written]
            + [f'removed server/{name} (generated, and no publisher generates it any more)' for name in removed]
            + [f'kept server/{name} ({why})' for name, why in sorted(plan.kept.items())] + plan.notes)


def pull(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False) -> list:
    """Regenerate this repo's objects_<publisher>.py files (written only on
    change, LF) and delete the stale ones. Returns human lines."""
    root = Path(projectRoot).resolve()
    plan = planPull(root, overrides, worktreeSiblings)
    written, removed = _apply(root, plan)
    return _pullLines(plan, written, removed)


def check(projectRoot: Path, overrides: dict | None = None, worktreeSiblings: bool = False) -> tuple:
    """(lines, exit code): 1 when a generated file is missing, differs from
    a fresh render, or is generated but nothing generates it any more."""
    root = Path(projectRoot).resolve()
    plan, server = planPull(root, overrides, worktreeSiblings), serverDir(root)
    stale = []
    for name, text in sorted(plan.writes.items()):
        current = readLF(server / name)
        if current is None:
            stale.append(f'MISSING: server/{name} is not generated yet')
        elif current != text:
            stale.append(f'STALE: server/{name} differs from a fresh render')
    stale += [f'STRAY: server/{name} is generated, but no publisher generates it any more' for name in plan.deletes]
    lines = [f'source: {line}' for line in plan.sources] + _pullLines(plan, [], []) + stale
    if stale:
        lines.append('STALE: run `cd server && python pubSubModels.py --pull` and commit server/objects_*.py.')
    return lines, 1 if stale else 0


@memoizedGit()
def push(projectRoot: Path, overrides: dict | None = None) -> tuple:
    """--push (see the module docs): pull in every consumer checkout of this
    repo's committed manifest that passes the gates. Returns (lines, exit
    code): 2 when a consumer errored, else 0. Never stages or commits."""
    root = Path(projectRoot).resolve()
    branch = _branchName(root)
    if branch is None:
        raise ModelsError('--push needs a branch: this repo is on a detached HEAD')
    committed = readManifestAt(root, 'HEAD')
    if committed is None:
        raise ModelsError(f'server/{MANIFEST} is not committed: run AppCreator.py (or python pubSubModels.py '
                          '--write-manifest), commit it, then push')
    found, changed, failed = [], [], False
    ownName, sources, _, _ = fleetSources(root, overrides, False, found)
    lines = [f'WARNING: {w}' for w in found]
    manifest = parseManifest(committed, ownName, f'HEAD:server/{MANIFEST}')
    if readLF(serverDir(root) / MANIFEST) != committed:
        lines.append(f'WARNING: server/{MANIFEST} differs from HEAD; consumers read the committed one, so '
                     'commit it and push again')
    for name in manifestConsumers(manifest):
        src = sources.get(name)
        label = (src.dirName if src else None) or name
        reason = 'no services file or snapshot names it' if src is None else _writeAllSkip(src, label, branch)
        if reason:
            lines.append(f'{label}: skipped ({reason})')
            continue
        consumerRoot = Path(src.root).resolve()
        try:
            plan = planPull(consumerRoot, overrides)
            written, removed = _apply(consumerRoot, plan)
        except Exception as e:  # one consumer's failure must not stop the others
            failed = True
            lines.append(errorLine(label, e))
            continue
        done = [f'wrote server/{n}' for n in written] + [f'removed server/{n}' for n in removed]
        lines.append(f'{label}: ' + (', '.join(done) or 'unchanged'))
        lines += [f'  {line}' for line in _pullLines(plan, [], [])]
        if done:
            changed.append((consumerRoot, [f'server/{n}' for n in written + removed]))
    if changed:
        lines += commitLines(root, 'the generated models', changed)
        lines.append("Then run `cd server && python pubSubMap.py --write-all`: the consumers' handler contracts "
                     'changed with their models.')
    else:
        lines.append('No generated model changed; nothing to commit.')
    return lines, 2 if failed else 0


# ----------------------------------------------------------------------------
# The handler rule: a handler consumes its message only through <Fn>Message
# ----------------------------------------------------------------------------

_VALIDATION_ERRORS = frozenset({'pydantic.ValidationError', 'pydantic_core.ValidationError',
                                'pydantic_core._pydantic_core.ValidationError'})


def handlerProblems(section, handler: str) -> list:
    """Why push handler `handler` of `section` does not consume its message
    only through its generated <Fn>Message ([] when it does):
    1. server/objects_<source>.py is generated, defines <Fn>Message, and its
       TOPIC_MESSAGES maps the topic to it;
    2. the map sees exactly one parse of the whole message, with that class,
       and it is not lenient (no `except Exception` swallows it);
    3. the message parameter is read once, in `<Fn>Message(**data)` or
       `<Fn>Message.model_validate(data)` (named keywords such as
       strict=True allowed; the class imported plainly, aliased or through
       its module), is never rebound, and is read
       elsewhere only inside an `except ValidationError` clause (that one
       type, not a tuple) of the try around that parse (the
       malformed-message fallback)."""
    entry = flag(section.data, 'handlers').get(handler)
    if entry is None:
        return [f'{handler} is not a push handler of class PubSubRequests']
    source, fn = entry['source'], entry['topicFn']
    message, fileName = messageClassName(fn), generatedFileName(source)
    anchor = f'{fileName}::{message}'
    fix = (f'Parse it with `message = {message}.model_validate(data)` (from objects_{source} import {message}), '
           'read everything from message, and move consumer logic to helpers.')
    problems = _generatedModuleProblems(section, fileName, message, f'{fn}_{source}')
    roots = [r for r in section.requirements.get(handler, []) if r.path == () and r.kind == 'model']
    if not (len(roots) == 1 and roots[0].model == anchor and not roots[0].lenient and not roots[0].deadBranch):
        seen = ', '.join(sorted(f'{r.model}{" (lenient)" if r.lenient else ""}' for r in roots)) or 'none'
        problems.append(f'the map must see one parse of the whole message with {anchor}, outside any '
                        f'`except Exception`; it sees: {seen}')
    problems += _dataUseProblems(section, handler, anchor, message)
    return [f'{handler}: {p}. {fix}' for p in problems]


def _generatedModuleProblems(section, fileName: str, message: str, topic: str) -> list:
    module = section.repo.module(fileName)
    if module is None or not isGeneratedModels(fileName, module.text):
        return [f'server/{fileName} is not a generated module (run python pubSubModels.py --pull)']
    out = []
    if not any(isinstance(n, ast.ClassDef) and n.name == message for n in module.tree.body):
        out.append(f'server/{fileName} defines no {message}')
    found = module.names.get('TOPIC_MESSAGES')
    try:
        topics = ast.literal_eval(found[1]) if found and found[0] == 'value' else {}
    except ValueError:
        topics = {}
    if not isinstance(topics, dict) or topics.get(topic) != message:
        out.append(f'server/{fileName} TOPIC_MESSAGES does not map {topic} to {message}')
    return out


def _handlerDef(section, handler: str):
    rel, line, _ = section.lines[('handler', handler)]
    module = section.repo.module(rel)
    node = next(n for n in ast.walk(module.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == handler and n.lineno == line)
    return module, node


def _dataUseProblems(section, handler: str, anchor: str, message: str) -> list:
    module, node = _handlerDef(section, handler)
    params = [*node.args.posonlyargs, *node.args.args]
    if len(params) < 2:
        return ['it takes no message parameter']
    p = params[1].arg
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    problems, loads = [], []
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == p:
            if isinstance(n.ctx, ast.Load):
                loads.append(n)
            else:
                problems.append(f'line {n.lineno} rebinds {p}')
        elif isinstance(n, ast.arg) and n.arg == p and n is not params[1]:
            problems.append(f'line {n.lineno} shadows {p} in a nested function')
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and p in n.names:
            problems.append(f'line {n.lineno} rebinds {p}')

    def isExpected(expr) -> bool:
        found = section.repo.resolveExpr(module, expr)
        return bool(found and found[0] == 'class' and found[1].anchor == anchor)

    def parseCall(load):
        parent = parents.get(load)
        if isinstance(parent, ast.keyword) and parent.arg is None:
            call = parents.get(parent)
            if isinstance(call, ast.Call) and not call.args and call.keywords == [parent] \
                    and isExpected(call.func):
                return call
        if isinstance(parent, ast.Call) and parent.args == [load] \
                and all(k.arg is not None for k in parent.keywords) \
                and isinstance(parent.func, ast.Attribute) and parent.func.attr == 'model_validate' \
                and isExpected(parent.func.value):
            return parent
        return None

    def fallbackTry(load):
        """The try whose `except ValidationError` clause holds `load`."""
        parent = parents.get(load)
        while parent is not None and parent is not node:
            if isinstance(parent, ast.ExceptHandler) and parent.type is not None:
                found = section.repo.resolveExpr(module, parent.type)
                if found and found[0] == 'ext' and found[1] in _VALIDATION_ERRORS:
                    return parents.get(parent)
            parent = parents.get(parent)
        return None

    fallbacks = [(load, fallbackTry(load)) for load in loads]
    main = [load for load, tryNode in fallbacks if tryNode is None]
    parses = [parseCall(load) for load in main]
    if len(main) != 1 or parses[0] is None:
        lines = ', '.join(str(load.lineno) for load in main) or 'none'
        problems.append(f'{p} must be read exactly once, in {message}.model_validate({p}) or {message}(**{p}); '
                        f'it is read outside an `except ValidationError` fallback (a clause of that one type: '
                        f'another exception, or a tuple holding ValidationError, does not count) at line(s) {lines}')
        return problems
    for load, tryNode in fallbacks:
        if tryNode is not None and not any(n is parses[0] for stmt in tryNode.body for n in ast.walk(stmt)):
            problems.append(f'line {load.lineno} reads {p} in an `except ValidationError` clause of a try that '
                            f'does not hold the {message} parse')
    return problems


# ----------------------------------------------------------------------------
# The command line
# ----------------------------------------------------------------------------

def main(argv=None, projectRoot: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--pull', '--write', action='store_true', dest='pull',
                      help="regenerate this repo's objects_<publisher>.py files")
    mode.add_argument('--push', action='store_true',
                      help='pull in every consumer checkout on this branch (never commits)')
    mode.add_argument('--write-manifest', action='store_true',
                      help='write server/pubsub_models.json (imports the app: ENVIRONMENT=localdev, like '
                           'AppCreator.py)')
    parser.add_argument('--repo', action='append', metavar='NAME=PATH[@REF]',
                        help="read a sibling from PATH (at REF) instead of ../<localRepoDirName>")
    parser.add_argument('--worktree-siblings', action='store_true',
                        help="read siblings' working trees instead of their committed refs")
    args = parser.parse_args(argv)
    projectRoot = Path(projectRoot or Path(__file__).resolve().parent.parent)
    try:
        return _run(args, projectRoot)
    except MapError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    except Exception as e:  # anything else is a bug, not a stale file
        print(f'ERROR: unexpected {type(e).__name__}: {e}', file=sys.stderr)
        return 2


def _run(args, projectRoot: Path) -> int:
    overrides = _parseOverrides(args.repo)
    if args.push or args.write_manifest:
        if args.worktree_siblings:
            raise ModelsError('--worktree-siblings goes with --pull or the check only: --push pulls from '
                              "each consumer's committed view, and --write-manifest reads only this repo")
    if args.write_manifest:
        if overrides:
            raise ModelsError('--write-manifest reads only this repo; --repo does not apply')
        if not os.environ.get('ENVIRONMENT', '').startswith('local'):
            raise ModelsError('--write-manifest imports the app: run it as `ENVIRONMENT=localdev python '
                              'pubSubModels.py --write-manifest` (like AppCreator.py)')
        server = str(serverDir(projectRoot.resolve()))
        if server not in sys.path:
            sys.path.insert(0, server)
        lines = writeManifest(projectRoot, buildManifest(projectRoot))
    elif args.push:
        lines, code = push(projectRoot, overrides)
        print('\n'.join(lines))
        return code
    elif args.pull:
        lines = pull(projectRoot, overrides, args.worktree_siblings) or ['generated models unchanged']
    else:
        lines, code = check(projectRoot, overrides, args.worktree_siblings)
        print('\n'.join(lines))
        return code
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())

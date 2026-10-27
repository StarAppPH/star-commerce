"""Build one repo's section of the Pub/Sub map (see pubSubMap.py).

A section is a pure function of one repo's server/ sources:
- the repo's projectName (server/AppConfig.py) and its services
  (server/services/*.py: the sibling projects and their checkout folders);
- its publishers: every @pubSubDecorator function, its topic, consumers,
  payload source, whether pubSubAdmin.py can see it, and the hand-publish
  sites attributed to it;
- its push handlers: every public @route_config method of class
  PubSubRequests, with the message contract pubSubMapContract.py resolves;
- the escape hatch PUBSUB_PAYLOAD_MODELS in a publisher's module, read by
  AST and never imported: it declares a model and suppresses nothing. The
  retired PUBSUB_CONTRACT_GAPS (it used to silence compat issues) and
  PUBSUB_MESSAGE_MODELS (a handler parses its generated <Fn>Message
  instead) are hatch errors.

Values that come from source and end up in file paths (projectName,
localRepoDirName) are validated here; anything else is reported, never used.

Pure stdlib, Python 3.10 compatible, byte-identical in every repo.
"""
from __future__ import annotations

import ast
import re

from pubSubMapContract import ContractResolver
from pubSubMapScan import (ClassInfo, MapError, Module, PayloadResolver, Repo, constStr, decoratorCall,
                           finalName, kwarg, readProjectName, sourceText)

HATCH_PAYLOAD_MODELS = 'PUBSUB_PAYLOAD_MODELS'
RETIRED_CONTRACT_GAPS = 'PUBSUB_CONTRACT_GAPS'
RETIRED_MESSAGE_MODELS = 'PUBSUB_MESSAGE_MODELS'
COMPAT_ISSUES = ('undeclared', 'nullable', 'literal', 'numericNarrowing', 'forbiddenExtra')
# Twin of PROJECT_NAME and REPO_DIR_NAME (plus _parseRepo's no-trailing-'.'
# rule) in sharedFileCheck.py; keep in step.
_PROJECT_NAME = re.compile(r'[A-Za-z0-9_-]+')
_DIR_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')

# What the map leaves out because it is the usual value; read it back with
# flag(entry, key). A publisher publishes to prod, lists its consumers
# literally and sits where pubSubAdmin.py looks; a handler's contract
# resolves; nothing is mock-generated, declared or truncated.
PUBLISHER_DEFAULTS = {'prodPublish': True, 'consumersLiteral': True, 'inScannedDir': True,
                      'mockGenerated': False, 'extraPublishSites': [], 'consumers': [],
                      'unresolvedConsumers': [], 'pubSubAdminProblems': []}
HANDLER_DEFAULTS = {'resolved': True, 'truncated': False}
SECTION_DEFAULTS = {'services': {}, 'publishers': {}, 'handlers': {}, 'utilityHandlers': [],
                    'unattributedPublishSites': [], 'hatchErrors': [], 'heuristicNames': [],
                    'ambiguousNames': []}
_DEFAULTS = {**SECTION_DEFAULTS, **PUBLISHER_DEFAULTS, **HANDLER_DEFAULTS}


def flag(entry: dict, key: str):
    """A section, publisher or handler field, with the default the map omits."""
    return entry.get(key, _DEFAULTS[key])


def _compact(entry: dict, defaults: dict) -> dict:
    return {k: v for k, v in entry.items() if not (k in defaults and v == defaults[k])}


def isSafeProjectName(name) -> bool:
    """Twin of _parseRepo's projectName check in sharedFileCheck.py; keep in step."""
    return isinstance(name, str) and _PROJECT_NAME.fullmatch(name) is not None


def isSafeDirName(name) -> bool:
    """A single plain folder name: no separator, drive, '..' or UNC prefix.
    Twin of _parseRepo's "dir" check in sharedFileCheck.py; keep in step."""
    return isinstance(name, str) and _DIR_NAME.fullmatch(name) is not None and not name.endswith('.')


def projectNameOf(repo: Repo) -> str | None:
    """projectName of class Project in server/AppConfig.py (readProjectName)."""
    module = repo.module('AppConfig.py')
    return readProjectName(module.text) if module else None


def servicesOf(repo: Repo, errors: list) -> dict:
    """{var: {projectName, localRepoDirName, file, cls, dev}} from
    server/services/*.py (`var = Cls(projectName=..., localRepoDirName=...)`;
    projectName defaults to the file stem, as AbstractService.getProjectName
    does). A name that could not be a project or a folder name is reported
    in `errors` and left out."""
    out = {}
    for rel, module in repo.modules.items():
        parts = rel.split('/')
        if len(parts) != 2 or parts[0] != 'services' or parts[1].startswith('__') \
                or parts[1] == 'AllServices.py':
            continue
        for stmt in module.tree.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Call)):
                continue
            call = stmt.value
            if not any(kwarg(call, k) is not None for k in ('localRepoDirName', 'cloudRunServiceName')):
                continue
            projectName = constStr(kwarg(call, 'projectName')) or parts[1][:-3]
            dirName = constStr(kwarg(call, 'localRepoDirName'))
            if not isSafeProjectName(projectName) or (dirName is not None and not isSafeDirName(dirName)):
                errors.append(f'server/{rel}: projectName {projectName!r} / localRepoDirName {dirName!r} '
                              'is not a plain name; this service is ignored')
                continue
            out[stmt.targets[0].id] = {'projectName': projectName, 'localRepoDirName': dirName,
                                       'file': rel, 'cls': finalName(call.func),
                                       'dev': constStr(kwarg(kwarg(call, 'path'), 'dev'))}
    return out


def _literal(node, depth: int):
    """A literal str, or a {str: ...} dict nested `depth` levels, else None."""
    if depth == 0:
        return constStr(node)
    if not isinstance(node, ast.Dict) or any(constStr(k) is None for k in node.keys):
        return None
    out = {}
    for k, v in zip(node.keys, node.values):
        out[constStr(k)] = constStr(v) if constStr(v) is not None else _literal(v, depth - 1)
        if out[constStr(k)] is None:
            return None
    return out


class Section:
    """A repo's section (`data`, what the map stores) plus what the join needs
    live: publisher payload shapes, structured requirements and lines."""

    def __init__(self, projectName: str, repo: Repo):
        self.projectName, self.repo = projectName, repo
        self.data: dict = {}
        self.shapes, self.requirements, self.lines = {}, {}, {}
        self.payloads = PayloadResolver(repo)
        self.secretKeys = frozenset()
        self.services: dict = {}


def _emptySection(files: dict[str, str], projectName: str | None) -> Section:
    repo = Repo(files)
    projectName = projectName or projectNameOf(repo)
    if projectName is None:
        raise MapError('server/AppConfig.py declares no projectName in class Project')
    if not isSafeProjectName(projectName):
        raise MapError(f'server/AppConfig.py: projectName {projectName!r} is not a plain name')
    return Section(projectName, repo)


def publisherProdFlags(files: dict[str, str], projectName: str | None = None) -> dict[str, bool]:
    """{publisher function: prodPublish} of one repo's server/ sources: the
    publishers buildSection finds, without its handler contracts (a fraction
    of the time). checkPubSubTopics.py compares it with the manifest in
    the predeploy job (job_predeploy.py)."""
    section = _emptySection(files, projectName)
    publishers = _publishers(section, servicesOf(section.repo, []), [])
    return {fn: p['prodPublish'] for fn, p in publishers.items()}


def buildSection(files: dict[str, str], projectName: str | None = None) -> Section:
    """The map section of one repo's server/ sources (see the module docs)."""
    section = _emptySection(files, projectName)
    repo, projectName = section.repo, section.projectName
    section.secretKeys = secretKeys(repo)
    hatchErrors = []
    services = section.services = servicesOf(repo, hatchErrors)
    publishers = _publishers(section, services, hatchErrors)
    unattributed = _handPublishSites(section, publishers)
    handlers, utility = _handlers(section, hatchErrors)
    section.data = _compact({
        'projectName': projectName,
        'services': {var: {'projectName': s['projectName'], 'localRepoDirName': s['localRepoDirName']}
                     for var, s in sorted(services.items())},
        'publishers': {fn: _compact(p, PUBLISHER_DEFAULTS) for fn, p in publishers.items()},
        'handlers': {h: _compact(d, HANDLER_DEFAULTS) for h, d in handlers.items()},
        'utilityHandlers': sorted(utility),
        'unattributedPublishSites': sorted(unattributed, key=lambda s: (s['anchor'], s['topic'])),
        'hatchErrors': sorted(set(hatchErrors)),
        'heuristicNames': sorted(repo.heuristic),
        'ambiguousNames': sorted(repo.ambiguous),
    }, SECTION_DEFAULTS)
    return section


def secretKeys(repo: Repo) -> frozenset:
    """SECRET_RESPONSE_KEYS when this repo's pubSubDecorator wrapper strips
    them from every payload (calls stripSecretKeys); empty otherwise."""
    module = repo.module('pubSub.py')
    entry = module.names.get('pubSubDecorator') if module else None
    if not entry or entry[0] != 'def':
        return frozenset()
    if not any(isinstance(n, ast.Call) and finalName(n) == 'stripSecretKeys' for n in ast.walk(entry[1])):
        return frozenset()
    found = repo.resolveName(module, 'stripSecretKeys')
    if not found or found[0] != 'func':
        return frozenset()
    keys = set()
    for name in sorted({n.id for n in ast.walk(found[2]) if isinstance(n, ast.Name)}):
        value = repo.resolveName(found[1], name)
        if value and value[0] == 'value':
            keys.update(constStr(n) for n in ast.walk(value[2]) if constStr(n))
    return frozenset(keys)



def _functions(body, owner, repo, module):
    """(owner ClassInfo or None, def) for module- and class-level defs."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield owner, stmt
        elif isinstance(stmt, ast.ClassDef):
            qual = f'{owner.key[1]}.{stmt.name}' if owner else stmt.name
            yield from _functions(stmt.body, repo.classInfo(module, stmt, qual), repo, module)


def _anchor(module: Module, owner, node) -> str:
    return f'{module.rel}::{owner.key[1]}.{node.name}' if owner else f'{module.rel}::{node.name}'


def _mockGenerated(repo: Repo) -> set:
    """Final names of calls made with publishToPubSubMockDb=True in
    pubSubMockDataGenerator.py."""
    module = repo.module('pubSubMockDataGenerator.py')
    names = set()
    for node in ast.walk(module.tree) if module else []:
        value = kwarg(node, 'publishToPubSubMockDb') if isinstance(node, ast.Call) else None
        if isinstance(value, ast.Constant) and value.value is True and finalName(node.func):
            names.add(finalName(node.func))
    return names


def _publishers(section: Section, services: dict, hatchErrors: list) -> dict:
    repo, out = section.repo, {}
    mocked = _mockGenerated(repo)
    for rel, module in repo.modules.items():
        found = 'pubSubDecorator' in module.text
        declaredModels = _payloadHatch(module, hatchErrors) if HATCH_PAYLOAD_MODELS in module.text else {}
        names = set()
        for owner, node in (_functions(module.tree.body, None, repo, module) if found else []):
            dec = next((d for d in node.decorator_list if finalName(d) == 'pubSubDecorator'), None)
            if dec is None:
                continue
            name = node.name
            names.add(name)
            if name in out:
                hatchErrors.append(f'duplicate publisher name {name}: {out[name]["anchor"]} and '
                                   f'{_anchor(module, owner, node)}')
                continue
            consumersNode = kwarg(dec, 'projectNameConsumers') or (
                dec.args[0] if isinstance(dec, ast.Call) and dec.args else None)
            literal = isinstance(consumersNode, (ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Name) for e in consumersNode.elts)
            consumers, unresolved = [], []
            for elt in (consumersNode.elts if literal else []):
                project = _serviceProject(module, elt.id, services)
                (consumers if project else unresolved).append(project or elt.id)
            prod = kwarg(dec, 'publishToProdPubSub')
            prodPublish = not (isinstance(prod, ast.Constant) and prod.value is False)
            source, model, shape = section.payloads.resolve(module, node, owner, declaredModels.get(name))
            out[name] = {
                'topic': f'{name}_{section.projectName}',
                'anchor': _anchor(module, owner, node),
                'kind': 'route' if decoratorCall(node, 'route_config') else 'module',
                'prodPublish': prodPublish,
                'consumers': sorted(set(consumers)),
                'consumersLiteral': literal,
                'unresolvedConsumers': sorted(unresolved),
                'inScannedDir': '/' not in rel or (rel.startswith('actions/') and rel.count('/') == 1),
                'mockGenerated': name in mocked,
                'payload': {'source': source, 'model': model},
                'extraPublishSites': [],
                'pubSubAdminProblems': _adminProblems(module, node, dec, services) if prodPublish else [],
            }
            if not literal:
                out[name]['rawConsumers'] = sourceText(module, consumersNode) if consumersNode else ''
            section.shapes[name] = shape
            section.lines[('publisher', name)] = (rel, node.lineno, (module.rel, node.lineno))
        for fn in sorted(set(declaredModels) - names):
            hatchErrors.append(f'server/{rel}: {HATCH_PAYLOAD_MODELS}[{fn!r}] names no '
                               '@pubSubDecorator function of this module')
        if HATCH_PAYLOAD_MODELS in module.text and HATCH_PAYLOAD_MODELS in module.names and not names:
            hatchErrors.append(f'server/{rel}: {HATCH_PAYLOAD_MODELS} sits in a module with no '
                               '@pubSubDecorator function')
    return dict(sorted(out.items()))


def _adminProblems(module: Module, node, dec, services: dict) -> list:
    """Why pubSubAdmin.py would not create this publisher's topic and
    subscriptions: it reads only a sync def decorated `@pubSubDecorator(...)`
    (a bare Name), consumers passed as `projectNameConsumers=[...]`, and
    each consumer by its services class name (getServiceByName)."""
    problems = []
    if isinstance(node, ast.AsyncFunctionDef):
        problems.append('it is an async def')
    if not (isinstance(dec, ast.Name) or (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name))):
        problems.append('the decorator is not written @pubSubDecorator(...)')
    consumers = kwarg(dec, 'projectNameConsumers')
    if consumers is None:
        problems.append('consumers are not passed as projectNameConsumers=[...]')
    classes = {svc['cls'].lower() for svc in services.values() if svc.get('cls')}
    for elt in getattr(consumers, 'elts', []):
        text = sourceText(module, elt)
        if text.lower() not in classes:
            problems.append(f'consumer {text} is not a services class name (getServiceByName)')
    return problems


def _serviceProject(module, name, services):
    entry = module.names.get(name)
    if entry and entry[0] == 'from' and entry[1].startswith('services') and entry[2] in services:
        return services[entry[2]]['projectName']
    return services[name]['projectName'] if name in services else None


def _payloadHatch(module: Module, hatchErrors: list) -> dict:
    entry = module.names.get(HATCH_PAYLOAD_MODELS)
    if entry is None:
        return {}
    data = _literal(entry[1], 1) if entry[0] == 'value' else None
    if data is None:
        hatchErrors.append(f'server/{module.rel}: {HATCH_PAYLOAD_MODELS} must be a literal '
                           "{'<publisher>': '<Model>'} dict")
        return {}
    out = {}
    for fn, modelName in data.items():
        ci = module.repo.resolveClass(module, ast.Name(id=modelName))
        if ci is None or not module.repo.isModel(ci):
            hatchErrors.append(f'server/{module.rel}: {HATCH_PAYLOAD_MODELS}[{fn!r}] names '
                               f'{modelName!r}, which is not a pydantic model there')
        else:
            out[fn] = ci
    return out


def _handPublishSites(section: Section, publishers: dict) -> list:
    """Attribute each direct publishMessage call (outside pubSub.py and
    tools/, at any depth: nested defs and lambdas count, anchored to their
    enclosing top-level function) whose topic is f'{X.__name__}_...' with X
    a decorated publisher; the rest are unattributed."""
    repo, unattributed = section.repo, []
    byLine = {loc[2]: name for (kind, name), loc in section.lines.items() if kind == 'publisher'}
    for rel, module in repo.modules.items():
        if rel == 'pubSub.py' or rel.startswith('tools/') or 'publishMessage' not in module.text:
            continue
        scopes = [(node, _anchor(module, owner, node))
                  for owner, node in _functions(module.tree.body, None, repo, module)]
        seen = set()
        for scope, anchor in scopes + [(module.tree, f'{rel}::<module>')]:
            for call in ast.walk(scope):
                if not (isinstance(call, ast.Call) and finalName(call.func) == 'publishMessage') \
                        or id(call) in seen:
                    continue
                seen.add(id(call))
                topic = kwarg(call, 'topicName') or kwarg(call, 'topic') or (
                    call.args[1] if len(call.args) > 1 else None)
                name = byLine.get(_topicPublisher(repo, module, scope, topic))
                if name is not None:
                    publishers[name]['extraPublishSites'].append(anchor)
                else:
                    unattributed.append({'anchor': anchor,
                                         'topic': sourceText(module, topic) if topic else '?'})
    for entry in publishers.values():
        entry['extraPublishSites'] = sorted(set(entry['extraPublishSites']))
    return unattributed


def _topicPublisher(repo, module, scope, topic):
    """(rel, line) of the function X in a topic f'{X.__name__}_...' (or
    X.__name__ + ...), following the assignments of X anywhere in the
    enclosing top-level function (or the module)."""
    head = None
    if isinstance(topic, ast.JoinedStr) and topic.values and isinstance(topic.values[0], ast.FormattedValue):
        head = topic.values[0].value
    elif isinstance(topic, ast.BinOp) and isinstance(topic.op, ast.Add):
        head = topic.left
        while isinstance(head, ast.BinOp):
            head = head.left
    if not (isinstance(head, ast.Attribute) and head.attr == '__name__'):
        return None
    candidates = [head.value]
    if isinstance(head.value, ast.Name):
        candidates = [n.value for n in ast.walk(scope) if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == head.value.id for t in n.targets)] + candidates
    for target in candidates:
        found = repo.resolveExpr(module, target)
        if found and found[0] == 'func':
            return (found[1].rel, found[2].lineno)
    return None


def _handlers(section: Section, hatchErrors: list):
    repo = section.repo
    module = repo.module('PubSubRequests.py')
    if module and RETIRED_CONTRACT_GAPS in module.names:
        hatchErrors.append(f'server/PubSubRequests.py: {RETIRED_CONTRACT_GAPS} is no longer supported: fix '
                           'the mismatch at its source (python pubSubMap.py --explain <handler>), then delete '
                           'the dict')
    if module and RETIRED_MESSAGE_MODELS in module.names:
        hatchErrors.append(f'server/PubSubRequests.py: {RETIRED_MESSAGE_MODELS} is no longer supported: each '
                           'handler parses its message with its generated <Fn>Message (python pubSubModels.py '
                           '--pull writes objects_<publisher>.py); then delete the dict')
    entry = module.names.get('PubSubRequests') if module else None
    if not entry or not isinstance(entry[1], ast.ClassDef):
        return {}, []
    psr = repo.classInfo(module, entry[1])
    resolver = ContractResolver(repo)
    handlers, utility, seen = {}, [], set()
    for c in repo.mro(psr):
        if not isinstance(c, ClassInfo):
            continue
        for name, node in c.methods.items():
            if name in seen or name.startswith('_') or not decoratorCall(node, 'route_config'):
                continue
            seen.add(name)
            if '_' not in name:
                utility.append(name)
                continue
            topicFn, source = name.rsplit('_', 1)
            reqs, truncated = resolver.handlerContract(psr, c.module, node)
            section.requirements[name] = reqs
            section.lines[('handler', name)] = (c.module.rel, node.lineno, None)
            handlers[name] = {
                'anchor': f'{c.module.rel}::{c.key[1]}.{name}',
                'source': source,
                'topicFn': topicFn,
                'requirements': [r.toJson() for r in reqs],
                'resolved': any(r.kind != 'dynamicKey' for r in reqs),
                'truncated': truncated,
            }
    return dict(sorted(handlers.items())), utility

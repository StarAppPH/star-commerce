"""App / API / TypeScript generation utilities.
Cleaner refactor of the original AppCreator script.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import types
import typing
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, get_args, get_origin
import datetime

from sharedConstantsTS import renderSharedConstantsTS
from pydantic import BaseModel, RootModel
from utils import annotationLeaves, annotationOrigins

# `--constants-only` has to work on a bare checkout. The imports below drag in
# the whole app chain: ApiRequests -> objects.py -> mongoDb, which instantiates
# mainDb() at module scope and calls AppConfig().getEnvironment(). AppConfig
# raises "Invalid environment" when ENVIRONMENT is unset, and its PubSubConfig
# additionally raises for every service under server/services/ that has no
# PUBSUB_ENABLED_* var. So the flag is read BEFORE them. Nothing on the
# constants path needs any of them, and `from __future__ import annotations`
# keeps the `apiRequests: ApiRequests` annotation below lazy.
_CONSTANTS_ONLY = __name__ == '__main__' and '--constants-only' in sys.argv[1:]

# ----------------------------- Global Path Constants ----------------------------- #


def _compute_project_root(start: Path) -> Path:
    """Ascend directories until a .git folder (project root) is found or stop at filesystem root."""
    cur = start
    while True:
        if (cur / '.git').exists() or cur.parent == cur:
            return cur
        cur = cur.parent


PORT: int = 5003
PROJECT_ROOT: Path = _compute_project_root(Path(__file__).resolve().parent)
SERVER_DIR: Path = PROJECT_ROOT / 'server'
CLIENT_DIR: Path = PROJECT_ROOT / 'client'
CLIENT_APP_DIR: Path = PROJECT_ROOT / 'client/app'
CLIENT_SCHEMAS_DIR: Path = CLIENT_APP_DIR / 'schemas'
SERVER_SCHEMAS_DIR: Path = SERVER_DIR / 'schemas'
CLIENT_API_DIR: Path = CLIENT_APP_DIR / 'api'
# client/lib, not CLIENT_APP_DIR: the client is scaffolded with
# --no-src-dir, so the '@/' alias resolves to client/ and
# '@/lib/generated/serverConstants' lands here.
CLIENT_GENERATED_DIR: Path = CLIENT_DIR / 'lib' / 'generated'
CLIENT_CONSTANTS_TS_PATH: Path = CLIENT_GENERATED_DIR / 'serverConstants.ts'

# Ensure required directories exist where appropriate
CLIENT_SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
SERVER_SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
CLIENT_API_DIR.mkdir(parents=True, exist_ok=True)
CLIENT_GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------- Pub/Sub models, then the app ----------------------------- #


def _pubSubStep(name: str, step):
    """step() of pubSubModels.py or pubSubMap.py: their hard error (MapError:
    an unparsable source file, a bad sibling manifest, a publisher without a
    payload model, a sibling the map would lose) stops the run with its
    message. Imported here so that --constants-only stays import-free."""
    from pubSubMap import MapError
    try:
        return step()
    except MapError as e:
        raise SystemExit(f'{name}: {e}') from None


# The generated objects_<publisher>.py files (pubSubModels.py) are imported by
# PubSubRequests and the actions, so they are refreshed before that import.
# __main__ only (tests import this module); skipped for --constants-only and
# for an unknown argument, which is refused below.
if __name__ == '__main__' and not sys.argv[1:]:
    from pubSubModels import pull
    for _line in _pubSubStep('Pub/Sub models', lambda: pull(PROJECT_ROOT)):
        print(f'PUBSUB MODELS: {_line}')

if not _CONSTANTS_ONLY:
    from ApiRequests import ApiRequests
    from PubSubRequests import PubSubRequests
    from AppConfig import SECRET_RESPONSE_KEYS
    from route_config import HTTP_METHODS
    from responseModelCheck import assertPublisherModels, responseModelProblems

# ----------------------------- Generated Code Config ----------------------------- #
# These lists control what gets written into the generated app.py and appPubSub.py.
# You can add or remove imports and config lines here.

APP_IMPORTS = [
    'from flask import Flask, request, jsonify',
    'from flask_cors import CORS',
    'from flask_compress import Compress',
    'from AppConfig import AppConfig',
    'from objects import *',
    'from ApiRequests import ApiRequests',
    'import os',
    'import logging',
    'from appPubSub import main',
    'from flask_jwt_extended import (JWTManager, create_access_token, jwt_required, get_jwt_identity)',
    '#from Settings import DatabaseSettingUpdater',
    'from datetime import timedelta',
    'from routeErrors import routeFailure',
    'from callerAuth import requireServiceCaller',
]

APP_PUBSUB_IMPORTS = [
    'from flask import Blueprint, jsonify, request',
    'import logging',
    'from mongoDb import db',
    'from PubSubRequests import PubSubRequests',
    'from routeErrors import routeFailure',
    'from pubSub import decodeMessage',
    'from objects import ApiResponse',
    'from callerAuth import requirePushCaller',
]

APP_FLASK_CONFIG = [
    'app = Flask(__name__)',
    'app.register_blueprint(main)',
    'if os.getenv("JWT_SECRET_KEY") == None:',
    '    raise ValueError("JWT_SECRET_KEY environment variable is not set")',
    '# Outside localdev the signing key must be a real secret: anyone who knows it can mint a token for any user.',
    'if os.getenv("ENVIRONMENT") != "localdev" and (len(os.getenv("JWT_SECRET_KEY")) < 32 or os.getenv("JWT_SECRET_KEY") == "change-me-to-a-secret-key"):',
    '    raise ValueError("JWT_SECRET_KEY must be a random secret of at least 32 characters outside localdev")',
    'app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")',
    'app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=5)',
    '# The largest request body Flask accepts (a larger one is refused with 413). Raise it if a route takes uploads.',
    'app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024',
    'jwt = JWTManager(app)',
    'CORS(app, resources={r"/*": {"origins": "*"}})',
    # Response compression (flask-compress): brotli at quality 4, else gzip at level 6, for JSON and CSV
    # bodies of 500 bytes or more, when the client's Accept-Encoding allows it. COMPRESS_REGISTER is off:
    # the _compressResponse hook at the end of this file runs it, except for NO_COMPRESSION_ENDPOINTS.
    'app.config["COMPRESS_ALGORITHM"] = ["br", "gzip"]',
    'app.config["COMPRESS_ALGORITHM_STREAMING"] = ["br"]',
    'app.config["COMPRESS_BR_LEVEL"] = 4',
    'app.config["COMPRESS_LEVEL"] = 6',
    'app.config["COMPRESS_MIN_SIZE"] = 500',
    'app.config["COMPRESS_MIMETYPES"] = ["application/json", "text/csv"]',
    'app.config["COMPRESS_REGISTER"] = False',
    # Kill switch: COMPRESS_DISABLED=1 (or true, yes, on) in the environment, read at start-up, turns
    # compression off without a code deploy (set it on the Cloud Run service).
    'app.config["COMPRESS_DISABLED"] = (os.getenv("COMPRESS_DISABLED") or "").strip().lower() in ("1", "true", "yes", "on")',
    'compress = Compress(app)',
    'logging.basicConfig(level=logging.INFO)',
]

APP_PUBSUB_CONFIG = [
    "main = Blueprint('main', __name__)",
    'logging.basicConfig(level=logging.INFO)',
]

# app.py's local bind (localdev and localprod; Cloud Run binds 0.0.0.0:8080).
# Loopback unless server/.env sets LOCAL_BIND_HOST: localdev accepts a sign-in
# with a raw uid and email, and devLogin signs in as any user, so a server
# reachable from the LAN lets anyone on it act as any user (against the
# restored prod dump, in localprod).
LOCAL_BIND_HOST_CODE = r"""

def _localBindHost() -> str:
    # The host a local run binds: 127.0.0.1, unless LOCAL_BIND_HOST (in
    # server/.env) names another, e.g. 0.0.0.0 to reach it from the LAN.
    host = (os.getenv("LOCAL_BIND_HOST") or "").strip() or "127.0.0.1"
    if host != "127.0.0.1":
        line = "!" * 78
        print(f"\033[91m{line}\n"
              f"LOCAL_BIND_HOST={host}: this server is reachable from your network.\n"
              "localdev accepts a sign-in with a raw uid and email, and devLogin signs\n"
              "in as any user: anyone who can reach it can act as any user.\n"
              f"{line}\033[0m")
        logging.warning({'event': 'localBindHostExposed', 'host': host})
    return host
"""

# Envelope statuses a browser can see. The generated Flask routes answer with
# the first three (_function_code); the client produces the last two itself
# (_ts_method_code). ApiResponse.ts derives its status literals from these same
# numbers, so the TS union cannot drift from what the code actually sends.
STATUS_OK = 200
# A ValueError the route raised (the caller's to fix, routeErrors.py),
# including the body userId that is not the JWT identity ("Unauthorized
# access") and a role refusal.
STATUS_BAD_REQUEST = 400
STATUS_FORBIDDEN = 403  # PermissionError
# routeErrors.py answers a server fault with a real 5xx (500, or 503 for a
# sibling service), so Cloud Run counts it. The generated client hands any
# status from this one up to the page as STATUS_FETCH_FAILED, with the
# server's message: a page cannot fix a server fault either.
STATUS_SERVER_ERROR = 500
STATUS_SERVICE_UNAVAILABLE = 503  # a dead letter another delivery is handling (Pub/Sub retries)
# flask_jwt_extended rejects a missing, expired or invalid token with its own
# {"msg": ...} body (401, or 422 for a malformed one); the client turns every
# such body into this single envelope.
STATUS_JWT_REJECTED = 401
STATUS_FETCH_FAILED = 0  # the client's catch: network error or non-JSON body
FAILURE_STATUSES = (STATUS_FETCH_FAILED, STATUS_BAD_REQUEST,
                    STATUS_JWT_REJECTED, STATUS_FORBIDDEN)

# client/app/api/ApiResponse.ts. The envelope every route returns, as a
# union discriminated on `status`, so checking `status === 200` narrows `data`
# to the route's responseModel. The schema's own ApiResponse types `data` as an
# opaque value and `status` as any number, so both are replaced here. No route
# answers a failure with data, so every failure arm has `data: null`.
API_RESPONSE_TS = f"""/**
 * This file was automatically generated by server/AppCreator.py.
 * DO NOT MODIFY IT BY HAND. Instead, modify server/AppCreator.py, and run
 * `cd server && ENVIRONMENT=localdev python AppCreator.py` to regenerate this file.
 */
import type {{ ApiResponse as ApiEnvelope }} from '../schemas/ApiResponseSchema';

type EnvelopeMeta = Omit<ApiEnvelope, 'data' | 'status'>;

/** The route succeeded: `data` is its responseModel. */
export interface ApiSuccess<T> extends EnvelopeMeta {{ status: {STATUS_OK}; data: T }}

/**
 * The route refused the request ({STATUS_BAD_REQUEST}, or {STATUS_FORBIDDEN} for a permission error), the JWT
 * was rejected ({STATUS_JWT_REJECTED}), or the request failed ({STATUS_FETCH_FAILED}: a network error, a body
 * that is not JSON, or a server fault, whose generic message carries an error id).
 */
export interface ApiFailure extends EnvelopeMeta {{ status: {' | '.join(str(s) for s in FAILURE_STATUSES)}; data: null }}

export type ApiResponse<T> = ApiSuccess<T> | ApiFailure;
"""

# ----------------------------- Bare responses ----------------------------- #
# Every route answers a dict with explicit keys, so its responseModel is a
# plain model. The ONLY exceptions are the routes listed here: routes other
# services ALREADY call over HTTP and read `data` as the bare list or number
# itself, so their answer can never be wrapped (a cross-repo shape never
# changes). Each one declares a RootModel describing that bare value, mapped
# here to why it is bare and who reads it, e.g.
#     'viewInventoryForPartnerService':
#     'a bare list of inventory rows (InventoryList): partner '
#     'job_syncInventory.py reads data["data"] as the list',
# A RootModel on any other route is a generation error, and
# test_route_response_models.py pins this map to exactly the routes declaring
# one. Empty in a new project: never add a NEW route here to avoid wrapping
# its answer.
CROSS_REPO_BARE_RESPONSE_ROUTES: Dict[str, str] = {}

# ----------------------------- Type Helpers ----------------------------- #

# Untyped Python containers map to `unknown`, never `any`: an `unknown` forces
# the caller to narrow, while `any` silently switches type checking off. Prefer
# a real model over a bare dict anyway — a TS interface is not assignable to
# Record<string, unknown>.
BASIC_PY_TO_TS: Dict[Any, str] = {
    int: "number",
    float: "number",
    str: "string",
    bool: "boolean",
    dict: "Record<string, unknown>",
    list: "unknown[]",
    Any: "unknown",
    type(None): "null",
}


def py_annotation_to_ts(ann: Any) -> str:
    """Convert a Python type annotation to a TypeScript type string."""
    if ann is inspect._empty:
        return BASIC_PY_TO_TS[Any]

    if ann in BASIC_PY_TO_TS:
        return BASIC_PY_TO_TS[ann]

    # typing constructs
    origin = get_origin(ann)
    args = get_args(ann) or ()

    # Literal[...] support -> union of literal values in TS
    if origin is typing.Literal:
        lit_parts: List[str] = []
        for a in args:
            if isinstance(a, str):
                lit_parts.append(f"'{a}'")
            elif a is None:
                lit_parts.append('null')
            elif isinstance(
                    a,
                    bool):  # booleans before int since bool is subclass of int
                lit_parts.append('true' if a else 'false')
            elif isinstance(a, (int, float)):
                lit_parts.append(str(a))
            else:  # fallback for unsupported literal types
                lit_parts.append(BASIC_PY_TO_TS[Any])
        # Deduplicate while preserving order
        seen = set()
        deduped = [p for p in lit_parts if not (p in seen or seen.add(p))]
        return " | ".join(deduped) if deduped else 'never'

    # List / list
    if origin in (list, typing.List):
        return _ts_array_of(args[0]) if args else BASIC_PY_TO_TS[list]

    # Dict
    if origin in (dict, typing.Dict):
        if len(args) == 2:
            return f"Record<{py_annotation_to_ts(args[0])}, {py_annotation_to_ts(args[1])}>"
        return BASIC_PY_TO_TS[dict]

    # Tuple
    if origin in (tuple, typing.Tuple):
        if not args:
            return BASIC_PY_TO_TS[list]
        if len(args) == 2 and args[1] is Ellipsis:
            return _ts_array_of(args[0])
        return f"[{', '.join(py_annotation_to_ts(a) for a in args)}]"

    # Union / Optional (flatten nested unions, dedupe)
    if origin in (typing.Union, types.UnionType):  # PEP 604 | support
        flat: List[Any] = []

        def _flatten(a: Any):
            o = get_origin(a)
            if o in (typing.Union, types.UnionType):
                for sub in get_args(a):
                    _flatten(sub)
            else:
                flat.append(a)

        for a in args:
            _flatten(a)
        rendered: List[str] = []
        seen: set[str] = set()
        for a in flat:
            ts = py_annotation_to_ts(a)
            if ts not in seen:
                seen.add(ts)
                rendered.append(ts)
        return " | ".join(rendered) if rendered else BASIC_PY_TO_TS[Any]

    # Forward ref as string
    if isinstance(ann, str):
        return ann

    # Fallback
    try:
        return ann.__name__
    except AttributeError:
        return BASIC_PY_TO_TS[Any]


def _ts_array_of(item: Any) -> str:
    """The TS array of `item`. A union element is parenthesized: `[]` binds
    tighter than `|`, so `string | null[]` would mean a string or an array of
    nulls."""
    ts = py_annotation_to_ts(item)
    if (get_origin(item) in (typing.Union, types.UnionType, typing.Literal)
            and ' | ' in ts):
        ts = f"({ts})"
    return f"{ts}[]"


# The params pubSubDecorator and app.py set, never sent by the client: no part
# of the TS method, the request body or the logged request keys.
_PUBLISHER_FLAGS = ('publishToProdPubSub', 'publishToPubSubMockDb')

# What a scalar route param (str, int, float, bool, a Literal, or an Optional
# of one) may be built from. app.py hands a route the raw JSON value, so a
# client could send an object or a list instead: the generated handler refuses
# one for a scalar param (S1: {"todoId": {"$ne": null}} reached a Mongo query
# as an operator).
_SCALAR_LEAVES = (str, int, float, bool, type(None))
_SCALAR_ORIGINS = (typing.Union, types.UnionType, typing.Literal)


def _is_scalar_annotation(ann: Any) -> bool:
    if ann is inspect._empty:
        return False
    return (all(origin in _SCALAR_ORIGINS for origin in annotationOrigins(ann))
            and all(leaf in _SCALAR_LEAVES for leaf in annotationLeaves(ann)))


def _shape_of_annotation(ann: Any) -> tuple | None:
    """The JSON shape a param annotated `ann` must arrive as, and whether None
    may stand for it: ('dict', optional) for a dict or a model, ('list',
    optional) for a list, tuple or set (a RootModel by its root). None for a
    scalar, Any or a mix of shapes, which the shape guard leaves alone."""
    import typing
    from pydantic import RootModel
    if ann is inspect._empty:
        return None
    if get_origin(ann) in (typing.Union, types.UnionType):
        members = get_args(ann)
    else:
        members = (ann, )
    shapes = set()
    for member in members:
        if member is type(None):
            continue
        origin = get_origin(member) or member
        if isinstance(origin, type) and issubclass(origin, RootModel):
            root = origin.model_fields['root'].annotation
            origin = get_origin(root) or root
        if origin is dict or (isinstance(origin, type)
                              and issubclass(origin, BaseModel)):
            shapes.add('dict')
        elif origin in (list, tuple, set, frozenset):
            shapes.add('list')
        else:
            return None
    if len(shapes) != 1:
        return None
    return shapes.pop(), type(None) in members

# Annotations that carry no type information. As a route param they would
# reach the browser as `unknown`, so the generator refuses them; a typed
# container such as dict[str, int] is fine.
_UNTYPED_PARAM_ANNOTATIONS = (dict, list, tuple, Any)

# The generic origins a route param may be built from: the ones
# py_annotation_to_ts renders. Anything else (set[str], Annotated[...],
# Sequence[str]) falls through to its bare __name__ and emits a TS type that
# does not exist.
_ALLOWED_PARAM_ORIGINS = (list, dict, tuple, typing.Union, types.UnionType,
                          typing.Literal)


# ----------------------------- JSON schema helpers ----------------------------- #


def _wrap_sibling_refs(node: Any) -> Any:
    """Return `node` with every {"$ref": X, ...siblings} rewritten as
    {"allOf": [{"$ref": X}], ...siblings}. Pure.

    pydantic puts a field's description next to its $ref, and json2ts reads
    a $ref with siblings as a NEW type: it emitted a duplicate `User1`
    interface instead of reusing the referenced one. Wrapped in allOf, the
    field is typed with the referenced interface itself and keeps its
    description as a doc comment."""
    if isinstance(node, list):
        return [_wrap_sibling_refs(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _wrap_sibling_refs(value) for key, value in node.items()}
    if '$ref' in out and len(out) > 1:
        ref = out.pop('$ref')
        out = {'allOf': [{'$ref': ref}], **out}
    return out


def _schema_property_names(schema: Any) -> set:
    """Every property name a JSON schema declares, at any depth: the model's
    own properties, every $defs entry and any inline nested object."""
    names: set = set()
    if isinstance(schema, dict):
        properties = schema.get('properties')
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names |= _schema_property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= _schema_property_names(item)
    return names


@functools.cache
def _secret_keys_typed_by(model: type) -> frozenset:
    """The SECRET_RESPONSE_KEYS a model's generated TS type would declare.
    ApiResponse strips those keys from every response, so a client typed to
    expect one would read undefined. Cached: _validate_route_meta runs for
    every route on each generator pass. Inert while SECRET_RESPONSE_KEYS
    (AppConfig.py) is empty."""
    if not SECRET_RESPONSE_KEYS:
        return frozenset()
    schema = model.model_json_schema(mode='serialization')
    return frozenset(_schema_property_names(schema) & SECRET_RESPONSE_KEYS)


# ----------------------------- Utility Classes ----------------------------- #


@dataclass
class Parameters:
    names: List[str] = field(default_factory=list)

    def as_assignment_block(self, indent: int = 4, kind: str = 'app') -> str:
        if not self.names:
            return ""
        pad = " " * indent
        if kind == 'appPubSub':
            return ""
        if kind == 'app':
            dictName = 'data'
            return "\n".join(f"{pad}{n} = {dictName}.get('{n}')"
                             for n in self.names if n not in _PUBLISHER_FLAGS) + "\n"

    def comma_join(self) -> str:
        return ", ".join(self.names)

    def body_names(self) -> tuple:
        """The params the client sends in the body: every one but the
        publisher flags."""
        return tuple(n for n in self.names if n not in _PUBLISHER_FLAGS)


# ----------------------------- Shared constants sync ----------------------------- #
# sharedConstantsTS.renderSharedConstantsTS renders sharedConstants.py's public
# values as TypeScript, with the guards that keep a secret or an unserializable
# value out of a file every browser downloads.


def writeSharedConstantsTS() -> None:
    """Write the rendered constants to client/lib/generated/serverConstants.ts."""
    code = renderSharedConstantsTS()
    CLIENT_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    # newline='\n' is load-bearing: default mode writes CRLF on Windows and
    # the drift test compares this exact text.
    with open(CLIENT_CONSTANTS_TS_PATH, 'w', encoding='utf-8',
              newline='\n') as f:
        f.write(code)
    print(f'Generated {CLIENT_CONSTANTS_TS_PATH.relative_to(PROJECT_ROOT)}')


# ----------------------------- AppCreator ----------------------------- #


class AppCreator:
    """Generates Flask routes, TypeScript request wrappers, and schemas."""

    def __init__(self, apiRequests: ApiRequests):
        self.apiRequests = apiRequests
        self._objectsClassesCache = None

    # ----------------------------- Introspection helpers ----------------------------- #
    def _get_user_defined_methods(self, kind: str):
        requestClass = ApiRequests if kind == 'app' else PubSubRequests
        methods = {}
        for cls in requestClass.__mro__:
            for name, func in cls.__dict__.items():
                if callable(func) and not name.startswith('_'):
                    methods.setdefault(name, func)
        return methods

    def _route_meta(self, func) -> Dict[str, Any]:
        return {
            'httpMethod': getattr(func, 'httpMethod', None),
            'jwtRequired': getattr(func, 'jwtRequired', None),
            'createAccessToken': getattr(func, 'createAccessToken', None),
            'successMessage': getattr(func, 'successMessage', None),
            # A route only another service calls (callerAuth.py); absent on
            # a push handler, which is always one.
            'serviceOnly': getattr(func, 'serviceOnly', False),
            # None on a PubSubRequests push handler (an ApiRequests route
            # without one is refused by _validate_route_meta).
            'responseModel': getattr(func, 'responseModel', None),
        }

    def _validate_route_meta(self, name: str, meta: Dict[str, Any],
                             fileName: str):
        if meta['httpMethod'] not in HTTP_METHODS:
            raise ValueError(
                f"please set the http method of {name} in {fileName} file")
        if meta['jwtRequired'] not in [True, False]:
            raise ValueError(f"please set the jwtRequired in {fileName} file")
        if meta['createAccessToken'] not in [True, False]:
            raise ValueError(
                f"please set the createAccessToken in {fileName} file")
        if not isinstance(meta['serviceOnly'], bool):
            raise ValueError(f"please set the serviceOnly in {fileName} file")
        if meta['serviceOnly'] and (meta['jwtRequired']
                                    or meta['createAccessToken']):
            raise ValueError(
                f"{name} in {fileName}: a serviceOnly route takes no JWT "
                "(its caller is a service, verified by callerAuth.py)")
        responseModel = meta.get('responseModel')
        # Only an ApiRequests route answers the typed client. A
        # PubSubRequests push handler's return value goes back to Pub/Sub
        # alone, so it may declare none.
        if responseModel is None and fileName == 'ApiRequests':
            raise ValueError(
                f"{name} in {fileName} declares no responseModel: add "
                "@route_config(responseModel=<objects.py model>), which "
                "types the generated client method and the response check")
        if (isinstance(responseModel, type)
                and issubclass(responseModel, RootModel)
                and name not in CROSS_REPO_BARE_RESPONSE_ROUTES):
            raise ValueError(
                f"responseModel of {name} in {fileName} is a RootModel "
                f"({responseModel.__name__}): a route answers a dict with "
                "explicit keys, so wrap the list or value in a model field. "
                "Only a route other services read as a bare value may answer "
                "one, listed with its consumers in "
                "CROSS_REPO_BARE_RESPONSE_ROUTES")
        if (responseModel is not None
                and responseModel not in self._objects_classes()):
            raise ValueError(
                f"responseModel of {name} in {fileName} must be a top-level "
                f"class defined in objects.py (got "
                f"{getattr(responseModel, '__module__', '?')}."
                f"{getattr(responseModel, '__qualname__', responseModel)}): "
                f"only objects.py classes get a generated TS schema")
        if responseModel is not None and fileName == 'ApiRequests':
            problems = responseModelProblems(responseModel)
            if problems:
                raise ValueError(
                    f"responseModel {responseModel.__name__} of {name} in "
                    f"{fileName} must be a ResponseModel all the way down "
                    "(objects.ResponseModel, never a database model):\n  - "
                    + '\n  - '.join(problems))
        if responseModel is not None:
            typedSecrets = _secret_keys_typed_by(responseModel)
            if typedSecrets:
                raise ValueError(
                    f"responseModel {responseModel.__name__} of {name} in "
                    f"{fileName} types {sorted(typedSecrets)}, which "
                    "ApiResponse strips from every response "
                    "(SECRET_RESPONSE_KEYS): declare it SkipJsonSchema[...] "
                    "so the client is never typed to expect it")

    def _objects_classes(self) -> list:
        """The classes of the real `objects` module, the one routes import
        from. generateTypeScriptSchemas emits a `<Name>Schema.ts` for each of
        its pydantic models, so these are the only class names that resolve
        in the client. Read once per AppCreator."""
        if self._objectsClassesCache is None:
            self._objectsClassesCache = self._classes_from_module(
                importlib.import_module('objects'))
        return self._objectsClassesCache

    def _objects_models(self) -> list:
        """The pydantic models among the objects.py classes: the ones that
        get a generated `<Name>Schema.ts`."""
        return [
            cls for cls in self._objects_classes()
            if issubclass(cls, BaseModel)
        ]

    # ----------------------------- Parameter helpers ----------------------------- #
    def _parameters(self, func: Callable) -> Parameters:
        names = [p for p in inspect.signature(func).parameters if p != 'self']
        return Parameters(names=names)

    def _validate_param_annotation(self, funcName: str, name: str,
                                   ann: Any) -> None:
        for origin in annotationOrigins(ann):
            if origin not in _ALLOWED_PARAM_ORIGINS:
                raise ValueError(
                    f"Function {funcName} parameter '{name}' is annotated "
                    f"{ann}, which uses {getattr(origin, '__name__', origin)}"
                    ": a route param may only be built from list, dict, "
                    "tuple, Literal and unions (X | None), the containers "
                    "the generated TS client can express")
        objectsClasses = self._objects_classes()
        for leaf in annotationLeaves(ann):
            if isinstance(leaf, type) and issubclass(
                    leaf, (datetime.date, datetime.time)):
                # app.py hands the route the raw JSON value, so the route
                # would get a string while the TS client sends a Date.
                raise ValueError(
                    f"Function {funcName} parameter '{name}' is annotated "
                    f"{ann}: a route param cannot be a {leaf.__name__}, "
                    "because the generated app.py passes the raw JSON value "
                    "(a string). Annotate it `str` (an ISO 8601 string) and "
                    "parse it in the route")
            if any(leaf is untyped for untyped in _UNTYPED_PARAM_ANNOTATIONS):
                raise ValueError(
                    f"Function {funcName} parameter '{name}' is annotated "
                    f"{ann}, which has a bare {getattr(leaf, '__name__', leaf)}"
                    ": give it a model from objects.py or a typed container "
                    "such as dict[str, X]")
            if leaf in BASIC_PY_TO_TS:
                continue
            if leaf not in objectsClasses:
                raise ValueError(
                    f"Function {funcName} parameter '{name}' uses "
                    f"{getattr(leaf, '__module__', '?')}."
                    f"{getattr(leaf, '__qualname__', leaf)}, which must be a "
                    "top-level class defined in objects.py: only objects.py "
                    "classes get a generated TS schema")

    def _scalar_params(self, func: Callable) -> List[str]:
        """The client-sent params annotated as a scalar (str, int, float,
        bool, a Literal, or an Optional of one): the generated handler
        refuses an object or a list for each."""
        return [
            name for name, param in inspect.signature(func).parameters.items()
            if name != 'self' and name not in _PUBLISHER_FLAGS
            and _is_scalar_annotation(param.annotation)
        ]

    def _shape_params(self, func: Callable) -> List[tuple]:
        """(name, 'dict' or 'list', None allowed) for each client-sent param
        annotated as a dict, a model or a list: the generated handler refuses
        any other value (a 400 "Invalid <param>"), so a bad shape never
        reaches the route as a TypeError (a 500). None is allowed for an
        Optional param or one with a default."""
        out = []
        for name, param in inspect.signature(func).parameters.items():
            if name == 'self' or name in _PUBLISHER_FLAGS:
                continue
            shape = _shape_of_annotation(param.annotation)
            if shape is not None:
                kind, optional = shape
                out.append((name, kind, optional
                            or param.default is not inspect._empty))
        return out

    def _params_with_annotations(self, func: Callable) -> Dict[str, Any]:
        """The route's client-sent params and their annotations, each
        checked by the param guards."""
        sig = inspect.signature(func)
        result: Dict[str, Any] = {}
        for name, param in sig.parameters.items():
            if name == 'self' or name in _PUBLISHER_FLAGS:
                continue
            ann = param.annotation
            if ann is inspect._empty:
                raise ValueError(
                    f"Function {func.__name__} parameter '{name}' missing type annotation"
                )
            self._validate_param_annotation(func.__name__, name, ann)
            if (param.default is None
                    and type(None) not in annotationLeaves(ann)):
                # The TS param would not admit null, so the default the
                # route relies on could never be sent.
                raise ValueError(
                    f"Function {func.__name__} parameter '{name}' defaults to "
                    f"None but is annotated {ann}: annotate it "
                    f"`{ann.__name__ if isinstance(ann, type) else ann} | None`")
            result[name] = ann
        return result

    # ----------------------------- Flask generation ----------------------------- #
    def _imports(self, kind: str) -> str:
        imports = APP_IMPORTS if kind == 'app' else APP_PUBSUB_IMPORTS
        return "\n".join(imports) + "\n\n"

    def _flask_config(self, kind: str) -> str:
        cfg = APP_FLASK_CONFIG if kind == 'app' else APP_PUBSUB_CONFIG
        return "\n".join(cfg) + "\n\n"

    def _compression_block(self, kind: str) -> str:
        """app.py's response-compression hook. flask-compress is configured in
        APP_FLASK_CONFIG with COMPRESS_REGISTER off, so this after_request hook
        is what compresses: every response unless COMPRESS_DISABLED is set
        (logged once at start-up as compressionDisabled),
        except those of
        NO_COMPRESSION_ENDPOINTS, the routes that issue an access token
        (createAccessToken). Such a response carries a fresh token next to
        sign-in data the caller sent, the pattern BREACH exploits, and is small
        anyway. The sliding jwtToken every other authenticated route returns
        is compressed: the client sends its token in an Authorization header it
        sets from localStorage, never in a cookie, so no other site can make a
        victim's browser send an authenticated request it partly controls,
        which BREACH needs."""
        if kind != 'app':
            return ''
        exempt = sorted(name for name, func in self._get_user_defined_methods('app').items()
                        if self._route_meta(func)['createAccessToken'])
        return ('\n\n# Never compressed: the routes that issue an access token (BREACH; see\n'
                '# AppCreator._compression_block).\n'
                f'NO_COMPRESSION_ENDPOINTS = frozenset({exempt!r})\n\n'
                'if app.config["COMPRESS_DISABLED"]:\n'
                "    logging.warning({'event': 'compressionDisabled'})\n\n\n"
                '@app.after_request\n'
                'def _compressResponse(response):\n'
                '    if app.config["COMPRESS_DISABLED"] or request.endpoint in NO_COMPRESSION_ENDPOINTS:\n'
                '        return response\n'
                '    return compress.after_request(response)\n')

    def _startup_check_block(self, kind: str) -> str:
        """app.py's startupCheck route, the path of Cloud Run's HTTP startup
        probe (cloudbuild.yaml's deploy-service step). gunicorn binds the port
        before its worker imports app.py, so a TCP probe passes at once and the
        import then runs on throttled CPU (request-based billing). This route
        answers only once app.py has fully imported, so startup, which has full
        CPU, lasts until the app is loaded. A plain Flask route: no
        route_config, JWT, caller check, database or client method. Its body
        is under COMPRESS_MIN_SIZE, so _compressResponse leaves it as is."""
        if kind != 'app':
            return ''
        return ("\n\n# Cloud Run's HTTP startup probe (cloudbuild.yaml) calls this. It answers\n"
                '# only once app.py has fully imported, so startup, which has full CPU,\n'
                '# lasts until the app is loaded (see AppCreator._startup_check_block).\n'
                "@app.route('/startupCheck', methods=['GET'])\n"
                'def startupCheck():\n'
                "    return jsonify({'status': 'ok'}), 200\n")

    def _error_handler_block(self, kind: str) -> str:
        """app.py's handler for an exception raised outside a route's try
        (building or serializing the response, a hand-written blueprint):
        answered and logged once by routeErrors like any route's failure,
        never Flask's HTML 500 page or a raw traceback in the log. An
        HTTPException (a 404, a 405) is answered as Flask would; the more
        specific handlers flask_jwt_extended registers win over this one."""
        if kind != 'app':
            return ''
        return (
            '\n\nfrom werkzeug.exceptions import HTTPException\n\n\n'
            '@app.errorhandler(Exception)\n'
            'def _unhandledError(e):\n'
            '    if isinstance(e, HTTPException):\n'
            '        return e\n'
            '    failure = routeFailure(e, status=403 if isinstance(e, PermissionError) else 400)\n'
            "    logging.log(failure.level, {'function': request.endpoint, 'event': 'error', 'traceback': failure.traceback})\n"
            '    return failure.response()\n')

    def _main_function_block(self, kind: str) -> str:
        if kind != 'app':
            return ''
        return LOCAL_BIND_HOST_CODE + (f"""
if __name__ == '__main__':
    if (AppConfig().getIsDevEnvironment()):
        print(f"\033[92m_______________________{{AppConfig().getEnvironment().upper()}}_______________________\033[0m")
    if AppConfig().getIsProductionEnvironment():
        print(f"\033[91m_______________________{{AppConfig().getEnvironment().upper()}}_______________________\033[0m")

    #from Settings import DatabaseSettingUpdater
    #DatabaseSettingUpdater().updateDatabaseSettingsToDefault()

    if AppConfig().getisLocalEnvironment():
        app.run(debug=False, host=_localBindHost(), port={PORT})
    else:
        app.run(host='0.0.0.0', port=8080)
""")

    def _function_code(self, name: str, params: Parameters,
                       meta: Dict[str, Any], kind: str, method: str,
                       publishToProdPubSub: bool | None,
                       scalarParams: List[str] = (),
                       shapeParams: List[tuple] = ()) -> str:
        hasUserId = 'userId' in params.names
        atFunction = 'app' if kind == 'app' else 'main'
        className = 'ApiRequests' if kind == 'app' else 'PubSubRequests'

        # The body is bound before anything reads it, whatever was sent: a
        # POST without a JSON object (no body, another content type, a JSON
        # list) reads as {}. A route's params are then None and the route
        # answers with the 400 envelope; a push handler's envelope parse
        # fails inside the try, a 400 too. Never a 500 (an unbound `data` or
        # param used to raise outside the try).
        bind_data = '    data = request.get_json(silent=True)\n    if not isinstance(data, dict):\n        data = {}\n'
        parameters_code = params.as_assignment_block(indent=4, kind=kind)
        # A messageId already in PubSubMessages was handled: answer 200
        # without running the handler again (both push branches below).
        pubsub_dedupe = (
            f'        if messageId != None and db.read({{"_id": messageId}},"PubSubMessages",findOne=True):\n'
            f'            response = ApiResponse(message="Message already processed", status={STATUS_OK}, data=None)\n'
            '            return jsonify(response.model_dump(exclude_unset=True)), response.status\n')
        retry_later = ''
        if kind == 'appPubSub' and name == 'createDeadLetter':
            # A handler error marked retryLater (deadLetters.ReplayInProgress:
            # another delivery holds the claim) is no fault: a 503 so Pub/Sub
            # retries, logged at WARNING without a traceback.
            retry_later = (
                "        if getattr(e, 'retryLater', False):\n"
                "            logging.warning({'function': 'createDeadLetter', 'event': 'retryLater', 'messageId': messageId})\n"
                f"            response = ApiResponse(message='Another delivery is handling this dead letter; retry later', status={STATUS_SERVICE_UNAVAILABLE}, data=None)\n"
                "            return jsonify(response.model_dump(exclude_unset=True)), response.status\n")
            # The dead-letter receiver (a dead-letter dashboard): every
            # message the dead-letter topic pushes is stored, so a malformed
            # one must not be a 400 that loops back. The envelope is read
            # defensively (a missing or mistyped part is None, a payload that
            # does not decode arrives as its text) and the handler's params
            # bind by name: messageId, originalMessage, originalTopicPath and
            # the attributes Pub/Sub sets when it forwards a message:
            # sourceSubscription (CloudPubSubDeadLetterSourceSubscription, the
            # short id), sourceSubscriptionProject
            # (CloudPubSubDeadLetterSourceSubscriptionProject) and
            # deliveryCount (CloudPubSubDeadLetterSourceDeliveryCount).
            pubsub_init = '    messageId = None\n    originalMessage = None\n'
            pubsub_parse = (
                '        envelopeMessage = data.get("message") if isinstance(data.get("message"), dict) else {}\n'
                '        envelopeAttributes = envelopeMessage.get("attributes") if isinstance(envelopeMessage.get("attributes"), dict) else {}\n'
                '        messageId = envelopeMessage.get("messageId") if isinstance(envelopeMessage.get("messageId"), str) else None\n'
                '        originalTopicPath = envelopeAttributes.get("originalTopicPath")\n'
                '        sourceSubscription = envelopeAttributes.get("CloudPubSubDeadLetterSourceSubscription")\n'
                '        sourceSubscriptionProject = envelopeAttributes.get("CloudPubSubDeadLetterSourceSubscriptionProject")\n'
                '        deliveryCount = envelopeAttributes.get("CloudPubSubDeadLetterSourceDeliveryCount")\n'
                '        originalMessage = envelopeMessage.get("data")\n'
                '        if isinstance(originalMessage, str):\n'
                '            try:\n'
                '                originalMessage = decodeMessage(originalMessage)\n'
                '            except Exception:\n'
                '                pass\n'
                + pubsub_dedupe)
        elif kind == 'appPubSub':
            # Parsed inside the try, so a malformed push is a 400 (S5).
            pubsub_init = '    messageId = None\n    originalMessage = None\n'
            pubsub_parse = (
                '        if isinstance(data["message"]["data"],str):\n'
                '            data["message"]["data"] = decodeMessage(data["message"]["data"])\n'
                '        originalMessage = data["message"]["data"]\n'
                '        originalTopicPath=data["message"].get("attributes").get("originalTopicPath")\n'
                '        messageId = data["message"]["messageId"]\n'
                + pubsub_dedupe)
        else:
            pubsub_init = ''
            pubsub_parse = ''
        # An object or a list for a scalar param is refused (S1): a
        # {"$ne": null} must never reach a query as an operator.
        scalar_guard = ''.join(
            f"        if isinstance({n}, (dict, list)):\n            raise ValueError('Invalid {n}')\n"
            for n in scalarParams) if kind == 'app' else ''
        # A dict, model or list param must arrive as that JSON shape.
        shape_guard = ''.join(
            f"        if {f'{n} is not None and ' if noneAllowed else ''}"
            f"not isinstance({n}, {kind_}):\n"
            f"            raise ValueError('Invalid {n}')\n"
            for n, kind_, noneAllowed in shapeParams) if kind == 'app' else ''
        jwt_decorator = '@jwt_required()' if meta['jwtRequired'] else ''
        # A machine's route (callerAuth.py) checks its caller's ID token
        # before anything else runs: every push handler, and every
        # serviceOnly route.
        if kind == 'appPubSub':
            caller_decorator = '@requirePushCaller\n'
        elif meta['serviceOnly']:
            caller_decorator = '@requireServiceCaller\n'
        else:
            caller_decorator = ''
        current_user_code = '    current_user = get_jwt_identity()\n' if (
            meta['jwtRequired'] and not meta['createAccessToken']) else ''
        userIdAndAccessTokenCheck = '        if userId != current_user:\n            raise ValueError("Unauthorized access")\n' if hasUserId and (meta['jwtRequired'] and not meta['createAccessToken']) else ''

        access_token_code = '        access_token = create_access_token(identity=res["_id"])\n' if meta[
            'createAccessToken'] else ''
        # A request body carries customer data, passwords and bank numbers,
        # so no route logs its values. A route logs which of its own params
        # it was sent (never another key: those are the caller's free text).
        # A login route's body carries the Firebase ID token, and not even
        # its keys are worth logging: its logs carry no request data at all.
        # A push handler logs the Pub/Sub messageId and the keys of the
        # message it was handed.
        if kind == 'appPubSub':
            logged_data = ", 'messageId': messageId, 'messageKeys': sorted(originalMessage) if isinstance(originalMessage, dict) else None"
        elif meta['createAccessToken']:
            logged_data = ''
        else:
            bodyNames = params.body_names()
            keys = f"sorted(key for key in data if key in {bodyNames!r})" if bodyNames else '[]'
            logged_data = f", 'requestKeys': {keys}"
        logger_success = f"    logging.info({{'function': '{name}', 'event': 'success'{logged_data}}})" if method == 'POST' else ''
        # routeErrors.routeFailure decides how a failure is answered (its
        # docstring has the mapping): `status` is what the branch answers an
        # error that is the caller's to fix, and a server fault is a 5xx,
        # whatever it says. The handler logs it once, as its own entry plus a
        # traceback without the exception's message. Only Pub/Sub consumer
        # routes return that traceback to the caller: DeadLetterDashboard
        # reads it on replay.
        error_logged = logged_data if method == 'POST' else ''
        error_log = f"        logging.log(failure.level, {{'function': '{name}', 'event': 'error'{error_logged}, 'traceback': failure.traceback}})\n"
        push_arg = ', push=True' if kind == 'appPubSub' else ''
        envelope_arg = ', envelopeParsed=originalMessage is not None' if kind == 'appPubSub' else ''


        # Response code
        if kind == 'app':
            if meta['createAccessToken']:
                response_code = f'    response = ApiResponse(message="{meta["successMessage"]}", status={STATUS_OK}, data=res, access_token=access_token)\n    return jsonify(response.model_dump(exclude_unset=True)), response.status'
            elif meta['jwtRequired']:
                response_code = f'    jwtToken = create_access_token(identity=current_user)\n    response = ApiResponse(message="{meta["successMessage"]}", status={STATUS_OK}, current_user=current_user, data=res, jwtToken=jwtToken)\n    return jsonify(response.model_dump(exclude_unset=True)), response.status'
            else:
                response_code = f'    response = ApiResponse(message="{meta["successMessage"]}", status={STATUS_OK}, data=res)\n    return jsonify(response.model_dump(exclude_unset=True)), response.status'
        if kind == 'appPubSub':

            response_code = f'    if messageId != None:\n        try:\n            db.create({{"_id":messageId,"_version":0}},"PubSubMessages")\n        except:\n            pass\n    response = ApiResponse(message="{meta["successMessage"]}", status={STATUS_OK}, data=res)\n    return jsonify(response.model_dump(exclude_unset=True)), response.status'

        if kind == 'app':
            if publishToProdPubSub is None:
                body_call = f"{className}().{name}({params.comma_join()})" if method == 'POST' else f"{className}().{name}()"
            elif publishToProdPubSub is not None:
                parameters = ''
                for x in params.names:
                    if x == 'publishToProdPubSub':
                        parameters += f"publishToProdPubSub={publishToProdPubSub},"
                    elif x == 'publishToPubSubMockDb':
                        parameters += "publishToPubSubMockDb=False,"
                    else:
                        parameters += f"{x}={x},"
                body_call = f"{className}().{name}({parameters})" if params.names else f"{className}().{name}(data,publishToProdPubSub={str(publishToProdPubSub)})"
        if kind == 'appPubSub':
            if name == 'createDeadLetter':
                body_call = f"{className}().{name}({params.comma_join()})" if params.names else f"{className}().{name}(data)"
            else:
                body_call = f"{className}().{name}(originalMessage)"


        return f"""
@{atFunction}.route('/{name}', methods=['{meta['httpMethod']}'])
{caller_decorator}{jwt_decorator}
def {name}():
{bind_data}{parameters_code}{pubsub_init}{current_user_code}    try:
{pubsub_parse}{scalar_guard}{shape_guard}{userIdAndAccessTokenCheck}        res = {body_call}
{access_token_code}    except PermissionError as e:
        failure = routeFailure(e, status={STATUS_FORBIDDEN}{push_arg})
{error_log}        return failure.response()
    except Exception as e:
{retry_later}        failure = routeFailure(e, status={STATUS_BAD_REQUEST}{push_arg}{envelope_arg})
{error_log}        return failure.response()

{logger_success}\n
{response_code}\n
"""

    def _all_functions_code(self, kind: str) -> str:
        out: List[str] = []
        for name, func in self._get_user_defined_methods(kind).items():
            meta = self._route_meta(func)
            fileName = 'ApiRequests' if kind == 'app' else 'PubSubRequests'
            self._validate_route_meta(name, meta, fileName)
            params = self._parameters(func)

            if hasattr(func,'publishToProdPubSub'):
                publishToProdPubSub = func.publishToProdPubSub
            else:
                publishToProdPubSub = None

            scalarParams = self._scalar_params(func) if kind == 'app' else []
            shapeParams = self._shape_params(func) if kind == 'app' else []
            out.append(
                self._function_code(name, params, meta, kind,
                                    func.httpMethod, publishToProdPubSub,
                                    scalarParams, shapeParams))
        return "\n".join(out)

    # ----------------------------- TS generation ----------------------------- #
    def _ts_route_models(self, meta: Dict[str, Any],
                         params: Dict[str, Any]) -> set:
        """The objects.py models a route's client code references: its
        responseModel and every objects.py class its params are built
        from.

        Read from the route itself, never by scanning the generated code,
        where a model name can also be an ordinary word: every method sends
        a "Content-Type" header and an `Authorization` one."""
        objectsClasses = self._objects_classes()
        names = set()
        if meta['responseModel'] is not None:
            names.add(meta['responseModel'].__name__)
        for ann in params.values():
            names.update(leaf.__name__ for leaf in annotationLeaves(ann)
                         if leaf in objectsClasses)
        return names

    def _ts_schema_imports(self, modelNames) -> str:
        """Import each of `modelNames` (objects.py classes) from its own
        `<Name>Schema.ts`, the file generateTypeScriptSchemas writes for it.

        Importing every exported name first-come-first-served instead let a
        field alias from an earlier file win the name (json2ts exports a
        `Date` alias for a field titled Date, which shadowed the global
        Date)."""
        schema_dir = str(CLIENT_SCHEMAS_DIR)
        import_lines: List[str] = []
        for modelName in sorted(set(modelNames)):
            file = f'{modelName}Schema.ts'
            with open(os.path.join(schema_dir, file), 'r',
                      encoding='utf-8') as f:
                src = f.read()
            # json2ts suffixes a clashing name with 1. When the model's own
            # name is the one that clashed (a field type alias of the same
            # name), `import { X }` would bind to the wrong declaration.
            if re.search(
                    rf'export\s+(?:type|interface)\s+{re.escape(modelName)}1\b',
                    src):
                raise ValueError(
                    f"{file} exports both {modelName} and {modelName}1: a "
                    f"field type alias collides with the model name, so "
                    f"`{modelName}` may not be the model. Rename the field "
                    f"or give it a different title")
            if not re.search(
                    rf'export\s+(?:type|interface)\s+{re.escape(modelName)}\b',
                    src):
                raise ValueError(
                    f"{file} does not export {modelName}: run "
                    "generateTypeScriptSchemas first")
            import_lines.append(
                f"import type {{ {modelName} }} from '../schemas/{modelName}Schema';")
        return "\n".join(import_lines)

    def _ts_route_response_models(self, routeModels: Dict[str, str]) -> str:
        """Each route's responseModel by route name, so a caller that
        fetches a route without ServerRequests can still be typed by the
        route it names."""
        if not routeModels:
            return "export type RouteResponseModels = Record<never, never>;\n"
        rows = "\n".join(f"    {route}: {model};"
                         for route, model in routeModels.items())
        return f"export interface RouteResponseModels {{\n{rows}\n}}\n"

    def _ts_route_request_bodies(
            self, routeParams: Dict[str, Dict[str, Any]]) -> str:
        """Each route's JSON body by route name: the same params, with the
        same TS types, as its ServerRequests method. A route without params
        takes an empty object."""
        if not routeParams:
            return "export type RouteRequestBodies = Record<never, never>;\n"
        rows: List[str] = []
        for route, params in routeParams.items():
            if params:
                body = f"{{ {'; '.join(self._ts_param_fields(params))} }}"
            else:
                body = "Record<string, never>"
            rows.append(f"    {route}: {body};")
        return ("export interface RouteRequestBodies {\n" + "\n".join(rows) +
                "\n}\n")

    @staticmethod
    def _is_public_route(meta: Dict[str, Any]) -> bool:
        """A route a caller holding no token can POST to, as another service
        does. A login route (createAccessToken) does not count, since the
        token it mints only lands in localStorage."""
        return (meta['httpMethod'] == 'POST' and not meta['jwtRequired']
                and not meta['createAccessToken'])

    def _ts_public_routes(self, routes: List[str]) -> str:
        if not routes:
            return "export type PublicRoute = never;\n"
        rows = "\n".join(f"    | '{route}'" for route in routes)
        return f"export type PublicRoute =\n{rows};\n"

    @staticmethod
    def _ts_param_fields(params: Dict[str, Any]) -> List[str]:
        """`name: TsType` for each route param: the method's parameter list
        and its RouteRequestBodies entry, typed the same way."""
        return [f"{p}: {py_annotation_to_ts(a)}" for p, a in params.items()]

    def _ts_method_code(self, name: str, params: Dict[str, Any],
                        meta: Dict[str, Any]) -> str:
        modelName = meta['responseModel'].__name__
        param_list = ", ".join(self._ts_param_fields(params))
        body_keys = ", ".join(f'"{k}": {k}' for k in params.keys())

        auth_header = 'Authorization: `Bearer ${localStorage.getItem("access_token")}`' if meta[
            'jwtRequired'] else ''
        # A failed login carries no access_token: never store "undefined".
        token_store = 'if (data.access_token) { localStorage.setItem("access_token", data.access_token); }' if meta[
            'createAccessToken'] else ''
        jwt_token_store = 'if (data.jwtToken) { localStorage.setItem("access_token", data.jwtToken); }' if meta[
            'jwtRequired'] else ''
        data_body = f"body: JSON.stringify({{{body_keys}}})," if meta[
            'httpMethod'] == 'POST' else ''
        return_type = f"Promise<ApiResponse<{modelName}>>"
        # Typing the parsed body gets the method itself type-checked,
        # instead of everything past res.json() being implicitly any.
        data_decl = f"const data: ApiResponse<{modelName}> & {{ msg?: string }} = await res.json();"
        # A server fault (routeErrors.py's 5xx) reaches the page as
        # STATUS_FETCH_FAILED with the server's generic message and error id.
        # flask_jwt_extended answers a rejected token with a bare
        # {"msg": ...} rather than an envelope; hand the caller an
        # ApiFailure instead.
        jwt_expired_check = f'if (data.msg) {{ localStorage.removeItem("access_token"); localStorage.removeItem("user_data"); window.dispatchEvent(new Event("jwt-expired")); return {{ message: data.msg, status: {STATUS_JWT_REJECTED}, data: null }}; }}' if meta[
            'jwtRequired'] else ''
        fetch_failed = f'{{ message: "Failed to fetch data", status: {STATUS_FETCH_FAILED}, data: null }}'

        return f"""
    async {name}({param_list}): {return_type} {{
        try {{
        const res = await fetch(`${{this.apiUrl}}/{name}`, {{
            method: '{meta['httpMethod']}',
            headers: {{
            "Content-Type": "application/json",
            {auth_header}
            }},
            {data_body}
            cache: 'no-store'
        }});
        {data_decl}
        if (res.status >= {STATUS_SERVER_ERROR}) {{ return {{ message: data.message, status: {STATUS_FETCH_FAILED}, data: null }}; }}
        {token_store}
        {jwt_expired_check}
        {jwt_token_store}
        return data;
        }} catch (error) {{
            console.error("Error:", error);
            return {fetch_failed};
        }}
    }}"""

    def validateRoutes(self) -> None:
        """Run every check the generators below run on a route (its
        decorator metadata, response model and param annotations) over all
        of them up front, so a bad route aborts the run before any route,
        schema or client file is rewritten instead of halfway through."""
        for kind, fileName in (('app', 'ApiRequests'),
                               ('appPubSub', 'PubSubRequests')):
            for name, func in self._get_user_defined_methods(kind).items():
                self._validate_route_meta(name, self._route_meta(func),
                                          fileName)
                if kind == 'app':
                    self._params_with_annotations(func)

    def generateTSFile(self):  # kept name for backward compatibility
        methods = self._get_user_defined_methods('app')

        method_code: List[str] = []
        routeModels: Dict[str, str] = {}
        routeParams: Dict[str, Dict[str, Any]] = {}
        publicRoutes: List[str] = []
        referencedModels: set = set()
        for name, func in methods.items():
            meta = self._route_meta(func)
            self._validate_route_meta(name, meta, 'ApiRequests')
            params = self._params_with_annotations(func)
            method_code.append(self._ts_method_code(name, params, meta))
            if self._is_public_route(meta):
                publicRoutes.append(name)
            routeParams[name] = params
            routeModels[name] = meta['responseModel'].__name__
            referencedModels |= self._ts_route_models(meta, params)
        # Route-name maps for a caller that fetches a route without
        # ServerRequests, typed by the route it names.
        route_models_code = "\n".join([
            self._ts_route_response_models(routeModels),
            self._ts_route_request_bodies(routeParams),
            self._ts_public_routes(publicRoutes),
        ])

        # Imports are exactly the models the routes reference, so the file
        # never carries an unused one.
        header: List[str] = [
            "import Server from \"./Server\";",
            "import type { ApiResponse } from \"./ApiResponse\";",
        ]
        schema_imports = self._ts_schema_imports(referencedModels)
        if schema_imports:
            header.append(schema_imports)

        ts_class_lines = header + [
            '', '    class ServerRequests extends Server {',
            '    constructor(environment : string) {',
            '        super(environment);', '    }', ''
        ] + method_code
        ts_class_lines.append("}")
        ts_class_lines.append("\n" + route_models_code)
        ts_class_lines.append("export default ServerRequests;\n")

        serverCode = f"""
class Server {{
    public apiUrl: string | null;
    constructor(environment:string) {{
        if (!['cloudprod','localprod','localdev','clouddev'].includes(environment)) {{
            throw new Error('Invalid environment');
        }}
        const urls = {{ localApi: 'http://127.0.0.1:{PORT}', productionApi: process.env.NEXT_PUBLIC_API_URL || '' }};
        this.apiUrl = ['cloudprod','localprod'].includes(environment) ? urls.productionApi : urls.localApi;
    }}
}}
export default Server;"""

        clientDir = str(CLIENT_API_DIR)
        os.makedirs(clientDir, exist_ok=True)
        with open(os.path.join(clientDir, 'Server.ts'), 'w') as f:
            f.write(serverCode)
        with open(os.path.join(clientDir, 'ApiResponse.ts'), 'w',
                  encoding='utf-8', newline='\n') as f:
            f.write(API_RESPONSE_TS)
        with open(os.path.join(clientDir, 'ServerRequests.ts'), 'w',
                  encoding='utf-8', newline='\n') as f:
            f.write("\n".join(ts_class_lines))
        print("Server.ts, ApiResponse.ts and ServerRequests.ts generated successfully!")

    # ----------------------------- Shared constants ----------------------------- #
    def generateSharedConstants(self):
        writeSharedConstantsTS()

    # ----------------------------- Schema generation ----------------------------- #
    def _classes_from_module(self, module):
        return [
            cls for _, cls in inspect.getmembers(module, inspect.isclass)
            if cls.__module__ == module.__name__
        ]

    def generateTypeScriptSchemas(self):
        # The pydantic models of the real objects module, the same classes
        # the route checks (_objects_classes) compare against. A model whose
        # schema fails to build fails the run below.
        classes = self._objects_models()

        input_dir = str(SERVER_SCHEMAS_DIR)
        output_dir = str(CLIENT_SCHEMAS_DIR)
        os.makedirs(output_dir, exist_ok=True)

        # A schema outliving the class it was generated from could still be
        # imported by the client; drop it from both directories.
        classNames = {cls.__name__ for cls in classes}
        for directory, suffix in ((input_dir, 'Schema.json'),
                                  (output_dir, 'Schema.ts')):
            for filename in os.listdir(directory):
                if (filename.endswith(suffix)
                        and filename[:-len(suffix)] not in classNames):
                    os.remove(os.path.join(directory, filename))
                    print(f"Removed stale schema {filename}")

        # A class whose schema cannot be built fails the run: skipping it
        # would leave its stale (or no) TS type behind while the routes that
        # return it still reference it.
        for cls in classes:
            schema = _wrap_sibling_refs(
                cls.model_json_schema(mode='serialization'))
            # Closed: a model's TS interface gets no `[k: string]: unknown`
            # index signature. Only an object schema has properties to
            # close; a RootModel's bare array or number has none.
            if schema.get('type') == 'object':
                schema['additionalProperties'] = False
            # Close the nested models too. Left open, json2ts gives each
            # nested interface a `[k: string]: unknown` index signature, and a
            # renamed nested field then still type-checks on the client.
            for definition in schema.get('$defs', {}).values():
                if definition.get('type') == 'object':
                    definition.setdefault('additionalProperties', False)
            write_path = os.path.join(input_dir, f'{cls.__name__}Schema.json')
            with open(write_path, 'w') as jf:
                json.dump(schema, jf, indent=4)

        node = shutil.which('node')
        if node is None:
            raise RuntimeError(
                'node is not on PATH: install Node.js, then '
                'npm install -g json-schema-to-typescript@15')
        # One node process compiles every schema with json2ts's own library
        # (tools/json2tsBatch.js, which names a missing or mismatched global
        # install): the CLI's output, without a process per file. It writes
        # only once every schema compiled; a failure fails the run, with
        # node's message, instead of leaving the previous TS types in place.
        batch = Path(__file__).resolve().parent / 'tools' / 'json2tsBatch.js'
        result = subprocess.run([node, str(batch), input_dir, output_dir],
                                capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        print(result.stdout, end='')
        if result.returncode != 0:
            raise RuntimeError(
                f'tools/json2tsBatch.js failed: {result.stderr.strip()}')

    # ----------------------------- Flask app generation ----------------------------- #
    def generateAPI(self, kind: str = 'app'):
        if kind not in {'app', 'appPubSub'}:
            raise ValueError('Type must be either "app" or "appPubSub"')
        code = self._imports(kind) + self._flask_config(
            kind) + self._all_functions_code(kind) + self._compression_block(kind) + self._startup_check_block(kind) + self._error_handler_block(kind) + self._main_function_block(
                kind)
        target_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   f'{kind}.py')
        with open(target_path, 'w') as f:
            f.write(code)
        print(f"Generated {kind}.py")


# ----------------------------- CLI Entry ----------------------------- #
if __name__ == '__main__':
    # A full run rewrites app.py, appPubSub.py, Server.ts, ApiResponse.ts,
    # ServerRequests.ts and every schema file via a globally installed
    # json2ts, and it needs a live ENVIRONMENT, every PUBSUB_ENABLED_* var and
    # a reachable Mongo just to import. A constants-only edit needs none of
    # that.
    if _CONSTANTS_ONLY:
        # Checked inside the branch too: the guard below is unreachable once
        # the flag matches, so without this `--constants-only --force` would
        # silently ignore the second argument.
        if sys.argv[1:] != ['--constants-only']:
            sys.exit(f'Unknown arguments: {sys.argv[2:]}. '
                     '--constants-only takes no other arguments.')
        writeSharedConstantsTS()
        sys.exit(0)
    if sys.argv[1:]:
        # Without this, `--constant-only` (one character short) is ignored and
        # silently does a FULL regeneration of every generated file.
        sys.exit(f'Unknown arguments: {sys.argv[1:]}. Did you mean --constants-only?')
    creator = AppCreator(apiRequests=ApiRequests())
    # Constants next (only the Pub/Sub models pull, at import above, runs
    # before): it takes milliseconds, and bad input there aborts before
    # anything else is rewritten. Its file and the generated
    # objects_<publisher>.py are the only ones written before the route
    # validation below.
    creator.generateSharedConstants()
    # Refuse to build, before any route, schema or client file is rewritten,
    # while a services/*.py dev= URL points at a port its sibling repo does not
    # bind: a wrong dev port makes every PUBSUB_ENABLED-gated .request() call
    # fail with a connection error, or reach whatever unrelated app is bound on
    # that port. Imported here rather than at module top: --constants-only
    # must stay import-free.
    from services.AllServices import serviceGetter
    from servicePortCheck import assertServiceDevPorts
    assertServiceDevPorts(serviceGetter.listOfServices, PROJECT_ROOT)
    # Every route check the generators run, run once before any of them: a
    # bad route found mid-run would leave the schemas regenerated but
    # app.py and ServerRequests.ts stale.
    creator.validateRoutes()
    # The payload model of every topic this repo publishes (pubSubModels.py;
    # test_pubSubModels.py enforces it), read from the imported app before any
    # file is rewritten: a publisher without one stops the run here. Written
    # to server/pubsub_models.json right before the map.
    from pubSubModels import buildManifest, writeManifest
    pubSubManifest = _pubSubStep('Pub/Sub models', lambda: buildManifest(PROJECT_ROOT))
    # Every payload model is a ResponseModel all the way down, as every
    # route's responseModel is (validateRoutes).
    _pubSubStep('Pub/Sub models', lambda: assertPublisherModels(PROJECT_ROOT))
    creator.generateTypeScriptSchemas()
    creator.generateTSFile()
    creator.generateAPI('app')
    creator.generateAPI('appPubSub')
    # Last: refresh server/pubsub_map.json and PUBSUB_MAP.md, the fleet's Pub/Sub
    # map (pubSubMap.py; test_pubSubMap.py enforces it). Imported here so that
    # --constants-only stays import-free; a hard error (an unparsable source file,
    # a conflicting localRepoDirName, or a sibling the map would lose: see
    # FleetMap.blockers) stops the run.
    from pubSubMap import writeMap
    for line in _pubSubStep('Pub/Sub models', lambda: writeManifest(PROJECT_ROOT, pubSubManifest)):
        print(f'PUBSUB MODELS: {line}')
    for line in _pubSubStep('Pub/Sub map', lambda: writeMap(PROJECT_ROOT)):
        print(f'PUBSUB MAP: {line}')

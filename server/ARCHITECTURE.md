# Architecture Guide — starCommerce

## Overview

This project follows the **Star App** architecture — a Flask server with MongoDB, JWT auth, Google Cloud Pub/Sub, Pydantic models, and auto-generated TypeScript client code.

The central idea: you write business logic in `ApiRequests.py` and `PubSubRequests.py`, then run `AppCreator.py` to generate everything else (Flask routes, TypeScript client, schemas).

---

## File Map

```
server/
├── app.py                  ← GENERATED (do not edit) — Flask routes
├── appPubSub.py            ← GENERATED (do not edit) — Pub/Sub endpoints
├── AppCreator.py           — Code generation engine
├── ApiRequests.py          — API business logic (source of truth for routes)
├── PubSubRequests.py       — Pub/Sub business logic (source of truth for events)
├── route_config.py         — @route_config decorator (metadata, role enforcement, responseModel check)
├── objects/                — Pydantic models, one module per domain (base.py: MongoModel, AppModel, StoredDocumentKeys, ResponseModel, the response check and projections; user.py, store.py, product.py, order.py, ...); __init__.py re-exports every name, so callers import from `objects`
├── responseModelCheck.py   — The response-model rule (ResponseModel all the way down), for AppCreator and the tests
├── sharedConstants.py      — Values shared with the client (SOURCE OF TRUTH)
├── sharedConstantsTS.py    — Renders sharedConstants.py as serverConstants.ts, refusing secrets (AppCreator calls it)
├── mongoDb.py              — MongoDB singleton with CRUD, transactions, versioning
├── AuthHandler.py          — Firebase ID token verification + signInIdentity
├── photoStorage.py         — Product photos in Firebase Storage: download URL parsing, the admin's own reference photos, saving store photos, deleting a store's folder, a user's (but its chat/) and named chat files
├── imageModel.py           — Meta's Muse Image model (editImages), through chatModel's client and retry
├── samModel.py             — Meta's SAM 3.1 (segment: a phrase's boxes in a photo), through chatModel's client and retry
├── productFinder.py        — The products in a group photo: named (Muse Spark), located (SAM) and cut out (Pillow)
├── AppConfig.py            — Environment config (dev/prod, timezone, DB, Pub/Sub)
├── roles.py                — Role definitions and access control
├── pubSub.py               — Google Cloud Pub/Sub client, @pubSubDecorator, MockData / mockDataConsumer
├── pubSubAdmin.py          — Pub/Sub topic/subscription management
├── pubSubMockDataGenerator.py — Records this repo's published messages into pubSubMockDb for other repos to replay
├── test_pubSubContracts.py — Replays other repos' recorded messages through PubSubRequests + static publisher/consumer checks
├── test_ifAllServicesMatchConsumerProjectName.py — Guards service projectName vs pubSubMockDb collection drift
├── pubSubMap.py            — Writes and checks the fleet's Pub/Sub map (with pubSubMapScan/Contract/Section.py)
├── pubsub_map.json         ← GENERATED (do not edit) — the Pub/Sub map; PUBSUB_MAP.md is rendered from it
├── test_pubSubMap.py       — Map freshness, publisher/handler edges, payloads vs handler contracts
├── sharedFileCheck.py      — Holds the shared files to create-star-app (test_sharedFiles.py)
├── services/              — One <name>.py per remote service (AbstractService: URLs, projectName, localRepoDirName, PubSub enable flag)
├── servicePortCheck.py     — Refuses codegen when a services/<name>.py dev= port differs from the sibling repo's AppCreator PORT
├── test_servicePortCheck.py — Unit tests for servicePortCheck.py (temp dirs, no DB)
├── test_cloudbuild.py      — Pins cloudbuild.yaml, the Dockerfile guard and tools/updateJobsFromService.py (fake gcloud)
├── Dockerfile              — The service image; refuses a build not run by ../cloudbuild.yaml
├── .dockerignore           — Keeps .env, venvs and keys/ out of a local docker build
├── utils.py                — Utility functions, date param parsers (appDayStart, appDayBounds, parseIsoInstant)
├── conftest.py             — Pytest guards (localdev, *Scratch DB, local Mongo only) + test helpers
├── test_route_response_models.py — Typed responses: enforcement, the check, generator guards, coverage
├── test_auth_surface.py    — Sign-in verification, the public and role-less route sets
├── test_route_responses.py — The sample routes' response shapes
├── test_route_responses_http.py — The generated Flask transport: envelopes, JWT gate, logging
├── requirements.txt        — Python dependencies
├── pytest.ini              — Pytest settings
├── schemas/                — Generated Pydantic JSON schemas
└── tools/
    ├── dumpProdDataToLocalDev.cmd   — Dump production MongoDB to local
    ├── restoreLocalDev.cmd          — Restore local MongoDB from dump
    ├── _gcloudTarget.ps1            — Shared pickers/cache/confirm for the GCP scripts
    ├── create_or_update_job.bat     — GCP Cloud Run job launcher
    ├── create_or_update_job.ps1     — GCP Cloud Run job script (+ optional scheduler)
    ├── update_all_jobs.bat          — Refresh all jobs of a service launcher
    ├── update_all_jobs.ps1          — Refresh all jobs of a service script (calls updateJobsFromService.py)
    ├── updateJobsFromService.py     — Give a service's jobs its image + env vars (run by cloudbuild.yaml)
    ├── run_job_once.bat             — Run a job_*.py once on Cloud Run launcher
    └── run_job_once.ps1             — Run a job_*.py once on Cloud Run script

client/app/
├── api/
│   ├── Server.ts           ← GENERATED — API URL config
│   ├── ApiResponse.ts      ← GENERATED — ApiResponse<T> = ApiSuccess<T> | ApiFailure
│   └── ServerRequests.ts   ← GENERATED — Type-safe API client (Promise<ApiResponse<Model>>)
└── schemas/                ← GENERATED — TypeScript types from Pydantic models

client/lib/
└── generated/
    └── serverConstants.ts  ← GENERATED — values copied from sharedConstants.py
```

---

## Dependency Graph

```
                    ┌──────────────────────────┐
                    │   AppConfig (Foundation)  │
                    │ Environment | DB | PubSub │
                    └────────────┬─────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
           mongoDb.py        roles.py            utils.py
          (DB singleton)   (Access control)     (Helpers)
              │                  │                   │
              └────────┬─────────┴─────────┬─────────┘
                       │                   │
                       ▼                   ▼
                  objects/            AuthHandler.py
                (Pydantic models)    (ID token + signInIdentity)
                       │
              ┌────────┼────────┐
              ▼        ▼        ▼
       route_config  pubSub.py  (your business logic files)
       (decorator)   (messaging)
              │
              └────────┬────────┐
                       ▼        ▼
              ApiRequests.py  PubSubRequests.py
              (API logic)     (Event logic)
                       │
                       ▼
                 AppCreator.py ──→ GENERATES:
                                    • app.py
                                    • appPubSub.py
                                    • ServerRequests.ts
                                    • Server.ts
                                    • ApiResponse.ts
                                    • schemas/*.ts
```

---

## Request Flow (Client → Server)

```
NextJS Client (ServerRequests.ts)
    │
    │  HTTP POST with JSON body + JWT header
    ▼
app.py (Generated Flask Route)
    │
    ├─ @jwt_required() → validate JWT token (missing/expired/invalid → 401 {msg})
    ├─ Parse request body (anything but a JSON object reads as {}) → extract parameters
    ├─ get_jwt_identity() → extract userId from token
    ├─ if userId != current_user → reject (400 "Unauthorized access")
    ├─ route_config roleAccess check (if set):
    │     └─ Fetch user from DB → check role(s) with OR logic (str or list[str])
    │
    ▼
ApiRequests.methodName(params)
    │
    ├─ mongoDb.db.read/create/update(...)
    │
    ▼
route_config checks the return value against the route's responseModel
(in localdev only, raising on a mismatch; the value is returned unchanged)
    │
    ▼
Response wrapped in ApiResponse(message, status, data, jwtToken)
(a raised exception → 400, a PermissionError → 403, both with data null;
the request's keys are logged, never its values)
    │
    ▼
JSON response → the client method returns ApiResponse<Model> and stores the refreshed JWT token
```

Secret field names listed in `AppConfig.SECRET_RESPONSE_KEYS` are automatically stripped from all HTTP responses (via the `ApiResponse.data` field serializer in `objects/base.py`) and from all pubsub payloads (via the `pubSubDecorator` publish step in `pubSub.py`). Endpoints never need manual pops or `{'field': 0}` projections to hide secrets.

---

## Code Generation Pipeline

**You write** → `ApiRequests.py`, `PubSubRequests.py`, `objects/`
**AppCreator generates** → everything else

### How it works:

1. `AppCreator.py` introspects your classes using Python's `inspect` module
2. It reads `@route_config` decorator metadata from each method:
   - `httpMethod` — GET, POST, PUT, DELETE, etc.
   - `jwtRequired` — whether JWT auth is needed
   - `createAccessToken` — whether to create a new token (login endpoints)
   - `successMessage` — response message on success
   - `roleAccess` — which role permission to check
   - `responseModel` — the `objects` model the route returns
3. It reads parameter names and **type annotations** from method signatures
4. It converts Python types to TypeScript types (`str→string`, `int→number`, `List[X]→X[]`, etc.)
5. It generates Pydantic JSON schemas from the `objects` package's models, then converts them to TypeScript with json-schema-to-typescript, the library behind `json2ts`, compiling every schema in one node process (`server/tools/json2tsBatch.js`, a shared file)

`validateRoutes()` runs every check first, so a bad route aborts the run before any route, schema or client file is rewritten (only `serverConstants.ts`, generated first, may already be).

### To regenerate after changes:

```bash
cd server && ENVIRONMENT=localdev python AppCreator.py
```

It needs node and the global json-schema-to-typescript at the major version `tools/json2tsBatch.js` pins (`npm install -g json-schema-to-typescript@15`); a missing or mismatched install, or a schema that fails to compile, fails the run, and no TS file is written. Rerun it after every model or route change and commit what it writes: no test checks that the committed generated files are current.

### What gets generated:

| Generated File | From | Purpose |
|---|---|---|
| `app.py` | ApiRequests.py | Flask routes with JWT, error handling, role checks |
| `appPubSub.py` | PubSubRequests.py | Pub/Sub endpoints with idempotency |
| `ServerRequests.ts` | ApiRequests.py | TypeScript API client: each method returns `Promise<ApiResponse<Model>>`; plus `RouteResponseModels`, `RouteRequestBodies`, `PublicRoute` |
| `ApiResponse.ts` | AppCreator's `STATUS_*` | The envelope union `ApiSuccess<T>` / `ApiFailure` |
| `Server.ts` | Config | API URL switching (local vs production) |
| `schemas/*.ts` | objects/ | TypeScript interfaces from Pydantic models (closed: no index signature; a schema whose model is gone is deleted) |
| `lib/generated/serverConstants.ts` | sharedConstants.py | Values shared verbatim with the browser |
| `mcpServer.py` | The `@mcp_tool` routes (mcpCreator.py) | The local MCP servers, one per audience |
| `chatTools.py` | The `@mcp_tool` routes (chatToolsCreator.py, the same specs) | The chat route's tools: each audience's tool list, argument models and calls (`chatModel.runTool`) |
| `pubsub_map.json`, `PUBSUB_MAP.md` | Every repo's publishers and `PubSubRequests.py` | The fleet's Pub/Sub map (`pubSubMap.writeMap`, the last step of a full run) |

---

## Typed API Responses

Every `ApiRequests` route declares what it returns, and both the client and the server hold it to that:

```python
@route_config(httpMethod='POST', jwtRequired=False,
              successMessage='Todos fetched',
              responseModel=SampleTodoListResponse)
def getTodos(self) -> dict:
    return {'todos': db.read({}, 'SampleTodos',
                             projection=projection_of(SampleTodoRow))}
```

### The model

- `responseModel` must be a class the `objects` package exports (`objects/__init__.py` re-exports it: only those get a TS schema, and `test_objects_package_exports.py` fails on one it does not) and a `ResponseModel`, all the way down: every model nested in it is a `ResponseModel` too, and a `ResponseModel` inherits from `ResponseModel`s only, never from a database model (`MongoModel`, `AppModel`, `StoredDocumentKeys`, a `*View`). The same holds for every Pub/Sub publisher's payload model. AppCreator refuses to generate otherwise, naming each offending model with its file:line (`responseModelCheck.py`), and `test_responseModelsAreResponseModels` asserts it. Routes return a dict with explicit keys; a list or primitive is wrapped (`{'todos': [...]}`).
- A `RootModel` (a bare list or number) is refused unless the route is in `AppCreator.CROSS_REPO_BARE_RESPONSE_ROUTES`, which maps each route to why it is bare and who reads it: only a route another service ALREADY reads as `data['data']` (a bare value) belongs there. Empty in a new project; never add a route to avoid wrapping a new response. `test_route_response_models.py` pins the map to exactly the RootModel routes.
- Its fields are built from allowed leaves only: a scalar, `datetime`, `date`, a `Literal`, an `Enum` or a `ResponseModel`, through `Optional`, unions, `list[...]` and `dict[str, ...]`. Never `Any`, a bare `dict`/`list`, `object`, a TypedDict, a dataclass, a tuple or a set: they could carry a whole document past the check. No `@computed_field`. route_config refuses a model that breaks this when the route is decorated.
- A `ResponseModel` declares EXACTLY what the route sends, each field under its serialized key (its alias, else its name): under the localdev response check it rejects any other key, at any depth (as `extra='forbid'` would; production never validates). So the route reads the declared fields with a projection DERIVED from the model, `projection_of(FooResponse)` (a nested `ResponseModel`, a list or union of them, is read field by field as dotted keys; `_id` is excluded unless declared), or builds the dict explicitly. A nested model that may be null must be read whole (a dotted projection drops a null or a legacy scalar), so `projection_of` refuses it: read that model with `db.readResponse(s)` or, for a pipeline, `db.aggregateResponses(FooResponse, pipeline, collection)`, which read it whole and cut every document. They take `readWhole=(...)` (top-level keys read whole: a field a legacy-shape repair needs as stored, or a key the route reads for its own use) and `prepare=fn(doc)` (run on each document before the cut), and `db.projectionStage(FooRow)` puts a row's projection in the middle of an `aggregateResponses` pipeline (before a `$group`). `projection_of` also raises for a nested model it cannot express (a dict of them, a list of lists, a union with a scalar). Fields read for the route's own use are cut before it returns: `projectedCopy(FooResponse, doc)` cuts a document down to what the model declares, at every depth. A union of two or more models declares a str discriminator (`Field(discriminator='kind')`): its value is cut as the member its tag names, never one guessed by validation.
- A stored document is still typed, as stored, with its `*View` (`class FooView(StoredDocumentKeys, Foo)`, `StoredDocumentKeys` listed first: `_id` and `_version` required, never minted), but a `*View` is never a response.
- A list route sends narrowed rows, not whole documents; the exceptions are listed in `WHOLE_DOCUMENT_ROWS` (`test_route_response_models.py`), and a stale entry fails.
- Omittable but never null: `X = Field(None)`. Omittable or null: `X | None = None`.
- Row models that restate domain fields are listed in `MIRRORS` (`test_route_response_models.py`), which fails when the domain type changes and the row does not.
- No bare `dict`/`list`/`Any` fields, no `dict[str, X]` keyed by customer data, no values in validator messages. A before-validator is copy-on-write (the package rule in `objects/__init__.py`'s docstring).
- A model may not type a `SECRET_RESPONSE_KEYS` key (AppCreator refuses): `ApiResponse` strips those keys, so declare such a field `SkipJsonSchema[...]`.

### The response check (`route_config._check_response_model`)

In localdev every call validates the return value through `objects.validate_route_response` (under the `responseCheck` context), so it runs in tests (which call `ApiRequests()` directly), in Flask and in nested calls:

- **localdev** (so pytest): a mismatch raises `ValueError` naming the route and at most 10 failing fields, never input values; a validator that crashes is reported by exception class only. A deep-copy snapshot also catches a before-validator that mutated the response.
- **Elsewhere**: the check is skipped (nothing validated, copied or logged), because validating every response costs every production call time.
- The route's value is always returned unchanged.
- A test that deliberately returns an unmodelled shape turns the check off with the `responseCheckOff` fixture.

### What AppCreator generates and refuses

- Each method returns `Promise<ApiResponse<Model>>`. `ApiResponse<T>` is `ApiSuccess<T>` (`status: 200`) | `ApiFailure` (`status: 0 | 400 | 401 | 403`, `data: null`). The statuses come from the `STATUS_*` constants in `AppCreator.py`, so the TS union cannot drift from what `app.py` sends:
  - 400: the route raised a `ValueError`, the caller's to fix (including a role refusal, a `userId` mismatch, a body that is not a JSON object and an object or a list for a scalar param), with its message; a pydantic `ValidationError` names the failing fields, never their values; 403: it raised `PermissionError`;
  - 401: the client turns flask_jwt_extended's bare `{msg}` body (missing, expired or invalid token) into this envelope, removes the stored token and dispatches `jwt-expired`;
  - 0: the client's `catch` (network error or non-JSON body), message "Failed to fetch data", or a server fault (below), with the server's message. A generated method never throws.
- **Errors** (`routeErrors.py`, a shared file; its docstring has the whole mapping): every generated handler answers a failure through `routeFailure(e, status=403|400)` and logs it once, as its own log entry plus a traceback WITHOUT the exception's message, chained causes or locals. Anything that is not the caller's to fix (a `KeyError`, a Mongo error, a bug) is a real HTTP 500 with "Something went wrong on our side. Please try again. (error id X)"; a sibling that failed (`services/__request.py`'s `ServiceUnavailable`, or a `ServiceError` other than a 400 with a message) is a 503 naming it. The error id is in the log line's traceback. A sibling's 400 passes its own message on; `mongoDb.OutdatedDataError` (the `_version` lock) is a 400. So raise a `ValueError` with a message the user can act on; any other exception reaches the user only as the generic message.
- `RouteResponseModels`, `RouteRequestBodies` and `PublicRoute` map route names to their model, body and public flag. The schema imports are exactly the models the routes reference.
- Every schema, nested `$defs` included, is closed (`additionalProperties: false`), so a renamed nested field fails `tsc`; a `$ref` with a sibling description is wrapped in `allOf`, so json2ts never emits a duplicate `X1` interface.
- It refuses: a route without a `responseModel`; a model that is not a class the `objects` package exports; a RootModel outside the allowlist; a model typing a secret key; a param that is unannotated, a bare `dict`/`list`/`tuple`/`Any`, a `datetime`/`date`/`time`, a class outside the `objects` package, a container TS cannot express, or a `None` default without `| None`.
- The generated API handlers bind the request body whatever was sent (anything but a JSON object reads as `{}`, so the params are None and the route answers 400), refuse an object or a list for a scalar param (`str`/`int`/`float`/`bool`/`Literal`, or Optional of one) with a 400 "Invalid <param>" before the route runs, and the same for anything but an object for a dict or model param, or anything but a list for a list param (None only when Optional or defaulted; no other coercion), and log which of their own params a POST sent (never values, never another key). A `createAccessToken` route logs no request data at all.
- **Caller auth** (`callerAuth.py`, a shared file; its docstring has the rules): AppCreator puts `@requirePushCaller` under the route of every Pub/Sub push handler and `@requireServiceCaller` under every `@route_config(serviceOnly=True)` route (exactly the routes of `actions/ServiceToServiceActions.py`, pinned by `test_callerAuth.py`). Both require a Google-signed ID token: a push's audience is its own URL (`https://<host><path>`, what pubSubAdmin.py configures) and its email `PUSH_SERVICE_ACCOUNTS`; a service call's audience is this service's URL and its email `SERVICE_CALLER_ACCOUNTS` (`services/__request.py` fetches the token from the metadata server on Cloud Run, for the sibling's URL; a local run sends none). A refusal is a 401 logged with its reason, never the token. Localdev skips the check; `CALLER_AUTH_MODE=report` with an unexpired `CALLER_AUTH_REPORT_UNTIL` (a date, through that day UTC, or a datetime with its offset, at most 30 days ahead) logs refusals and runs the route; past that date, or without a readable one, it enforces, and report mode logs a WARNING at startup (roll out with both set: deploy the callers, watch the `callerRejected` log lines, then unset them). `callerVerified()` tells a route whether this request's caller passed the check. A `serviceOnly` route takes no JWT.
- **Accounts and identities between sibling services**: a service's `SERVICE_CALLER_ACCOUNTS` lists exactly the runtime accounts (`<svc>-run@<project>.iam.gserviceaccount.com`) of the sibling services that call it, nothing else (never a push account, never its own). Its `PUSH_SERVICE_ACCOUNTS` starts with its own `<svc>-push` account (`[0]`, what its own subscriptions mint tokens for) and then lists the `<svc>-push` account of every sibling that publishes to it: a push subscription lives with the publisher and mints its OIDC token for the PUBLISHER's push account, so without that entry every cross-service push is a 401 and dead-letters. `test_callerAuth.py` restates both tuples in its `EXPECTED_*` block. `create-star-app link` adds a sibling's runtime account once its record has one; `cloud-setup`'s `siblings.wire` makes each repo of a pair accept the other (both tuples and their `EXPECTED_*` mirrors) and commits and pushes those two files there, so that repo's trigger redeploys with them; `cloud-teardown` removes our accounts from the siblings again. A refused call or push is a 401 `callerRejected` line in the callee's log: run `create-star-app cloud-setup --step siblings.wire` in either repo.
- The generated Pub/Sub push handlers parse the push envelope inside the try, so a malformed push is a 400 "Malformed Pub/Sub push" (GCP retries, then dead-letters), never a 500, and log the messageId and the message's keys. A fault in the handler after the envelope parsed is a 500 (GCP retries any non-2xx alike).
- `route_config` refuses `roleAccess` without `jwtRequired=True`: the role check reads the user the body's userId names, which only a JWT route ties to the caller.
- The response check's mutation guard compares with `==`: a mutation that keeps the response equal is not caught, and a validator that swaps a `float('nan')` for another NaN reads as a mutation.
- A defaulted field is optional (`x?:`) in the TS type. A model a route always returns as a full `model_dump()` may opt in to `ConfigDict(json_schema_serialization_defaults_required=True)` to make them required; never a model a route takes as a param. A `@computed_field` is declared in the TS type but never sent by a raw-dict route: do not use one on a response model.

### Dates in params

`app.py` passes the raw JSON value, so a date param is `str` (no `datetime` route params). Parse it with `utils`:

- `appDayStart(isoDate)` — an ISO `YYYY-MM-DD` date as the first instant of that day in the app timezone (UTC+8, a fixed offset with no daylight-saving time);
- `appDayBounds(startDate, endDate)` — the first and the last instant of two such dates;
- `parseIsoInstant(value, name)` — an ISO 8601 date-time (`str | None`), parsed exactly as a pydantic `datetime | None` field would.

Each refuses anything else with a generic message that never quotes the value. Flask sends response datetimes as RFC 1123.

### Publishers

A `@pubSubDecorator` route's return value IS the message every consumer receives. Type it, never reshape it, keep `@pubSubDecorator` the OUTER decorator (so the response check runs before the publish), and pin the payload with `lastPublishedPayload('<route>')` in a test (`test_everyPublisherRoute_hasAPayloadPin`). `PubSubRequests` push handlers need no `responseModel`.

### Tests

- `conftest.py`, before any project import: refuses to start unless `ENVIRONMENT=localdev` and `databaseName` ends in `Scratch` (use `teststarCommerceScratch`; the default `teststarCommerce` holds the restored prod dump and tests wipe the DB), unsets the cloud Mongo variables, and replaces pymongo's `MongoClient` with `_LocalOnlyMongoClient`, which refuses every non-local host.
- **Parallel suites**: the suites of different repos may run at the same time on one local mongod. Each wipes only its own `*Scratch` DB, `pubSubMockDb` is only read (only the generators write it), and only `mainDb` / `TestDb` build indexes. Two runs of the same repo at once need different `*Scratch` names (e.g. `databaseName=teststarCommerce<Branch>Scratch`). Never run a `pubSubMockDataGenerator.py` while any suite runs: it rebuilds the `pubSubMockDb` collection the other suites replay. A sibling server a suite calls runs on its own `*Scratch` DB, never the one that sibling's own suite uses (e.g. ecommerce's accounting on :5000: `databaseName=testaccountingForEcomScratch`). Two ecommerce runs at once would share that one accounting server and its `testaccountingForEcomScratch` DB: a second ecommerce run needs its own accounting server on another port and DB, or must not run at the same time.
- Route shape tests use the conftest helpers: `assertExactShape` (validate and pin exact keys), `assertMatchesModel`, `modelKeys` / `hiddenModelKeys`, `assertRowKeys`, `dumpedKeys`, `wipeCollections`, `createTestUser`, `utcNow`, `appToday`, and `lastPublishedPayload` / `publishedPayloads` (publishes are recorded in memory; only the generator writes `pubSubMockDb`).
- `test_everyApiRoute_isCalledOutsidePytestRaises` requires every route to be called by a test that expects it to succeed (`KNOWN_UNCALLABLE_ROUTES` lists the exceptions, with the reason).
- `test_auth_surface.py` pins the routes that answer without a JWT and the JWT routes with no `roleAccess`: a new one must be added there on purpose.

```bash
cd server && ENVIRONMENT=localdev databaseName=teststarCommerceScratch pytest
```

---

## JWT Authentication

### Login Flow

```
Client: signInWithPopup → user.getIdToken() → loginWithGoogle({ idToken })
    ▼                                          (AuthContext.signInWithGoogle)
ApiRequests.loginWithGoogle() → @route_config(createAccessToken=True)
    ▼
AuthHandler.verifyFirebaseIdToken(idToken)
    │  only an RS256 token whose key id Google's certs hold is decoded; the
    │  certs are cached for their max-age, a fetch is attempted at most once
    │  a minute, a failed one keeps the last good certs for 6 hours past
    │  their expiry, and an answer of the wrong shape is refused. google-auth
    │  checks the signature, the expiry and the audience (the FIREBASE_PROJECT_ID
    │  env var, or sharedConstants.FIREBASE_PROJECT_ID, which
    │  client/lib/firebase.ts reads too; while it is the YOUR_PROJECT
    │  placeholder, or a demo- id outside emulator mode, nothing verifies
    │  and a warning says so); then the issuer
    │  and a 1-128 character subject. Anything else is a PermissionError (403)
    │  with one generic message; only the exception's class name is logged.
    ▼
requires a verified email and the google.com sign-in provider
    ▼
AuthHandler.signInIdentity(uid, email, name) → the stored Users document
(created on first sign-in; the first user of an empty Users is superAdmin
when it wins the one-time claim: inserting the Bootstrap 'superAdmin'
document, whose unique _id lets only one of two racing sign-ins succeed. The
claim outlives an emptied Users; delete it on purpose to re-run the bootstrap)
    ▼
app.py calls create_access_token(identity=user["_id"])
    ▼
ApiResponse includes access_token
    ▼
Client stores in localStorage("access_token")
```

Without an `idToken`, only localdev accepts the client's raw `uid`/`email` (signing in without a Firebase project, and the tests); elsewhere it is refused (403). `devLogin` and `devGetUserList` answer in localdev only. A login route's logs carry no request data.

**Local sign-in through the Firebase Auth emulator.** Before `cloud-setup` the project id is `demo-<name>` (`sharedConstants.FIREBASE_PROJECT_ID`, `.firebaserc`), a project Firebase runs only in its local Emulator Suite. `create-star-app dev` starts the Auth and Storage emulators (`firebase.json`: 127.0.0.1:9099 and :9199, UI on :4000) and runs app.py with `FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099`; `client/lib/firebase.ts` connects to the emulators while `NEXT_PUBLIC_ENVIRONMENT` is `localdev`, before and after cloud-setup. The emulator's ID tokens are unsigned (`alg: none`), so anyone can write one for any uid. `verifyFirebaseIdToken` decodes one (`_decodeEmulatorIdToken`) only while `emulatorTokensAccepted()`: `ENVIRONMENT` is `localdev` (AppConfig's `Environment`) AND `FIREBASE_AUTH_EMULATOR_HOST` is `127.0.0.1:<port>` or `localhost:<port>`; and only a token whose header is `alg: none` with an empty signature. The audience and issuer must still name `FIREBASE_PROJECT_ID`, the subject must be set and the token unexpired; a signed token is verified against Google's keys as always. clouddev, cloudprod and localprod refuse them even with the variable set, and the module logs `firebaseAuthEmulatorMode` (or `firebaseAuthEmulatorHostIgnored`) once at import when the variable is set. Never set `FIREBASE_AUTH_EMULATOR_HOST` on a deployed service, and never widen this rule. Storage files follow `storage.rules` (in the emulator and, once cloud-setup deploys them, on the real bucket): a signed-in user reads and deletes `users/<uid>/` only, and uploads there only a JPEG, PNG or WebP under 10 MB.

### Subsequent Requests

```
Client sends: Authorization: Bearer {token}
    ▼
@jwt_required() validates token
    ▼
current_user = get_jwt_identity() → userId
    ▼
Authorization check: userId == current_user
    ▼
JWT refreshed on every successful request (jwtToken in response)
    ▼
Token expires after 5 minutes (configured in app.py)
```

### Token Expiry Handling (Client)

When a JWT-protected request returns `data.msg` (Flask-JWT-Extended error format), the client:
1. Removes `access_token` from localStorage
2. Removes `user_data` from localStorage
3. Dispatches `window.dispatchEvent(new Event("jwt-expired"))` for the app to handle
4. Returns the `ApiFailure` `{ message: data.msg, status: 401, data: null }`

---

## Role-Based Access Control

### How roles work:

1. **Define roles** in `roles.py` → `AllRoles` class contains all permission flags
2. **Define user types** in `roles.py` → `UserRoleTypes` maps user types to role configurations
3. **Assign roles** → `User.setRole(userType)` sets all role flags based on user type
4. **Enforce at route level** → `@route_config(roleAccess='canDoSomething')`

### Enforcement flow:

```python
# In ApiRequests.py — you write this:
@route_config(httpMethod='POST', jwtRequired=True, roleAccess='canUpdateUserRole')
def setUserRole(self, userId: str, targetUserId: str, userType: str):
    ...

# route_config.py does this automatically at runtime:
# 1. Fetch user: user = User(**db.read({'_id': userId}, 'Users'))
# 2. Check: user.checkIfAuthorized('canUpdateUserRole')
# 3. If role value is False → raise PermissionError
# 4. If authorized → call the actual method
```

### Adding a new role:

1. Add the role to `AllRoles` in `roles.py`
2. Set its default value in each user type in `UserRoleTypes`
3. Use it in `@route_config(roleAccess='yourNewRole')`

---

## Database Layer (mongoDb.py)

### Singleton Access

```python
from mongoDb import db  # Already instantiated — use directly

db.create(data, 'CollectionName')
db.read(query, 'CollectionName', findOne=True)
db.update(query, updateData, 'CollectionName')
db.readWithPagination(query, 'CollectionName', page=1, pageSize=20)
db.aggregate(pipeline, 'CollectionName')
db.createTransaction(updateFunction)
```

### Environments

| Environment | Database Name | Connection |
|---|---|---|
| `localdev` | `teststarCommerce` | localhost:27017 |
| `localprod` | `teststarCommerce` | localhost:27017 |
| `clouddev` | `teststarCommerce` | MongoDB Atlas (`MONGO_URI` env var) |
| `cloudprod` | `databaseName` (required, e.g. `starCommerce`) | MongoDB Atlas (`MONGO_URI` env var) |

The `databaseName` env var names the database. In cloudprod it is required: AppConfig refuses to start without it (`databaseName environment is not set for cloudprod environment`), so a production service never falls back to a default. Elsewhere it defaults to `teststarCommerce`.

### Connection budget

The Star App services share one MongoDB Atlas Flex cluster, which caps connections across all of them (each service has its own database and user). What counts is sockets, not clients:

- One connected client holds about 6 monitor sockets on a 3-member replica set (pymongo monitors each member on two sockets), plus its pool sockets, opened on demand up to the number of operations in flight at once.
- Multiply by the processes: Cloud Run instances (up to the service's max instances), gunicorn workers per instance and running job tasks. A second connected client per process doubles the monitor share.
- An idle instance keeps its sockets until Cloud Run scales it down, so `--min-instances` above 0 holds its share permanently.

So a cloud process holds one real client:

- `db` (`mainDb`) is the only client that connects at import.
- Any other `MongoClient` built at import uses `connect=False`, so pymongo opens no connection or topology monitor until a first operation (it only defers: a first operation would connect). `pubSubMockDb` is built that way, and it is never used in the cloud: `pubSub.publishMessage` refuses to write it in every cloud environment.
- No `testDb` exists in the cloud: only the tests use one.
- A second `mainDb()` or `PubSubMockDb()` call returns the existing instance and builds no new client (`_initialized`).
- A missing or empty `MONGO_URI` fails at once, never a client for a default host.
- The Dockerfile runs one gunicorn worker per instance, with 8 threads (`--threads 8`): keep it one process (every extra worker is another client). The threads share its one client (PyMongo is thread-safe), so they add pool sockets on demand, up to one per request in flight, and no monitor sockets.
- Do not set `maxPoolSize` to fit the cap: the pool opens sockets only for concurrent operations, and a lower limit just queues them. Cap the processes instead.

`create-star-app cloud-setup` creates a service with `--max-instances=10`. Size each service's max instances (and its jobs' parallelism) so the fleet's total stays under the cluster's cap; the fleet repos set their own.

`server/test_mongoDb_cloud_clients.py` (shared) pins the clients built at import. A new client at import, such as a second cluster, goes in with `connect=False` and a pin in that file's per-repo block.

### Optimistic Locking

Every document must have a `_version` field. When updating with `checkVersion=True`:
1. Read the document's current `_version`
2. Attempt update with `_version` match in the query
3. If another process updated first → version mismatch → error raised
4. Prevents lost updates in concurrent scenarios

### Transactions

```python
def myTransaction(session):
    db.create(data1, 'Collection1', session=session)
    db.update(query, data2, 'Collection2', session=session)
    # If any operation fails, all are rolled back

db.createTransaction(myTransaction)
```

---

## Pub/Sub Integration

### Inbound Flow (receiving events from other services)

```
External Service publishes message
    ▼
Google Cloud Pub/Sub Topic
    ▼
Push subscription → POST to /methodName on appPubSub.py
    ▼
Decode base64 message data
    ▼
Idempotency check: has messageId been processed?
  ├─ Yes → return 200 (skip)
  └─ No → call PubSubRequests.methodName(data)
              ▼
           Store messageId in 'PubSubMessages' collection
              ▼
           Return 200
```

### Outbound Flow (publishing events to other services)

```python
from pubSub import pubSubDecorator
from services.exampleService import exampleService

@pubSubDecorator(projectNameConsumers=[exampleService])
@route_config(httpMethod='POST', jwtRequired=True, successMessage='Order created')
def createOrder(self, userId: str, customerId: str, items: list,
                publishToProdPubSub: bool,            # no default: pubSubDecorator raises at import if it has one
                publishToPubSubMockDb: bool = False):  # True only in pubSubMockDataGenerator.py
    ...
    return db.create(order, 'Orders')  # the return dict IS the message payload
```

The decorator publishes the return dict on topic `createOrder_starCommerce`; each consumer receives it in a `PubSubRequests` handler named `createOrder_starCommerce`. `@pubSubDecorator(..., publishToProdPubSub=False)` makes a publisher mock/replay-only: production never publishes it (`app.py` passes the decorator's value), generator rows are still produced, and a call with `publishToProdPubSub=True` raises. A commented, fully annotated copy of this example lives at the bottom of `ApiRequests.py`.

Where a publish actually goes (`pubSub.publishMessage`):

| Environment | `publishToPubSubMockDb=True` | otherwise |
|---|---|---|
| `localdev` | `MockData` row in `pubSubMockDb.starCommerce` -- what consumers replay | passive capture row in `pubSubMockDb.starCommerce_devData` (skipped under pytest; never consumed, safe to drop) |
| `localprod`, `clouddev` | mock row (the generator refuses to run here, so this should never happen) | nothing is published |
| `cloudprod` | raises | the real topic, and only if at least one consumer has `PUBSUB_ENABLED_<SERVICE>=true` |

### Dead Letter Handling

A handler that does not return 200 is retried up to `max_delivery_attempts` (5, set in `pubSubAdmin.py`) and the message is then routed to the dead-letter topic. If you add a `createDeadLetter` receiver to `PubSubRequests.py` (AppCreator recognises that name and wires the dead-letter push to it), list it in `UNTESTED_PUBSUB_FUNCTIONS` next to `ping`: it is fed by the dead-letter topic, never by a `@pubSubDecorator` function, so it is the second and last legitimate entry there.

---

## Pub/Sub mock messages & contract tests

Nothing at runtime checks that the dict a publisher returns is the dict a consumer's handler expects. Instead, every repo records its real published payloads into one shared local MongoDB database, `pubSubMockDb`, and every consumer replays the payloads addressed to it through its real handlers under `pytest`.

### Flow

```
PUBLISHER REPO                                    CONSUMER REPO
-----------------------------------------------   ------------------------------------------
ApiRequests.createOrder(...,                      PubSubRequests.createOrder_<publisher>(data)
    publishToProdPubSub=False,                                      ^
    publishToPubSubMockDb=True)                                     |
  @pubSubDecorator(projectNameConsumers=[svc])                      |
        |                                                           |
        | pubSubMockDataGenerator.py calls it once                  | test_pubSubContracts.py
        v                                                           | replays every row addressed
MockData row in pubSubMockDb.<publisher projectName>                | to Project().projectName, in
    projectNameConsumers = [svc.getProjectName(), ...]   ---------->+ `index` order, via
    consumeFunctionName  = 'createOrder_<publisher>'                | mockDataConsumer.consumeMockData(
    index                = emit order                               |   publisherProjectName, handler)
    dateOfCreation       = expires after 3 days                     v
                                                  test_atExitValidation FAILS if any
                                                  addressed row was never consumed
```

### File map

| File | Role |
|---|---|
| `pubSub.py` | `@pubSubDecorator` (publishes the return dict, or stores it as `MockData` when `publishToPubSubMockDb=True`), `MockData` (3-day `checkIfValid` expiry), `mockDataConsumer` (replays rows in `index` order and tracks what was consumed) |
| `pubSubMockDataGenerator.py` | Publisher side. Calls every `@pubSubDecorator` function once with `publishToProdPubSub=False, publishToPubSubMockDb=True`. Deletes and rebuilds only this repo's `pubSubMockDb.starCommerce` collection. localdev only; wipes `TEST_COLLECTIONS` before and after |
| `test_pubSubContracts.py` | Consumer side. Three static AST tests (publisher flags, generator coverage, handler/message 1:1), the replay (`REPLAY_STEPS` seed + assert per handler, `REDELIVERY_STEPS` idempotency per handler) and `test_atExitValidation` |
| `test_ifAllServicesMatchConsumerProjectName.py` | Every `services/<name>.py` is registered and its `getProjectName()` is an existing `pubSubMockDb` collection, i.e. messages addressed to it can actually be consumed |
| `services/<name>.py` | `projectName=` must equal the target repo's `AppConfig().projectName` whenever it differs from the filename; mock messages are addressed with `getProjectName()` |
| `servicePortCheck.py` | Every `services/<name>.py` `dev=` port must equal the target repo's `AppCreator.py` `PORT` (read from `../<localRepoDirName>/server/AppCreator.py`); `AppCreator.py` exits with a drift table on mismatch and only warns when the sibling checkout is absent. `--constants-only` skips it |

### Rules and caveats

- **Run order**: run every publisher's `pubSubMockDataGenerator.py` before a consumer's `pytest`. Each generator deletes and rebuilds only its own collection, so publishers can be regenerated in any order among themselves, but always before the consumers that read them.
- **One shared database**: `pubSubMockDb` is shared by every repo on the machine. Never run two repos' suites or generators at the same time. Some suites delete their own repo's collection while running; regenerate afterwards.
- **Expiry**: rows older than 3 days are rejected by `MockData.checkIfValid`. The fix is always to re-run the publisher's generator, never to edit the row.
- **Wipes**: `pubSubMockDataGenerator.py` owns the one `TEST_COLLECTIONS` list (it includes `PubSubMessages`, the route's messageId de-dupe log) and the one `wipeDatabase()`; the replay test imports both. Both are localdev-only and empty those collections of this repo's local test database before and after. Extend the list whenever a handler or generator setup writes to a new collection.
- **Bootstrap**: with a completely empty `pubSubMockDb` the two static tests that read it (`test_ifAllApiRequestTested`, `test_ifAllPubSubMockDataHasFunctions`) refuse to run. `python pubSubMockDataGenerator.py` creates this repo's collection even with zero publishers, which is enough.
- **Shared code**: the "Shared AST helpers" and "Static contract tests" regions of `test_pubSubContracts.py` (between their `# >>> star-app shared:` marker lines) are identical in every repo; only the text outside them (`wipeDatabase()`, the replay section) is per-repo. `test_sharedFiles.py` fails when a region differs from create-star-app's `templates/test_pubSubContracts.py.tmpl`: fix the template, commit it, then run `cd server && python sharedFileCheck.py --write` in every repo (see "Shared files").
- **What guards what**: `pubSubDecorator` raises `ValueError` at import / call time when a publisher lacks either flag, gives `publishToProdPubSub` a default, or is called with both flags `True`. `test_ifAllPubSubFunctionsGenerated` only adds generator coverage (every publisher called in `pubSubMockDataGenerator.py` with the literal `publishToPubSubMockDb=True`) and the explicit `publishToProdPubSub` keyword on every call site in the top-level `server/*.py` files (non-recursive, `PubSubRequests.py` excepted).
- **Redelivery**: the generated route de-dupes most redeliveries by messageId, but `PubSubMessages` is written after the handler returns, so a crash between the handler and the 200 redelivers the same messageId into the handler. Handlers must be idempotent; `REDELIVERY_STEPS` proves it.
- **`UNTESTED_PUBSUB_FUNCTIONS`** holds only handlers no publisher will ever address: `ping`, and `createDeadLetter` if you add it. Never park a real handler there.

### The Pub/Sub map

The replay needs every repo's generator run on one machine. The map is the static complement: `server/pubsub_map.json` (the source) and `server/PUBSUB_MAP.md` (rendered from it) list every `@pubSubDecorator` publisher in the fleet, each consumer's handler, the payload paths each handler reads and whether the publisher's types satisfy them. Both are generated, checked in and identical in every repo; never edit them.

- **Regenerate**: `cd server && python pubSubMap.py --write` (AppCreator also runs it at the end of a full generation). `python pubSubMap.py` exits 0 only when the map is fresh and clean for this repo, equivalent to `test_pubSubMap.py` (use it where pytest can't run); 1 when stale or a problem fails this repo's map tests; 2 on an error. Problems that only concern other repos print WARNING lines and keep exit 0. `--explain <topic|handler|publisherFn>` prints live `file:line` and where each side was read from.
- **Siblings** are found through `services/*.py` (`localRepoDirName`, resolved as `../<dir>`) and read at a committed git ref: this branch's name if the sibling has it, else `main` (`--repo name=path[@ref]` overrides one; `--worktree-siblings` reads working trees). A missing sibling falls back to the snapshot and gets a gitignored `server/services/<name>.notFound.md` note; read it. `--write` and the AppCreator hook refuse (exit 2, "refusing to write the map") while a sibling this repo publishes to or consumes from, or one in the checked-in map, is neither checked out nor in the snapshot: clone it to `../<localRepoDirName>` or pass `--repo name=path[@ref]`.
- **`test_pubSubMap.py`** fails on a stale map, a consumer with no handler, a handler with no publisher, a message contract it cannot resolve, a publisher `pubSubAdmin.py` cannot see (a non-literal consumer list; consumers passed positionally instead of as `projectNameConsumers=`; a decorator not written `@pubSubDecorator(...)`, e.g. `@pubSub.pubSubDecorator`; an `async def`; a consumer whose name is not a `services/` class name (case-insensitive); an unknown consumer; a file outside `server/*.py` and `server/actions/*.py`), a hand `publishMessage` on a topic that is not a decorated function's, a dead hatch or a `PUBSUB_CONTRACT_GAPS` dict (retired, see below), and a compat issue (`undeclared`, `nullable`, `literal`, `numericNarrowing`, `forbiddenExtra`) on any flow, in both the publisher's repo and the consumer's.
- **Compat is strict**: a mismatch is fixed at its source, never recorded: the publisher's payload model, or the consumer's handling (`--explain <handler>` shows both sides). A flow's verdict is `ok`, `issues` (fails) or `unverified` (payload unknown, or a snapshot side that could not be re-checked; shown in the map, not failed). `PUBSUB_CONTRACT_GAPS`, the old list of known mismatches, is retired: defining it in any form, even `{}`, is a hatch error.
- **Escape hatch** (a literal dict, read by AST; it declares a model and suppresses nothing): `PUBSUB_PAYLOAD_MODELS = {'<fn>': '<Model>'}` in the module of a publisher that is no route. It is that publisher's payload model: `pubSubModels.py` writes it to the manifest, `route_config.check_route_response` holds the payload to it (localdev only), and the map takes it as the payload when it cannot type the return value (`declared`) and fills in the unknown value types of a returned dict literal from it. A handler has none: it parses its message with its generated `<Fn>Message` (`test_everyHandlerParsesItsGeneratedMessage`), and the retired `PUBSUB_MESSAGE_MODELS` is a hatch error in any form.
- **Blind spot**: static and type-level only. Value-dependent logic is not modelled, so the replay above stays the ground truth.
- **A contract change** (a payload at a path a consumer reads, a handler's reads, a consumer list, a renamed publisher or handler) is done only when the map is regenerated and committed in every repo of the fleet, each after the change is committed in its own repo. Siblings are read at this repo's branch name if they have that branch, else at `main`, so regenerate with the same branch name checked out in every repo (a cross-repo feature branch shares one name), or after merging to `main`; otherwise the other repos read the old `main`. `cd server && python pubSubMap.py --write-all` regenerates this repo and every sibling checkout on the same branch in one go (skipping the others with a reason) and lists the repos to commit; the commits stay manual.

### Generated message models (`pubSubModels.py`)

The map checks types statically; the generated models enforce them at the consumer's door.

- **Publisher**: this repo commits `server/pubsub_models.json`, the serialization JSON schema of every topic's payload model: the route's `responseModel`, or `PUBSUB_PAYLOAD_MODELS = {'<fn>': '<Model>'}` in the module of a publisher that is no route (a hand-publish site checks its payload with `route_config.check_route_response`). AppCreator builds it after `validateRoutes()` and writes it before the map; `ENVIRONMENT=localdev python pubSubModels.py --write-manifest` writes it alone. A serializer in a payload model needs a return type, or the manifest would state the declared type instead of what is sent.
- **Consumer**: `server/objects_<publisherProjectName>.py` holds one pydantic class per object schema (fields and types only) and a `<Fn>Message` per topic this repo consumes. It is generated (`python pubSubModels.py --pull`; AppCreator pulls before it imports the app): never edit it, and keep logic out of it. `--push` in the publisher pulls in every same-branch consumer checkout; the commits stay manual.
- **A handler** parses its whole message once, `message = <Fn>Message.model_validate(data)`, and reads only `message`. It touches `data` again only inside an `except ValidationError:` on that parse: a fallback that keeps acting on the few fields an older handler read, so drift elsewhere in the payload does not drop the action. Logic lives in helpers that take the message; what is stored is built through this repo's own models (the generated classes do no empty-string coercion, and the publisher's `_version` never reaches this repo's lock).
- **Tests**: `test_pubSubModels.py` fails on a stale generated file or manifest, and on a handler that breaks the rule above (`test_everyHandlerParsesItsGeneratedMessage`). `test_ifAllPubSubMockDataParsesWithGeneratedModels` parses every mock message addressed to this repo with its generated model, as stored and as sent on the wire. A consumer test feeds a payload the publisher can produce: every required key, not a hand-trimmed subset.

### Deploying strict consumers

A consumer's generated models refuse a payload that does not fit them, so a payload change deploys in an order:

- **Narrowing, or adding a field**, ships the publisher first: its consumers already accept the narrower payload and ignore the new key until they pull.
- **Widening** (a new `Literal` member, a field that becomes nullable or optional, a dropped field, `int` to `float`) ships after every consumer has pulled the new models and deployed, or their parse refuses the new payload.
- **Before the first strict deploy**, scan the production data each payload carries (on a restored dump) for stored shapes the models refuse (legacy shapes, a date stored as a string, a fractional quantity) and fix them first. A generated model is stricter than the hand-written one it replaces, which read only a few fields.

### Adding a new publisher (this repo emits a message)

1. In `ApiRequests.py`, decorate the method with `@pubSubDecorator(projectNameConsumers=[<service>, ...])` outside `@route_config`, add `publishToProdPubSub: bool` (no default) and `publishToPubSubMockDb: bool = False` parameters, and return a dict with explicit keys -- that dict is the payload, and the route's `responseModel` is its model (consumers generate their message class from it; see "Generated message models"). Use the commented `createOrder` example at the bottom of `ApiRequests.py` as the pattern.
2. Make sure each consumer service in `services/` has `projectName=` set to the target repo's `AppConfig().projectName` if it differs from the filename, and remove that service from `SERVICES_WITHOUT_MOCK_DATA_YET` in `test_ifAllServicesMatchConsumerProjectName.py` (it is keyed on `service.name`; leaving it there silently disables the drift guard for that consumer).
3. In `pubSubMockDataGenerator.py`, seed whatever the method reads, then call it once with the literal keywords `publishToProdPubSub=False, publishToPubSubMockDb=True` (an AST scan looks for that literal). Emit creates before the updates/deletes that touch the same document. Add any new collections to `TEST_COLLECTIONS`.
4. `python AppCreator.py`, then `python pubSubMockDataGenerator.py`, then `pytest` here, all with `ENVIRONMENT=localdev databaseName=teststarCommerceScratch` (the generator wipes the selected DB and does not check its name; pytest refuses any other DB). Pin the new payload with `lastPublishedPayload('<route>')` in a test. Importing the module already proved the flag rules (the decorator raises otherwise); `test_ifAllPubSubFunctionsGenerated` proves the generator call and the explicit `publishToProdPubSub` at every call site. AppCreator rewrote `pubsub_map.json` / `PUBSUB_MAP.md`: commit them (see "The Pub/Sub map").
5. Set `PUBSUB_ENABLED_<SERVICE>=true` (e.g. `PUBSUB_ENABLED_EXAMPLE_SERVICE`) in every environment that must publish to that consumer. `AppConfig` raises at start-up if the variable is missing, `pubSubAdmin.py` raises "No Cloud Run URL mapped" for a consumer that is not enabled/mapped, and `cloudprod` publishing silently skips disabled consumers.
6. Run `pubSubAdmin.py` from THIS repo BEFORE deploying: it creates the topic `<function>_starCommerce` and one push subscription `<function>_starCommerce_to_<consumerService>` per consumer (endpoint `<consumer URL>/<function>_starCommerce`). `checkPubSubTopics` (run by the predeploy job, before the deploy) fails the build while a topic for an enabled publisher is missing, so the order is: pubSubAdmin, then deploy.
7. In every consumer repo, follow "Adding a new consumer handler" below and run its suite -- its `test_atExitValidation` fails until the new message is handled.

### Adding a new consumer handler (this repo receives a message)

1. Find the payload: the publisher's method return dict, or the row in `pubSubMockDb.<publisherProjectName>` after its generator ran.
2. Run `python pubSubModels.py --pull` (or AppCreator) so `objects_<publisherProjectName>.py` has the topic's `<Fn>Message`. In `PubSubRequests.py`, add `<function>_<publisherProjectName>(self, data)` under `@route_config(httpMethod='POST', jwtRequired=False, ...)` that parses `message = <Fn>Message.model_validate(data)` and reads only `message` (see "Generated message models"). Make it idempotent (look up by natural key, return the existing doc / `{'updated': 0}` on redelivery) and never raise on a benign missing document.
3. In `test_pubSubContracts.py`, add a `REPLAY_STEPS` entry keyed `PubSubRequests().<handler>.__name__: (seed, assert)` -- seed only prerequisites the handler reads, assert the real DB state after a first delivery -- and a `REDELIVERY_STEPS` entry `(owned collection, payload -> owned _id, check)`. Put the publisher in `EXPECTED_PUBLISHERS` if it is not there yet. Add any new collections to `TEST_COLLECTIONS`.
4. `python AppCreator.py` (regenerates `appPubSub.py`), run the publisher's generator, then `pytest` here (`ENVIRONMENT=localdev databaseName=teststarCommerceScratch`). `test_ifAllPubSubMockDataHasFunctions` and `test_atExitValidation` prove the handler and the message are matched 1:1. Commit the rewritten `pubsub_map.json` / `PUBSUB_MAP.md`; `test_pubSubMap.py` proves the handler's reads match the publisher's payload.
5. Nothing to run in GCP from this repo: this repo's `pubSubAdmin.py` creates nothing for topics it consumes. The push subscription `<function>_<publisher>_to_<thisService>` is created by the PUBLISHER's `pubSubAdmin.py`, which needs `PUBSUB_ENABLED_<THIS_SERVICE>=true` and this repo's Cloud Run URL mapped on its side -- ask for that (or do it) in the publisher repo.

---

## Pydantic Model Hierarchy

```
BaseModel (Pydantic)
    ├── MongoModel          — DB-persisted documents (_id, _version, empty-string coercion)
    │       └── User, SampleTodo, Product, Order, etc.
    ├── AppModel            — Embedded/non-persisted models (empty-string coercion only)
    │       └── FirebaseUserObject, etc.
    ├── StoredDocumentKeys  — Mixin for a document READ from Mongo (_id, _version required)
    │       └── UserView(StoredDocumentKeys, User), SampleTodoView, etc.
    ├── ResponseModel       — What a route sends, and every model nested in it (never a database model)
    │       └── UserResponse, SampleTodoRow, SampleTodoListResponse, NoContentResponse, etc.
    └── ApiResponse         — Response wrapper (no coercion needed)
```

### When to use which:

| Base Class | Use For | Has _id/_version | Empty String → None |
|---|---|---|---|
| `MongoModel` | Documents stored in MongoDB | Yes (auto-generated) | Yes |
| `AppModel` | Embedded sub-documents, DTOs, request models | No | Yes |
| `ResponseModel` | A route's responseModel, a publisher's payload, and every model nested in either | Only if declared | Yes |
| `BaseModel` | Simple data carriers with no `str \| None` fields | No | No |
| `StoredDocumentKeys` (first base) | A document as read from Mongo (`*View`); never a response | Yes (required) | From the domain model |

### Empty String Coercion

All `str | None` fields in `MongoModel`, `AppModel` and `ResponseModel` subclasses automatically convert `""` to `None`. This prevents issues where empty strings pass `is not None` checks but fail when used as DB lookup keys. The coercion is copy-on-write: it never edits the dict it validates, because route_config validates a route's response without copying it.

```python
# Example: empty string becomes None automatically
class Product(MongoModel):
    category: str | None = None

p = Product(category="")
assert p.category is None  # True — coerced to None
```

---

## Generated vs. Manual Files

### Files you edit (source of truth):

| File | What to add |
|---|---|
| `ApiRequests.py` | New API endpoints (add methods with @route_config) |
| `PubSubRequests.py` | New Pub/Sub event handlers |
| `pubSubMockDataGenerator.py` | One call per `@pubSubDecorator` publisher with `publishToPubSubMockDb=True`; `TEST_COLLECTIONS` |
| `test_pubSubContracts.py` | `REPLAY_STEPS` / `REDELIVERY_STEPS` / `EXPECTED_PUBLISHERS` per handler (never the two `star-app shared` regions: see "Shared files") |
| `test_ifAllServicesMatchConsumerProjectName.py` | `SERVICES_WITHOUT_MOCK_DATA_YET` when a target repo has no mock data yet |
| `services/<name>.py` | One file per remote service; `projectName=` when it differs from the filename |
| `objects/<domain>.py` | New Pydantic models (in the module of their domain, re-exported from `objects/__init__.py`) |
| `sharedConstants.py` | Values the client needs too (see the warning in its docstring — everything public there ships to every browser) |
| `roles.py` | New roles and user types |
| `AuthHandler.py` | Auth-related logic |
| `utils.py` | Utility functions |

### Files you never edit (auto-generated):

| File | Regenerated by |
|---|---|
| `app.py` | `ENVIRONMENT=localdev python AppCreator.py` |
| `appPubSub.py` | `ENVIRONMENT=localdev python AppCreator.py` |
| `ServerRequests.ts` | `ENVIRONMENT=localdev python AppCreator.py` |
| `ApiResponse.ts` | `ENVIRONMENT=localdev python AppCreator.py` |
| `Server.ts` | `ENVIRONMENT=localdev python AppCreator.py` |
| `schemas/*.ts` | `ENVIRONMENT=localdev python AppCreator.py` |
| `lib/generated/serverConstants.ts` | `python AppCreator.py --constants-only` |
| `mcpServer.py`, `chatTools.py` | `ENVIRONMENT=localdev python AppCreator.py` (or `python mcpCreator.py` / `python chatToolsCreator.py` alone) |
| `pubsub_map.json`, `PUBSUB_MAP.md` | `python pubSubMap.py --write` (AppCreator runs it too) |

### Shared files

create-star-app is the source of truth for the files every Star App repo keeps identical: the generic tests (`test_file_sizes.py`, `test_route_coverage.py`, `test_mock_drift.py`, ...), `servicePortCheck.py`, the `pubSubMap*.py` tools, `sharedFileCheck.py`, the `tools/` job scripts and the two `star-app shared` regions of `test_pubSubContracts.py`. Its `fleet/sharedFiles.json` lists them. `test_sharedFiles.py` fails while a copy differs from the create-star-app checkout at `../create-star-app` (its committed HEAD), and skips without one. A `# >>> star-app per-repo: <name>` block keeps this repo's own content.

Never edit one repo's copy alone: change the template in create-star-app, commit it, then run `cd server && python sharedFileCheck.py --write` in every repo. `--template-worktree` previews uncommitted template work. The usage notes are in the `sharedFileCheck.py` docstring.

### Adding a new API endpoint:

1. Add (or reuse) its response model in `objects/<domain>.py` (re-exported from `objects/__init__.py`): a `ResponseModel` declaring exactly the fields it sends, e.g. `class MyThingResponse(ResponseModel)`.
2. Add a method to a class in `ApiRequests.py`:
   ```python
   @route_config(httpMethod='POST', jwtRequired=True, successMessage='Done',
                 responseModel=MyThingResponse)
   def myNewEndpoint(self, userId: str, someParam: str) -> dict:
       result = db.read({'_id': someParam}, 'MyCollection',
                        projection=projection_of(MyThingResponse),
                        findOne=True)
       if result is None:
           raise ValueError('Not found')
       return result
   ```
3. Run `cd server && ENVIRONMENT=localdev python AppCreator.py`
4. `app.py` now has a `/myNewEndpoint` route
5. `ServerRequests.ts` now has `myNewEndpoint(userId: string, someParam: string): Promise<ApiResponse<MyThingResponse>>`; narrow on `res.status === 200` before reading `res.data`
6. Add a test calling it outside `pytest.raises` that pins its shape (`assertExactShape`), and add it to `test_auth_surface.py`'s sets if it answers without a JWT or has no `roleAccess`

## Product photos

`generateProductPhotos` (actions/PhotoActions.py, an admin route and MCP tool) turns photos the admin uploaded under their own `users/<uid>/` folder into 3 product photos (`PHOTOS_PER_CALL`) with Meta's `muse-image-1.0` (imageModel.py), in the store's look: its `photoStyle` and brand colors (`primaryColor`, `secondaryColor`), given at `createUserStore` (the primary color required there, the secondary optional; a store from before that may have none) and changed with `updateMyStoreTheme`. The photo style is sent only to the admin (`getUserStore`'s AdminStoreResponse); the brand colors also go to the shop (`startGuestSession`'s ShopStoreResponse, each only once set), which the client draws in the primary color (buttons, the user's messages, the map pin, the voice glow), as it does the admin's chat (`getUserStore`, re-read after every saved turn, so a save shows at the end of its turn). photoStorage.py reads the reference photos through the Storage SDK by bucket and path, never by fetching a URL: only a Firebase download URL of our bucket is accepted (the emulator's `http://127.0.0.1:9199/...` in localdev only), and only JPEGs or PNGs of at most 10 MB each and 25 MB together (HEIC/HEIF refused). The results go to `stores/<storeId>/photos/<photoId>.jpg` with a download token, and one GeneratedPhotos document each. `storage.rules` lets anyone get a file there by its path (never list the folder) and nobody write it: only the server does, with its own credentials, so the Cloud Run service account needs object access to the bucket (`roles/storage.objectAdmin`). After a rules change: `firebase deploy --only storage`. Each store makes at most 100 photos a day, and all stores together at most `PHOTO_DAILY_CAP_ALL` (default 1000; every Google sign-in becomes an admin with a store, so the store cap alone bounds nothing) (UTC+8, PhotoUsage counters, actions/usageCounters.py, shared with the chat's caps). Photos the model did not return are not counted; one returned but not saved is (Meta billed it). One deadline from the call's start bounds the reads and the model call; each photo is recorded right after its upload, so a failed later upload still returns the saved ones. At most 2 photo calls run at once per process (actions/callSlots.py, shared with the chat).

Every photo call makes 3 photos from two prompts (`PhotoActions.photoPrompts`), whose two model calls run at once: photo 1 from the global prompt (`globalPhotoPrompt`: the fixed `PHOTO_INSTRUCTION`, then `Special request: <notes>` when the admin sent notes, `Product: <name>` when known, and the product alone on a plain, seamless background in the store's primary color (`backgroundLine`: no props, surfaces or scenery), or, for a store without a primary color, the brand colors line when the secondary is set), photos 2 and 3 from the themed prompt (`photoPrompt`: the same first lines, the brand colors line (accents in the background and props) when a color is set, and `Style: <photoStyle>`; never the plain background). A store with no photo style gets photo 1 only, and `notGenerated` says so (`NO_STYLE_GLOBAL_ONLY`); when one model call fails and the other returns photos, those are kept, the rest given back, and `notGenerated` says some could not be made (`SOME_NOT_MADE`). There is no count to choose: the photo form asks none, and a photo form saved when it did shows without its count (`ChatActions._turnRow`).

`makeProductPhotoChoices` (an admin route and MCP tool) is the photo step of adding a product: the new products form takes one optional JPEG, PNG or WebP per row (uploaded to the admin's `users/<uid>/chat-photos/`; `NewProductAnswerRow.imageUrl`, checked as the admin's own before the turn), and for each product sent with one the model calls it with the new product's `_id`, one product per turn. It reads the photo once, copies it as it is into `stores/<storeId>/photos/<photoId>.<jpg|png|webp>` (a GeneratedPhotos document with `source: 'original'`, no model and no prompt; no photo quota, as Meta is not called), then makes the 3 photos from it as above (its optional `notes` are the special request), counted as above. When fewer can be made, the original and those made are still returned and `notGenerated` says why: no photo style yet (photo 1 only), a daily cap reached (`CAPS_ORIGINAL_ONLY`: "...so no styled photos were made"), or the model unavailable or declining. The chat shows the result as a `productPhotoChoice` card, its photos numbered 1 (the original) to 4; the admin taps one and "Use this photo", or says its number ("use the 2nd"), and the model calls `setProductPhoto` with it. A card whose photo a later turn put on its product (that turn's product card shows one of the card's photos) shows as picked (`useChat.photoChoicesSettled`, client only). The admin's tapped pick (`ProductPhotoAnswer`: its `productId` and `photoId`, both checked against the card) becomes the turn the model acts on with `setProductPhoto`. Every photo the model made keeps its prompt (`GeneratedPhoto.prompt`; null on an original and on photos saved before prompts were kept), and both photo tools return it on each photo, which the photos card and the choice card show folded under "Prompt used for photo 1" / "... photos 2 and 3" (one fold per prompt; a card saved before each photo carried its own shows its one card-level `prompt`). No log carries a prompt.

`findProductsInPhoto` (an admin route and MCP tool) adds several products from one group photo the admin attached in the chat (its url, which the admin's attachment note gives the model): productFinder.py turns the photo upright (EXIF), asks Muse Spark (no tools) to list each product it shows, at most 12, as a shop name and a short visual phrase, leaving out things placed for scale, then asks SAM (`sam-3.1`, samModel.py, `POST /v1/responses` with the phrase and the photo, `store: false`) for each phrase's boxes, 4 calls at once. SAM takes one concept a request and answers in `output_text`: per match `<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|>` (inclusive pixel corners in a w x h image) and a mask, which is not read. The biggest match is the product; a product with no match, or whose SAM call failed, is `notFound` (all failing is a 503), and one whose box mostly covers a kept one (IoU 0.7) is the same product named twice and dropped. Both models get a copy no longer than 1600 px; each box, with an 8% margin, is cut out of the full photo as a JPEG and saved to the admin's own `users/<uid>/found/` (photoStorage.saveFoundPhoto), and the route returns each name with its cut-out's tokenless URL. The model then opens the new products form with those names and URLs, which prefill each row's name and photo (`NewProductsForm.photos`), so `makeProductPhotoChoices` takes each cut-out as the original. Each store looks through at most 20 group photos a day, and all stores together at most `PRODUCT_FIND_DAILY_CAP_ALL` (default 200; 0 turns it off), PhotoUsage counters given back when the find fails. Pillow does the cropping (`requirements.txt`). No log carries the photo, a path, a name or a phrase.

`findColorsInPhoto` (an admin route and MCP tool, also in the setup chat's toolset, so a new admin's brand colors can come from their logo; `requireAnyOwnerStore`) reads the exact main colors of an image the admin attached, since the chat model cannot read pixel values: colorFinder.py (Pillow, no model) shrinks it to 200 px (nearest pixel, so no blend of two colors is made up), leaves out transparent pixels, sorts the rest into 16 median-cut buckets, gives each bucket its most common exact color (its average when no color holds a quarter of it, a photo's noise), merges colors closer than 40 in RGB, drops those under 3% and returns at most 6 (`PHOTO_COLORS_MAX`), biggest share first, as uppercase hex with their share and whether they are neutral (channels within 24: white, black, grey). An optional `focus` ("green square logo") has SAM find that part first (productFinder.biggestMatch) and only its box is read, with no margin; no match reads the whole image (`focusFound: false`). Only the focused read costs: it takes a photo slot and a PhotoUsage counter (`colorFinds`: 50 a day per store, `COLOR_FIND_DAILY_CAP_ALL` default 500 for all stores, 0 turns it off), given back when it fails; the whole-image read takes neither. The chat shows the colors as a `photoColors` card of swatches; the admin picks a primary and/or secondary color from it (`brandColors` answer, each one of the card's colors, not the same twice), and the model saves them with `updateMyStoreTheme` (`createUserStore` while setting up). No log carries the image, a path, the focus or a color.

`setProductPhoto` (an admin route and MCP tool) shows one of the store's photos on one of its products: `Product.photoUrl` becomes that GeneratedPhotos document's download URL (only a photo of the admin's own store, never a URL sent in; no photoId takes it off). The shop's and the admin's product rows carry it (an older product has none), and the client shows it through next/image.

## Delivery

An order is picked up or delivered (`Order.fulfillment`, actions/OrderActions.py). The store's policy says which it offers (`deliveryOptions`; a store that has not said offers pickup only). `createOrder` takes `fulfillment` and, for a delivery, `delivery`: the address, the map pin's latitude and longitude, and optional notes, each checked by name (a refusal never quotes a value; empty notes are none). When only one way can be booked it is chosen for the customer; when both can, the customer chooses. Delivery can be booked only once the store has a delivery zone, and only to a pin inside one (below). The zone under the pin prices it: its flat `deliveryFee` in the store's currency, nothing once the items come to its `freeDeliveryFrom`, and an order whose items come to less than its `minimumOrder` is refused (checked on today's prices before anything is reserved, and again in the transaction). The order keeps the zone's name (`deliveryZone`) and the fee, which is part of its `total`: `payOrder` charges it and cancelling a paid order refunds it, while a return refunds its items only. Every order row carries the delivery fields (`OrderDeliveryRow`); an order stored before delivery existed reads as a pickup with no fee (`OrderActions.orderRow`, `withDeliveryDefaults`), and one priced per km before zones keeps its `deliveryDistanceKm` (null on every order since) and has a null `deliveryZone`. `handoverDays` is now the days until a pickup order is ready; a delivery takes its zone's `deliveryDays`.

In the chat, the model asks "pickup or delivery?" only when the store offers both and `getStorePolicy` lists a delivery zone (saying the fee depends on the zone); otherwise it leaves `fulfillment` out and `createOrder` picks the one way that can be booked. For a delivery, `showDeliveryPinForm` (a customer form tool) opens a pin-only map card (the map and "use where I am", nothing typed) with the store's zones shaded; it names the zone under the pin and its fee, sends only once the pin moved or found where the customer is and sits inside a zone, and refuses when the store cannot deliver (no zone yet). Its answer (`DeliveryPinAnswer`, the pin) becomes the customer's turn, refused outside every zone, with the zone's policy in its details; it is no confirmation (`chatModel.PIN_IS_NOT_CONFIRMATION`): the model gives the zone's fee, days, minimum and free delivery total, asks in chat for the address and a landmark, then separately any notes for the rider, and passes those with the pin to `createOrder`. The checkout and order cards show how each order is fulfilled, the fee and its zone, and a link to the delivery pin.

### Delivery zones

A store that delivers says where: up to `DELIVERY_ZONES_MAX` (10) zones drawn on a map, each a closed shape of 3 to 100 corners in drawing order (the last joins the first) with its own delivery policy (`DeliveryZone`, objects/store.py): a `name` (unique in the store, case ignored), a flat `deliveryFee` (0: free), `deliveryDays` (0: same day), and optionally a `minimumOrder` and a `freeDeliveryFrom` total. They are kept on the store (`Store.deliveryZones`). No zone means no delivery. A zone drawn before zones had policies (shape only) counts as none (`ruledZones`) until it is drawn again.

The admin chat opens `showDeliveryZonesForm` (an admin form tool) in the policy's delivery section instead of asking about areas or fees in text. The map (`DeliveryZonesForm.tsx`) places a corner per tap, closes a zone on a tap of its first corner (or Close zone), then asks for that zone's policy right under the map; a tap on a zone selects it to edit or delete. It keeps zones apart as they are drawn: a corner inside another zone, or a line cutting through one, is refused with that zone outlined; a tap near another zone's corner lands exactly on it (and while drawing those corners show), so neighbouring zones share a border with no gap and no overlap. Save sends every zone through `setMyDeliveryZones` (no chat or MCP tool), then answers with `DeliveryZonesAnswer`, which the server turns into "Saved N delivery zones: …" from what the store now holds, with each zone's policy in its details.

`setMyDeliveryZones` checks each zone, naming its position and the field, never a value: its shape (`deliveryZoneProblem`: too few or too many corners, a corner placed twice, sides that cross, corners in one line), its policy, a repeated name, and that no two zones overlap (`zonesOverlap`: sides that cross through each other, or a corner or the middle of a side of one strictly inside the other; a shared border or corner is no overlap). The client runs the same checks first (`deliveryZones.ts`). Shapes are flat in latitude and longitude (a town or a city, never across the 180th meridian). `deliveryAreas` stays an optional name for them all (e.g. Metro Manila).

A pin belongs to the first zone listed that holds it, its border included (`deliveryZoneAt`), so a pin on a shared border has one policy. `getStorePolicy`, `getMyStorePolicy` and `updateMyStorePolicy` send `deliveryZones`: each zone's policy without its shape (none while the store does not deliver), and the policy card lists them. The store-wide `offersFreeDelivery` and `freeDeliveryMinimum` are retired (`RETIRED_POLICY_FIELDS`): a stored policy that still has them is read past them, and the next `updateMyStorePolicy` unsets them. The per-km price and the exchange rates it needed are gone; an ExchangeRates collection left in a database is no longer read.

## Chat attachments

`sendStoreChatMessage` and `sendAdminChatMessage` (actions/ChatActions.py) take `attachments`: the Storage download URLs of up to 4 files the caller uploaded to their own folder, JPEG, PNG, WebP or PDF. The client uploads them under `users/<uid>/chat/`: `storage.rules` lets a user upload JPEG, PNG or WebP anywhere under `users/<uid>/` and a PDF only under `users/<uid>/chat/`, owner-only and under 10 MB (customers are Firebase anonymous users, so the folder is theirs too). The server takes a file from anywhere under the caller's own `users/<uid>/`. With files the text may be empty; with neither the message is refused ("Your message is empty: type a message or attach a file"). `_validAttachments` checks each URL before the turn slot, the lock or any quota, reading nothing: our bucket (`photoStorage.objectPathFromUrl`, the token dropped), the caller's own folder (`ownImagePath`, HEIC/HEIF refused), a `.jpg`, `.jpeg`, `.png`, `.webp` or `.pdf` name, no file twice; a refusal names `attachments[i]`, never the URL. A message carries one PDF at most ("Attach at most one PDF per message": two would weigh 100 images, past Muse's 50), and a form answer none ("Send a form answer without attachments").

After the lock and the chat quota each file is counted per day (UTC+8, ChatUsage counters: 20 per customer across stores, 300 per store, `CHAT_ATTACHMENTS_DAILY_CAP_ALL` (default 3000; 0 turns attachments off) for all stores; an admin counts against the store and all-stores counters only; `usageCounters.takeDailyQuotas`, shared with photos and voice). Then they are read through the Storage SDK, never by URL: `photoStorage.ownFiles` checks each one's metadata (its Storage type must be the one its extension names, at most 10 MB), then their sizes together, then `downloadFiles` reads them and checks their first bytes (JPEG `FF D8 FF`, PNG's 8-byte signature, `%PDF-`; a HEIC/HEIF `ftyp` box asks to convert), which also covers product photos.

Budgets per request and per process:
- **Bytes:** one request sends at most 20 MB of files (`CHAT_ATTACHMENTS_MAX_TOTAL_BYTES`): this message's first, refused past it; the history gets what is left. A message's own files are weighed in images only by the one-PDF rule: a PDF of 48 pages or more with images may pass Muse's 50 and get the unreadable message; the history's image budget is then 0, never below.
- **Images:** Meta takes at most 50 images per request, and a PDF counts up to its first 50 page images, so a PDF weighs 50 and an image 1 (`chatModel.MAX_IMAGES_PER_REQUEST`, `PDF_IMAGE_WEIGHT`, `IMAGE_WEIGHT`). This message's files are sent as they are; history files only while the weights stay within 50.
- **In flight:** a process holds at most 60 MB of attachment bytes at once (`CHAT_ATTACHMENT_BYTES_IN_FLIGHT_MAX`, raw, reserved from the stored sizes before any download, released when the turn ends). A message that cannot reserve its files is a 503 ("Attachment reading is unavailable right now"), its quotas given back; a history file that cannot is a note.

The files go to Muse (`muse-spark-1.3`) as base64 data URLs in the user message's content parts, after its text: an image as `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}`, a PDF as `{"type": "file", "file": {"filename": ..., "file_data": "data:application/pdf;base64,..."}}`. The raw bytes are dropped once the parts are built. The text part ends with a note per file (`Attachment 1: image receipt.jpg`); an admin's image note adds its canonical URL (`photoStorage.publicPathUrl`, no token), which the admin assistant may pass to `generateProductPhotos` (which checks it again as the JWT user's, and refuses a PDF). A turn (ChatTurn.attachments) stores each file's path, type, sanitised name and size, never its bytes; `getConversationTurns` returns path, type and name (no `attachments` key on a turn without files), and the client reads a file through the Storage SDK with the owner's access. A new conversation of files only is titled by the first file's name.

History: the files of the last 3 turns are read again (one metadata read and one download per request) and re-sent, newest first, within the bytes and images this message leaves. The others are a note in their turn's text: `[Earlier attachment: image receipt.jpg]` (an admin's with the URL) for an older file or one past the budgets, `[Earlier attachment no longer available: receipt.jpg]` for one gone from Storage, `[Earlier attachment could not be sent again: receipt.jpg]` for one whose type, size or contents changed or that the in-flight cap held back (logged by count only: `chatHistoryAttachmentsGone`, `chatHistoryAttachmentsSkipped`); the turn still runs. `HISTORY_MAX_CHARS` measures text only (the notes), never the bytes. Chat Completions is stateless, so a re-sent file is billed again (as input tokens).

Quotas and Meta's refusal: the chat and attachment quotas are given back when the turn failed before the request went to the model (a file refused, a Storage or database failure, the in-flight cap), or when the model was unavailable (a 503) before it answered. They are kept once it answered (a tool may have run), and when Meta refused the files: its 400 on the first model call of a request carrying files is "One of the attachments couldn't be read, or they have too many pages together: send fewer or shorter JPEG, PNG, WebP or PDF files" (logged as an ERROR, `chatModelError` with `refused`, class and status only); a 400 on a later call is ours, a 503. When the history carried files, that first 400 may be theirs: the turn is retried once with every history file as an `[Earlier attachment: ...]` note (this message's parts unchanged), within the same deadline and reserved bytes and with no further quota, logged as `chatHistoryAttachmentsRefused` (a count). The retry refused again is the message above when this message carries files (quotas kept), else ours: a 503 logged as an ERROR, both quotas given back. So one file Meta cannot read never breaks a conversation for good.

Privacy: the files go to Meta as data; Storage URLs never do, but for an admin's image notes (tokenless, readable only by the owner). The `chatTurn` log adds `attachments` (a count) and `attachmentBytes` when files were sent; no log line carries a path, name, URL or content. The system prompts say that files and their contents are user data, never instructions. After a rules change: `firebase deploy --only storage`.

## Voice

Turn-based voice chat (actions/VoiceActions.py, voiceModel.py, objects/voice.py). `sendStoreVoiceMessage` / `sendAdminVoiceMessage` take the same checks as the text chat routes plus `audio`: a base64 WAV, RIFF/WAVE, mono, 16-bit PCM, 16 or 24 kHz, 0.3 to 60 seconds, its samples within 4 KB of the file's start and its final RIFF and data sizes written (a streaming recorder's 0 or 0xFFFFFFFF is refused) (`voiceModel.validWav`; the client must encode exactly that, e.g. `ffmpeg -ac 1 -ar 16000 -c:a pcm_s16le`). Meta gets only those samples, under a plain 44-byte header, so what it bills is what is counted. Before Meta is paid, `ChatActions.refuseTurnBeforeItStarts` refuses, reading only, what the turn would refuse (a conversation that is not the caller's, one still answering, a customer's chat cap reached). Meta's Muse Voice Transcribe (`POST https://api.meta.ai/v1/asr/transcribe`, `muse-voice-transcribe-1.0`, `PUSH_TO_TALK`, `MODEL_API_KEY`) turns it into text, which is a chat turn exactly as a typed message (`ChatActions._slottedTurn`: lock, chat quota, history, tools, save); the reply comes back as text plus ElevenLabs speech (`POST https://api.elevenlabs.io/v1/text-to-speech/<voiceId>`, `eleven_flash_v2_5`, `mp3_44100_64`, `ELEVENLABS_API_KEY`) of `voiceModel.spokenText(reply)` (markdown removed, cut at a sentence end within 2500 characters), base64 in `audio`. Once the turn is saved the route answers 200 even when speaking fails (`audio: null`, logged `voiceSpeakFailed`: a cap reached or nothing to say at info, a failure at warning); after a speech cap is reached, audio stays null for the rest of the day. `speakChatTurn` speaks a reply already saved (a turn of the caller's own conversation, its access checked again: the admin's own store, or the customer at a set-up store), never free text. The whole voice message holds one of the chat's 4 turn slots; `speakChatTurn` has 2 of its own. Time: a planned budget of 120 s from the start; transcription 15 s, the turn 90 s, speech 15 s or what is left (skipped under 5 s). It is no hard bound: requests' timeout applies per socket operation, so the body read can take up to about twice an attempt's timeout, and the headers are bounded only by requests' per-read timeout: a trickling server can hold an attempt longer, up to Cloud Run's request timeout (300 s by default), the hard ceiling: gunicorn's gthread `--timeout` only watches the worker.

The store's voice is `Store.voiceId`, set with `updateMyStoreTheme(voiceId=...)` (`""` back to the default; any other id must pass `voiceModel.voiceExists`) and sent only to the admin, only once set; `ELEVENLABS_DEFAULT_VOICE_ID` speaks for a store with none, and for a store voice ElevenLabs refuses (logged `voiceFallback`). `listVoices` (an admin route and MCP tool) pages through ElevenLabs' `GET /v2/voices`, the `default` voices then the `saved` ones, offering only the `premade` and `generated` categories (never a cloned, professional or famous voice, a person's). Its `nextPageToken` is ours (`<listing>:<their token>`).

Daily caps (UTC+8, VoiceUsage counters, TTL index): seconds transcribed 1,800 per customer (across stores, as the chat's caps), 6,000 per store, `VOICE_DAILY_SECONDS_ALL` (default 30,000) for all stores; characters spoken 20,000 / 100,000 / `VOICE_DAILY_CHARS_ALL` (default 2,000,000). An admin counts against the store and all-stores counters only. Failed transcription gives its seconds back (not one Meta heard as nothing: billed), failed speech its characters.

**Voice mode's replies stream from ElevenLabs to the browser.** The client (`components/voice/VoiceMode.tsx`) listens with the browser's own speech recognition and sends the words as a chat message with `speak: true`. Once the turn is saved, `VoiceActions.replySpeech` makes a single-use ElevenLabs token (`voiceModel.speechToken`: `POST https://api.elevenlabs.io/v1/single-use-token/tts_websocket` with our key; it expires after 15 minutes and serves one stream) and the reply carries `speech: {token, voiceId, modelId, text}` (`ChatSpeech`: the store's voice, `eleven_flash_v2_5`, `voiceModel.spokenText(reply)`). The browser (`components/voice/elevenLabsStream.ts`) opens `wss://api.elevenlabs.io/v1/text-to-speech/<voiceId>/stream-input?model_id=…&output_format=pcm_24000&single_use_token=…`, sends the text, and plays the 24 kHz PCM chunks through Web Audio as they arrive. Our key never leaves the server. The token is the only gate, so it is issued only for a reply just saved, at most 6 a minute per user and 60 per store (`SPEECH_TOKENS_PER_MINUTE_*`, VoiceUsage counters), and the text's characters are counted against the daily character caps when it is issued; what a browser then sends with it is not ours to count, so the account's hard ceiling is the ElevenLabs API key's own credit limit (set one on a key used only by this app). Past a cap, or with ElevenLabs failing, `speech` is null (logged `voiceTokenSkipped`) and the browser's own voice speaks the reply; the counters are given back on a failure. The thinking texts still come from our server (`speakChatThinking`, cached clips).

Every call is a plain `requests` call (one Session, `trust_env` off, fixed URLs, redirects off, the body read within the attempt's timeout and a size cap), retried once through `chatModel.callWithRetry`, `chatModel.callModel`'s loop. A key that is not printable ASCII without spaces is a 503 (logged `voiceKeyInvalid`). Privacy: the recording goes to Meta and the reply text to ElevenLabs; no log line carries audio, a transcript, a reply, a voice's text or a key.

## Thinking texts

Every `@mcp_tool` route declares `thinkingText`, required (`mcpTools.mcp_tool`: at least 3 distinct trimmed phrases of up to 60 characters; mcpCreator refuses a tool without them); chatToolsCreator writes them into `chatTools.THINKING_TEXTS`. A chat turn runs in one blocking request, so the client sends `turnKey` (its own random id, 16 to 64 of `A-Za-z0-9_-`) with the message (`sendAdminChatMessage` / `sendStoreChatMessage` and both voice routes). Just before `ChatActions._runModel` runs a route tool, `chatThinking.note` saves one of its texts, picked at random, on the ChatThinking document `<userId>:<turnKey>` (form tools save none); the turn deletes it when it ends (a TTL index on `expireAt`, 120 s, deletes one left behind). Saving or deleting it never fails the turn (logged `chatThinkingNotSaved` / `chatThinkingNotCleared`). While it waits the client polls `getChatThinking(turnKey)` every second, which reads only the caller's own document, and shows the text in place of "Thinking".

Voice mode also says it: `speakChatThinking(turnKey)` speaks the current text in the store's voice (`Store.voiceId`, else `ELEVENLABS_DEFAULT_VOICE_ID`). ElevenLabs speaks each (voice, text) once: the clip is kept in ThinkingSpeech (`_id` = sha256 of the voice and the text) and reused for every turn after, so only that first time counts against the store's and all stores' daily character caps (never a customer's: the clip serves everyone). The client plays one clip at a time, never over the reply, and stops it when the reply comes. At every deploy `job_predeploy.py` runs `chatThinking.syncThinkingSpeech()`, which keeps the clips in step with the texts: it deletes those of a text no tool has any more (an edited text is a removed one and a new one), then speaks every missing (voice, text) of the default voice and each store's voice, up to 500 clips in 240 s. It never fails the deploy; what it misses is spoken on first use. The texts are the app's own strings, never a user's words.

## Payment methods

How a store takes payment is its policy's `authorizedPaymentMethods` (objects/payment.py), set with `updateMyStorePolicy`: a list of methods (`cash`, `card`, `eWallet`, `bankTransfer`, `check`, `crypto`), each once, each with what paying that way needs (`methodProblem`, shared by the model and the route): cash nothing; card and crypto their `provider` (`PaymentProvider`, only `demo` today: payments.py); eWallet the store's own `eWallets` accounts (`ewalletName`, `accountNumber`), a `provider`, or both; bankTransfer its `banks` (`bankName`, `accountNumber`); check its `checkPayees` (the account names a check is made out to). Cash, checks and bank transfers are always paid by hand to the store, cards and crypto always through a provider. It replaced the bare list `acceptedPaymentMethods` (retired: read past, unset on the next save, and not converted, so a store that answered the old question takes no order until its admin answers the new one).

Checkout: `createOrder` refuses an order at a store that has not said (`NO_PAYMENT_METHODS`), and answers `paymentOptions` (`objects.paymentOptions`): one option per way to pay, `{method, provider, eWallets, banks, checkPayees}`; an eWallet with both is two options, the provider's first. `payOrder(paymentMethod, paymentProvider)` takes one of them, checked against the store's policy after the order's status:
- with a provider, the order is claimed (pending to paid) and charged as before (payments.py). A real provider (PayMongo, Maya) will confirm the charge through its webhook instead, which needs the claim state payments.py describes;
- without one, the order moves from pending to `awaitingPayment`, charging nothing. It no longer expires (the sweep matches pending only) and keeps its units reserved. The customer may give the payment's reference (`addPaymentReference`, a chat tool, stored as `manualPaymentReference`, at most 100 characters, a later one replacing it) and a photo of its receipt (`addPaymentReceipt`, a route only, sent from the order's card: the client uploads the photo to the customer's own `users/<uid>/receipts/`, and the server reads it by path, re-encodes it as a JPEG with no metadata and at most 1600 pixels a side (`photoStorage.receiptImage`) and keeps it at `stores/<storeId>/receipts/<id>.jpg` with no download token, a later one replacing and deleting the earlier; `Order.paymentReceiptPath` is never sent, `paymentReceiptAt` is). No storage rule matches that folder, so no client reads it: the store's admin sees it through `getPaymentReceipt` (a route only, its owner store's orders only), which answers it as a JPEG data URL, and a store reset deletes it with the store's folder. Bank accounts carry `bankName`, `accountName` (the name the account is under) and `accountNumber`. The store's admin confirms the money arrived (`confirmOrderPayment`: awaitingPayment to paid, `paidAt` now), after which the order is completed as usual. `completeOrder` refuses an order awaiting payment.

An order awaiting payment counts toward the customer's `MAX_PENDING_ORDERS_PER_STORE` unpaid orders (`OrderActions.unpaidMatch`), and either side can cancel it (no refund). An order's `paymentProvider` is null for a payment made by hand; such a payment has no `paymentReference`, so cancelling it (`_refund`) or receiving one of its returns refunds nothing through the provider and leaves `refundedAt` null: the store gives the money back itself, and the tools tell the model to say so. An order stored before e-wallets were one method keeps its `gcash`, `maya` or `grabPay` (`OrderPaymentMethod`); one stored before `paymentProvider` and `manualPaymentReference` existed lacks both, and its rows send them as null. A checkout card saved before payment options carries `paymentMethods` and can no longer be answered; a policy card saved before keeps `acceptedPaymentMethods`.

## Cancelling an order

The store's admin cancels a pending, awaitingPayment or paid order (`cancelStoreOrder`); the customer cancels their own (`cancelMyOrder`, actions/FulfillmentActions.py): an unpaid one (pending or awaitingPayment) always, a paid one only as the store's policy says (`paidOrderCancellation`): `notAllowed` or not said, never; `beforeHandover`, while it is paid (not completed); `withinHours`, while it is paid and less than `paidOrderCancellationHours` (1 to 168, asked only with `withinHours`; another answer clears it) after its `paidAt`. The policy is read before the move and becomes its status guard, so a handover or payment that races the cancel is decided by the one guarded flip. A paid order cancelled by either side is refunded after the flip (`_refund`), when it was paid through a provider (see Payment methods). A refusal past the hours names them; otherwise a paid order the policy does not let go is "ask the store".

## Returns

A customer sends back items of a completed order (actions/ReturnActions.py, the Returns collection, objects/returns.py). `requestReturn` names the items by their 1-based position in the order (the customer's order rows carry no productId) with a quantity each; a later request can return what is left. Its transaction first `$inc`s the order's `_version` (so racing requests of one order WriteConflict on Atlas, as OrderLocks does for createOrder), then counts the order's requests (at most `RETURN_REQUESTS_PER_ORDER_MAX`) and the units already in a requested, approved or received return; on a standalone mongod there is no transaction and the count is best effort. A rejected or cancelled return frees its units again.

Statuses: requested → approved or rejected (the admin, `decidedAt`) or cancelled (the customer, while requested); approved → received (`receiveReturn`). Every move is a guarded status flip; a refused one re-reads the status to say why.

The policy (`objects.returnRefusal`, pure): `changedMind` needs `acceptsReturns` true and is taken within `returnWindowDays`; `defective` and `wrongItem` within `defectReportDays`, falling back to `returnWindowDays` when returns are accepted. A window of N days ends when the Nth app day (UTC+8) after the handover's app day (`completedAt`) ends. A store that has not answered the question that applies (or has no policy document) is refused with "ask the store". `nonReturnableItems` is free text: the admin rejects those requests.

Money: receiving refunds the return to the order's payment through the demo provider, whatever `refundMethod` says, and only after the flip to received (`FulfillmentActions.recordedRefund`, the refund step `cancelStoreOrder` uses too, guarded on a null `refundedAt`). With `restock=true` the same transaction as the flip `$inc`s each product's `inventory` and `_version`. A restock that would take a product's inventory plus its reserved units past `PRODUCT_INVENTORY_MAX` is refused before the flip, naming the order item (the admin receives it without restocking, or lowers that stock first); a product deleted since is skipped and the return records `restocked: false`, so `restocked: true` means every item's units went back. A return's `refundAmount` is capped at what the order's other requested, approved or received returns left of its total, so rounding never refunds more than was paid. A refund that raises leaves the return received with a null `refundedAt` (a 500): a real gateway needs a retry of those.

---

## Chat cards and forms

A chat reply carries `components` (objects/chatComponents.py), the cards and forms the client shows under it; each turn stores them as they were sent (`ChatTurns.components`), and `getConversationTurns` sends them with the turn.

- **Cards** (chatCards.py) are built by the server from what a tool returned in that turn (its tool message's JSON), never from the model's words: products, orders, the order to pay, returns, the policy, photos, a new product's photo choices, the store (with its brand colors: getUserStore sends them, and the other store tools' cards read them through `CardContext.brandColors`), and the look `updateMyStoreTheme` saved (`storeTheme`: the brand colors and photo style). A tool that refused shows nothing. A later tool that changes one row (`completeOrder` after `getStoreOrders`) updates that row on the turn's card that lists it. A turn keeps its latest `CHAT_COMPONENTS_PER_TURN_MAX` components.
- **Forms** (chatForms.py) are opened by the model with a form tool: a chat-only tool (in `chatTools.TOOLS`, never on the MCP servers) that runs in the chat route and tells the model the form is on screen: the store's map pin, new products, photos to upload, phone verification and a delivery pin. Everything else is asked in chat, one short question per message (the prompts in chatModel.py say so; voice runs the same turn, and a reply that opens a form says it is below): the store's details at setup, the policy (after getMyStorePolicy, one section at a time, asking only what is missing or to change), a return request, and pickup or delivery with the delivery address. A prompt keeps only the order of the steps and the exceptions to its rules: the fields each tool needs are in its description, which the model gets with every request. A turn saved with a component kind since retired (the old store details, policy, return and fulfillment forms) is sent without it (`chatForms.COMPONENT_KINDS`).
- **Answers**: a form, or a card that takes a choice (products to order, a payment method, paid orders to hand over, a requested return to decide, a new product's photo), has a `formId`. The client sends its answer as the chat route's `form` (one of the `*Answer` models), in that conversation. Under the conversation's lock, `chatForms.answerTurn` checks it against the component it answers (in this conversation, of the matching kind, answered once, its picks on the card), and turns it into the user's turn: `userText`, the summary the user sees (also in the reply), and `userContent`, the summary and the details the model reads (`Form answer: {...}`) and acts on with the matching tool. History replays `userContent`.
- **Setup**: an admin whose store is not set up yet (the empty store sign-up creates) chats with `chatForms.SETUP_TOOLSET` (getUserStore, createUserStore) and its own prompt, which asks the details one at a time (never the address) and creates the store once the owner confirms a summary. Once `createUserStore` succeeds the rest of that turn has the admin's tools (`chatForms.setsUpTheStore`), so the model opens the map (`showStoreLocationForm`) in the same reply; the pin the owner sends next is saved (`setUserStoreLocation`) and `suggestStoreAddress` offers the store's address from it, saved only on the owner's yes. `listMyConversations` lists those chats too.
- Phone verification runs in the browser (Firebase phone sign-in linked to the shopper's anonymous account, then `verifyCustomerPhone`), since only a Firebase ID token proves a number; its answer only tells the model it is done.

## Store reset

`resetMyStore(confirmName)` (actions/StoreActions.py, an admin route with no `@mcp_tool`, so neither the chat nor the MCP servers have it) lets an admin delete their store and start over, keeping their sign-in. It works until `STORE_RESET_ENDS_AT` (sharedConstants.py, `2026-11-05T00:00:00+08:00`, also in `client/lib/generated/serverConstants.ts`): from that instant, read through the module's `_now()` seam, it is a 403 ("Store reset is no longer available") before any read or write, in every environment, and the client hides the button. After that date the route, the dialog and the constant can go.

Steps:
1. `requireAdmin` (a customer's JWT is a 403), then the caller's store by `ownerQuery`, set up or not: none is a 400 ("You have no store to reset").
2. `confirmName` must be the store's name (NFKC, trimmed, case-folded), or `reset` for a store not set up yet (it has no name), else a 400 ("Type your store's name to confirm").
3. One `db.createTransaction` deletes the old Stores document on `_id` + `_version` (a change since the read is `STORE_CHANGED`), creates a new empty store with a **new `_id`** and its empty policy (`createEmptyStore`, which sign-up uses too), and copies the old store's counters of today's app day (UTC+8) to the new id (`_carryOverUsage`: the kinds each of ChatActions, PhotoActions and VoiceActions exports as `STORE_USAGE_KINDS`, `$max` with the same `expireAt`), so a reset never gives a day's per-store quota back. Every reader finds a store by its owner or by a set-up id, so once this commits the old id reaches nothing. On a standalone mongod there is no transaction; a failure between the writes leaves the admin with no store, which `createUserStore` makes up for.
4. The old id's documents go after the swap: the ids of its Conversations are read once, then per batch of 500 the attachment paths of their ChatTurns are read, the turns deleted (both on the `(conversationId, seq)` index) and the batch's Conversations deleted by `_id`; then Products, Orders, OrderLocks (no storeId index: an accepted collection scan), Returns, Policies and GeneratedPhotos by `storeId`. A failure here runs the Storage step first, then is logged as an ERROR (`storeResetIncomplete`, with the old store id for a cleanup and the Storage step's `storageFailures`), and the route fails; what is left is unreachable. The delete is best effort for the reset's window: a write racing it lands only on the old id and stays there as an orphan, never seen from the new store. A racing `createOrder` leaves no order (the Products go before the Orders, so it finds no product), but a stray OrderLock, a late Return, a chat turn or new conversation, or a generated photo with its GeneratedPhotos row can be left; the `storeReset` log line names `oldStoreId` for a cleanup.
5. Storage, within 60 seconds: `photoStorage.deletePrefix` deletes `stores/<oldStoreId>/` (the generated photos) and `users/<adminUid>/` (reference photos and other uploads) but never its `chat/` subfolder: the admin's uid is also their customer uid at other stores, and those chats refer to files there. Of `users/<adminUid>/chat/`, `deleteOwnChatFiles` deletes only the paths the deleted store's turns carried (one blob each, each checked to be under that subfolder; a customer's files, under another uid, are skipped). `deletePrefix` refuses any prefix that is not exactly `stores/<id>/` or `users/<id>/` (`STORE_FOLDER`, `USER_FOLDER`). A failed step is logged (`storeResetStorageFailed`, its kind and the error class) and the reset still answers 200.
6. The answer is `getUserStore`'s (`myStoreResponse`): `store: null`, the new store not being set up, so the client lands at store setup. The old shop link (its slug) is free once the swap commits: anyone may create a store with that name and link.

Kept: Users, Customers, Bootstrap (the superAdmin claim is not re-run), the daily usage counters (ChatUsage, PhotoUsage, VoiceUsage; the store's are carried over to the new id), the admin's chat files at other stores, and customers' uploads under their own `users/<uid>/`, which may belong to chats with other stores. The `storeReset` log line carries the old and new store ids, the count deleted per collection and the files deleted; never a name, the confirmation text, a path or another user's id.

---

## Environment Setup

### Required Environment Variables

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `localdev`, `localprod`, `clouddev`, or `cloudprod` |
| `JWT_SECRET_KEY` | Secret for signing JWT tokens. Scaffolding writes a random one into `server/.env`; outside localdev app.py refuses a key under 32 characters or the old placeholder |
| `LOCAL_BIND_HOST` | Optional, local runs only: the host app.py binds (default `127.0.0.1`). `0.0.0.0` opens it to the LAN, announced by a startup warning |
| `GOOGLE_CLOUD_PROJECT_ID` | GCP project ID |
| `MONGO_URI` | MongoDB Atlas connection string (clouddev and cloudprod, set on the Cloud Run service; a cloud `mongoDb()` without it refuses to start) |
| `databaseName` | The database to use. Required in cloudprod (set it on the service, its jobs and the predeploy job); elsewhere it defaults to `teststarCommerce` |
| `FIREBASE_PROJECT_ID` | Optional: the Firebase project sign-in tokens must come from (blank or unset: `sharedConstants.FIREBASE_PROJECT_ID`) |
| `FIREBASE_AUTH_EMULATOR_HOST` | Local only (`create-star-app dev` sets it): `127.0.0.1:9099`. With `ENVIRONMENT=localdev` the server accepts the Auth emulator's unsigned tokens; every other environment ignores it. Never set it on a deployed service |
| `FIREBASE_STORAGE_EMULATOR_HOST` | Local only (`create-star-app dev` sets it): `127.0.0.1:9199`, the Storage emulator. With `ENVIRONMENT=localdev`, photoStorage.py reads and writes the emulator (as it does with `STORAGE_EMULATOR_HOST`), on a loopback host only; without one, localdev photo calls are a 503 and never reach a real bucket. Every other environment ignores both |
| `FIREBASE_STORAGE_BUCKET` | Optional: the bucket of the product photos (blank or unset: `<FIREBASE_PROJECT_ID>.firebasestorage.app`) |
| `PHOTO_DAILY_CAP_ALL` | Optional: the most product photos all stores make together per day (default 1000; 0 turns photos off, "Photo generation is turned off right now"; an invalid value logs `photoDailyCapAllInvalid` and uses the default) |
| `MODEL_API_KEY` | Meta's Model API key, for the chat, the product photos and voice transcription (without it they answer a 503) |
| `ELEVENLABS_API_KEY` | ElevenLabs' API key, for spoken replies and `listVoices` (without it they answer a 503: "Voice is unavailable right now"). Limit it to text to speech and voices, with a monthly credit limit |
| `ELEVENLABS_DEFAULT_VOICE_ID` | The ElevenLabs voice a store speaks with until it picks one, and the fallback of a store voice ElevenLabs refuses |
| `VOICE_DAILY_SECONDS_ALL` | Optional: the most seconds of speech all stores transcribe together per day (default 30000; 0 turns voice messages off; an invalid value logs `sttDailyCapAllInvalid` and uses the default) |
| `VOICE_DAILY_CHARS_ALL` | Optional: the most reply characters all stores speak together per day (default 2000000; 0 turns speech off; an invalid value logs `ttsDailyCapAllInvalid` and uses the default) |

### Running Locally

```bash
# From project root (not server/); Git Bash syntax
ENVIRONMENT=localdev python server/app.py
```

The server reads `ENVIRONMENT` to determine which database and configuration to use. For local development, it connects to `localhost:27017` with database name `teststarCommerce`.

A local run (localdev, localprod) binds `127.0.0.1:5003`, reachable from this machine only: localdev accepts a sign-in with a raw uid and email, and `devLogin` signs in as any user, so a server reachable from the network lets anyone on it act as any user (against the restored prod dump, in localprod). To reach it from another device (a phone on the same Wi-Fi), set `LOCAL_BIND_HOST=0.0.0.0` in `server/.env`; app.py then prints a red warning on start-up and logs `localBindHostExposed`. Cloud Run always binds `0.0.0.0:8080`.

app.py compresses its responses (flask-compress): a JSON or CSV body of 500 bytes or more goes out as brotli (quality 4) or gzip (level 6) when the request's `Accept-Encoding` allows it, with `Vary: Accept-Encoding`. Browsers and Next.js's server-side `fetch` decode it on their own. The routes that issue an access token (`createAccessToken`) are never compressed (`NO_COMPRESSION_ENDPOINTS` in app.py): a compressed response that carries a secret next to text the caller sent can leak the secret through its length (BREACH). The sliding `jwtToken` other routes return is compressed, since the client sends its token in an `Authorization` header and never in a cookie, so no other site can make a browser send an authenticated request. Set `COMPRESS_DISABLED=1` on the Cloud Run service to turn compression off without a code deploy (or `true`, `yes`, `on`; app.py reads it when it starts and logs `compressionDisabled` once). `AppCreator._compression_block` writes the hook; `test_response_compression.py` pins it.

---

## Tools

### MongoDB Dump & Restore

```bash
# Dump production data to local backup
server/tools/dumpProdDataToLocalDev.cmd

# Restore from existing backup (no download)
server/tools/restoreLocalDev.cmd
```

`dumpProdDataToLocalDev.cmd` dumps the `starCommerce` database into the local `teststarCommerce`. It takes the source from the first of these it finds:

1. `MONGO_DUMP_URI` in the environment: the Atlas connection string of a read-only user (never commit it).
2. `MONGO_DUMP_URI` in `server/cloud.env`, which `create-star-app cloud-setup` writes (gitignored).
3. `MONGO_DUMP_HOST` and `MONGO_DUMP_USER` from the environment, else the defaults at the top of the script (empty in a new project). mongodump gets the user and asks for the password itself (masked), so it is on no command line.

With none of them it names all three and exits 1. It runs under `setlocal`, so nothing it sets reaches the calling shell, prints `Dumping from <host> as <user>`, and never prints the password. With a connection string (1 or 2) the password is on mongodump's command line, which other processes of the same Windows user and command-line auditing can read: that is why the URI is a read-only user's. The dump is always scoped to the `starCommerce` database (`--db`; the cluster is shared), and mongosh and mongorestore run without the connection string in their environment. It needs the MongoDB Database Tools (`mongodump`, `mongorestore`) on PATH (`winget install MongoDB.DatabaseTools`, then add its `bin` folder to PATH; `create-star-app doctor` checks).

### GCP Cloud Run Jobs

**Automatic refresh on deploy.** Every push to `main` runs `cloudbuild.yaml` (repo root; identical in every Star App repo, every repo-specific value is a trigger substitution) through the repo's GitHub-connected Cloud Build trigger:
1. `build`: `docker build server` with `--build-arg BUILD_CONFIG=cloudbuild.yaml`;
2. `push`: the image to Artifact Registry;
3. `update-predeploy-job`: `updateJobsFromService.py --only=<service>-predeploy` gives just that job the new image and the service's env vars; a missing job fails the build (create it once with `server/tools/create_or_update_job.bat`, job file `job_predeploy.py`, no schedule);
4. `run-predeploy-job`: `gcloud run jobs execute <service>-predeploy --wait` runs `server/job_predeploy.py` on the new image: the Pub/Sub topic check (`checkPubSubTopics.main`: a stale `pubsub_models.json`, a missing prod topic, or a lookup that failed) and the index builds against the main database (with the required-index checks). A non-zero exit fails the build before the deploy, so the old revision keeps serving. The execution gets `EXPECTED_COMMIT_SHA=$COMMIT_SHA` and the job exits 1 first when its image was built from another commit (`BUILD_COMMIT_SHA`, baked in by the Dockerfile): an overlapping build that re-imaged the job in between cannot pass this build's check. The service's containers run neither: the Dockerfile CMD is `exec gunicorn` alone, and `mainDb` builds no index at import in the cloud. Only this build runs the check: a console deploy, a manual `gcloud run deploy` or a revision rollback skips it;
5. `deploy-service`: `gcloud run services update <service> --image=... --startup-probe=...`, which waits for the new revision and fails the build when it never becomes ready. The HTTP startup probe GETs `/startupCheck`, a plain route app.py answers only once it has fully imported (`AppCreator._startup_check_block`): the services bill per request (CPU throttled outside requests), and with the default TCP probe startup ended as soon as gunicorn bound port 8080, so the worker imported the app on throttled CPU (minutes with no traffic, 7-30s on the first requests). Startup now has full CPU until the app is loaded; a revision that never answers (120 tries, 1s apart; the same 120s as gunicorn's `--timeout 120`) fails the deploy and the old revision keeps serving. Rollback: `services update` keeps the probe once a build has set it, so an image without `/startupCheck` (a git revert of the probe, a console or manual deploy of an older image, a hotfix branch cut from an older main) fails the probe and cannot be deployed until the probe is set back with `--startup-probe=tcpSocket.port=8080` (or dropped with `--startup-probe=""`). Routing traffic back to an existing older revision works: each revision keeps its own probe;
6. `update-jobs`: `server/tools/updateJobsFromService.py` gives the service's `<service>-*` Cloud Run jobs the image that just went live and the service's env vars; a failed job update fails the build (the service is already live by then). Rules (which jobs, secret refs, exit codes): see the docstring of `server/tools/updateJobsFromService.py`. New jobs and their Cloud Scheduler schedules stay manual (below).

`server/Dockerfile` refuses an image built without that build-arg (`ARG BUILD_CONFIG` + a `RUN` that exits 1), so a trigger that still holds the inline build the Cloud Run wizard stored goes red at `docker build`, before anything is pushed or deployed, and the old revision keeps serving. A local `docker build` of `server/` needs `--build-arg BUILD_CONFIG=cloudbuild.yaml` too.

**A project made by `create-star-app cloud-setup` already has the trigger.** Step `cloudbuild.trigger` creates `<service>-main` on `cloudbuild.yaml` for every push to `main`, with the substitutions the file needs, so there is nothing to switch. The user can see it with `gcloud builds triggers describe <service>-main --project=<PROJECT> --region=<REGION> --format="yaml(filename,substitutions,build)"`: `filename: cloudbuild.yaml`, the substitutions, no `build:`. Switching a trigger is only for a project that was set up by hand.

<details>
<summary>Legacy: switch a hand-made trigger from its inline build to the file</summary>

Switching a trigger to the file is a one-time step the user runs (Claude never writes to GCP). `gcloud builds triggers update github <TRIGGER> --build-config=cloudbuild.yaml` fails with INVALID_ARGUMENT (gcloud 493), so export, edit and import the trigger. The export goes to the temp folder, never the repo, so it cannot be committed by accident (in cmd, `%TEMP%\trigger.yaml`):

```powershell
gcloud builds triggers describe <TRIGGER> --project=<PROJECT> --format=yaml > $env:TEMP\trigger.yaml
# Edit it: delete the whole `build:` block, add `filename: cloudbuild.yaml`,
# and move any `build.substitutions` entries to a top-level `substitutions:` map.
# Drop `createTime` / `resourceName` if the import complains about them.
gcloud builds triggers import --project=<PROJECT> --source=$env:TEMP\trigger.yaml
# Expect filename: cloudbuild.yaml, the substitutions, and no build:
gcloud builds triggers describe <TRIGGER> --project=<PROJECT> --format="yaml(filename,substitutions,build)"
```

</details>

`updateJobsFromService.py` writes to GCP: Claude and its agents never run it, not even with `--dry-run` (it still calls gcloud). `test_cloudbuild.py` pins the yaml, the Dockerfile guard and the script (fake gcloud, no network).

After the last `COPY` and `pip install`, the Dockerfile precompiles the app (`RUN python -m compileall -q -j 0 /app`): the build context (a git checkout) has no `__pycache__`, so each new instance would otherwise compile every module it imports before gunicorn's worker can serve; a syntax error in any .py file under server/ (tests and job scripts included) now fails the build rather than a deploy or cold start.

```bash
# Create or update a Cloud Run job from a job_*.py file (pickers and a review step)
server/tools/create_or_update_job.bat

# The same thing without the pickers
python server/tools/createOrUpdateJob.py --project=<PROJECT> --region=<REGION> --service=<SERVICE> --job-file=job_X.py [--schedule="0 8 * * *" --time-zone=Asia/Manila] [--scheduler-service-account=<EMAIL>] [--yes]
```

`server/tools/createOrUpdateJob.py` (a shared file; pure stdlib) makes or updates ONE job. `create_or_update_job.bat` keeps the pickers and the review summary and calls it, and so does `create-star-app cloud-setup` for the `<service>-predeploy` job. What it does:
1. Maps `--job-file` to the job name (`job_sync_Foo.py` on service `svc` is `svc-sync-foo`). A name ending in `-once` is refused: that suffix belongs to `run_job_once.bat`'s temporary jobs.
2. Reads the service's image, env vars and runtime service account. An env var that references a secret, or a service with no env vars, is exit 2.
3. Chooses create or update from the service's job list, prints a summary (env var names and counts, never values) and asks for `y` unless `--yes`.
4. Writes the env vars to a temp JSON file (deleted afterwards) and runs `gcloud run jobs create|update` with `--env-vars-file`, `--command=python`, `--args=<file>`, `--max-retries=0` and the service's account.
5. With `--schedule` (five cron fields) and `--time-zone`, creates or updates the Cloud Scheduler job `<job>-scheduler`, which POSTs to the job's `:run` URL with an OAuth token. Its identity is `--scheduler-service-account`. Without that flag an existing scheduler keeps its account and a new one gets the service's runtime account, which then needs `roles/run.invoker` on the job. The fleet repos pass `<project>@appspot.gserviceaccount.com`. Without `--schedule` an existing scheduler is left alone.

Exit codes: 0 done (or a dry run), 1 a gcloud write failed (a failed job write skips the scheduler), 2 nothing was written. It writes to GCP, so Claude and its agents never run it, not even with `--dry-run`: they give the user the command.

```bash
# Re-sync EVERY <service>-* job with the service's current image and env vars
# (calls tools/updateJobsFromService.py; every push already does this)
server/tools/update_all_jobs.bat
```

To retry a red `update-jobs` step, rerun the build in Cloud Build. To re-sync the jobs after the service changed without a build (e.g. an env var edited in the console, which no push follows), the user runs `server/tools/update_all_jobs.bat`: it picks the project and service, previews the sync with `updateJobsFromService.py --dry-run`, asks for confirmation, then runs `updateJobsFromService.py` (the service's current image; the same job rules as the build, multiline values such as `FIREBASE_PRIVATE_KEY` intact). Running `python server/tools/updateJobsFromService.py --project=<PROJECT> --region=<REGION> --service=<SERVICE>` directly does the same without the pickers.

```bash
# Run a job_*.py ONCE on Cloud Run (e.g. a backfill after a deploy)
server/tools/run_job_once.bat
```

This creates a temporary job named `<service>-<jobfile>-once` from the
service's image and env vars, executes it, waits, prints its logs and
status, and deletes it after a successful run (then offers to delete the
local `job_*.py` too, default no; a file tracked in git is removed with
`git rm` and the removal is committed alone on `main`, never pushed; on
another branch the deletion is only staged). On failure the job is kept
and the console link, re-run command and delete command are printed. It
only ever lists job files, never Cloud Run jobs, and every name it creates,
executes or deletes ends in `-once`, so scheduled jobs are never touched.
The confirmation asks you to type the job's short name instead of `y`.
The `-once` suffix is reserved: `create_or_update_job.ps1` refuses a job
file whose derived name would end in `-once`.

All three scripts share `_gcloudTarget.ps1`: numbered project/service
pickers, a per-repo cache of the last confirmed target in
`.last_gcloud_target.json` (gitignored), a single review summary before
anything is written, and `--project=` on every gcloud call (the global
gcloud default project is never used or changed).

### Accounts and identities

`create-star-app cloud-setup` makes three service accounts per service in the GCP project. Each has one job, so a leak of one cannot do what the others can.

| Account | Used by | Roles it holds |
|---|---|---|
| `<svc>-run@<project>.iam.gserviceaccount.com` | the Cloud Run service and its jobs (the runtime identity); the identity a service call to a sibling is signed as | `roles/pubsub.publisher`, `roles/pubsub.viewer`, `roles/run.invoker` on the project |
| `<svc>-push@<project>.iam.gserviceaccount.com` | the OIDC token of every push subscription this service publishes to | none of its own; the Pub/Sub service agent may mint tokens for it |
| `<svc>-build@<project>.iam.gserviceaccount.com` | the Cloud Build trigger `<svc>-main` | `roles/run.developer`, `roles/artifactregistry.writer`, `roles/logging.logWriter`, `roles/storage.objectViewer`, and `roles/iam.serviceAccountUser` on `<svc>-run` |

The Pub/Sub service agent (`service-<projectNumber>@gcp-sa-pubsub.iam.gserviceaccount.com`) has these bindings:
- `roles/iam.serviceAccountTokenCreator` on `<svc>-push`, so Pub/Sub can sign the push token.
- `roles/pubsub.publisher` on `deadLetterTopic`, so a subscription can forward a failed message there.
- `roles/pubsub.subscriber` on each source subscription that has a dead-letter policy (`pubSubAdmin.py` sets it).

Where each account appears in the code:
- `callerAuth.py`: `PUSH_SERVICE_ACCOUNTS[0]` is this service's own `<svc>-push`; `SERVICE_CALLER_ACCOUNTS` lists the `-run` accounts of the siblings that call this one. See "Accounts and identities between sibling services" above for how a sibling is added.
- `cloudbuild.yaml` and the trigger: `<svc>-build`.
- Cloud Run service and jobs: `<svc>-run` (`--service-account`).
- The dead-letter dashboard's `<name>-run` account is the second entry of every sibling's `PUSH_SERVICE_ACCOUNTS` (`PUSH_SERVICE_ACCOUNTS[1]`, never `[0]`): its "Retry Prod" replays a dead letter with an ID token minted for exactly that endpoint, and the sibling accepts it as a push caller. The dashboard's own tuple holds its push account alone, so a replay token can never create a dead letter.

`create-star-app doctor --cloud` reads these bindings and prints the exact command for any that is missing; it never applies a fix.

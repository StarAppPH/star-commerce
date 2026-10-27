"""Local Pub/Sub dev tool: `dispatch` forwards `*_devData` captures, `push` replays a mock message.

In `localdev` a `@pubSubDecorator` function never contacts the other services:
`pubSub.publishMessage` only appends the payload to `<projectName>_devData` in
the shared local Mongo `pubSubMockDb` and returns, so a button click in a dev
UI goes nowhere. This tool closes that gap by wrapping a captured mock message
in the same push envelope Google Cloud Pub/Sub would POST and sending it to a
consumer's local Flask dev server route `/<consumeFunctionName>`. It has two
subcommands:

    dispatch   background loop that forwards NEW `*_devData` captures to every
               addressed consumer (Ctrl-C stops it)
    push       manual replay of one (or every) mock message, the only way to
               deliver the generator-made `pubSub`-type messages (the
               per-publisher collections such as `accounting`,
               `warehouseManagementService`, `ecommerceV2`, `salesApp`), which
               never enter `*_devData`

The same file lives in server/tools/ of every Pub/Sub repo and is
byte-identical: each copy imports AppConfig, mongoDb, pubSub, utils,
servicePortCheck and services.* from the repo it sits in, so it speaks for
that repo's project name, dev port and known sibling services. Run it from
the repo root with that repo's interpreter:

    ENVIRONMENT=localdev server/venv/Scripts/python server/tools/devPubSub.py dispatch
    ... dispatch --since 2026-09-21T00:00:00+08:00   also enqueue captures stored since then
    ... dispatch --once --dry-run                    one pass, print envelopes, POST nothing
    ... dispatch --no-prompt                         never ask to start an unreachable consumer
    ... dispatch --auto-start                        start unreachable consumers without asking
    ... dispatch --target accounting=http://127.0.0.1:5001/ --repo accounting=D:\\src\\accountingSystem
    ... dispatch --only-publisher accounting         watch only accounting_devData
    ... dispatch --retry-failed                      re-queue deliveries the consumer rejected
    ... dispatch --reset                             drop the dispatcher state database first

    ENVIRONMENT=localdev server/venv/Scripts/python server/tools/devPubSub.py push --publisher accounting --index 0 --repeat 2
    ... push --publisher accounting --function createUnit_accounting --repeat 2
    ... push --publisher accounting --dev-data --function createUnit_accounting     (reads accounting_devData)
    ... push --all --dry-run                                                        (list every message addressed to this repo)
    ... push --all --publisher warehouseManagementService
    ... push --consumer accounting --publisher ecommerceV2 --index 3                 (POSTs to accounting's dev URL)

(SalesApp's venv has no flask, so its copy is run with the global `python`.)

Consumers are resolved from the owning repo's server/services/*.py plus the
repo itself; a consumer the repo has no service file for is skipped with a
warning. accountingSystem and ecommerce know all three sibling services, so
run `dispatch` from either of those to cover every service (SalesApp's copy
only knows accounting). Consumer URLs are restricted to loopback addresses so
mock traffic can never be replayed into a deployed service. Every run must
pass the localdev gate first: the Mongo/pubSub imports are deferred inside
the functions that need them, so nothing connects to a database or loads the
app until that gate has passed. Only `requests` and `pymongo` are used beyond
the standard library.

dispatch
--------
The messageId sent to a consumer is the mock document's `_id`, so the
consumer's own `PubSubMessages` marker makes restarts and duplicates safe: a
re-send of something already handled just answers "Message already processed".

State (watermark per collection, one delivery row per doc x consumer) lives in
the SEPARATE Mongo database `pubSubDevDispatcher`. It must never be a
collection inside `pubSubMockDb`: `pubSub.MockDataConsumer` iterates every
collection there and test_pubSubContracts.py would break.

The first run seeds every watermark with "now" (or just past the newest capture
already stored, whichever is later) because the `*_devData` collections grow
with every click and nobody wants the backlog replayed. `--since <ISO datetime>`
moves the watermarks back so captures stored after that moment are ALSO
enqueued; it is not a replay of deliveries already tracked. A naive `--since`
value is read as UTC. To re-send something use `--retry-failed` (for rows the
consumer rejected) or `push`.

Consumers that are not running accumulate pending rows and receive everything
once they come up. Handler failures (400/403) and envelopes that cannot be
built are marked failed and not retried automatically (no marker was written,
so a re-send is safe).

All four repo copies share that state database but know different consumers,
so two rules keep them consistent. A capture addressed to a consumer this copy
has no service file for is stored as `skipped`; the next copy that DOES know
the consumer adopts those rows (flips them to pending) at startup and on every
pass, so nothing is silently lost behind the shared watermark. And only one
dispatcher may run at a time: a lock document with a heartbeat is held in the
state database, a second copy refuses to start while the heartbeat is fresh
(consumers that mark a messageId only after a successful handler would
otherwise run it twice), and a stale lock from a crashed run is taken over.

push
----
Flags:
    --publisher   pubSubMockDb collection, i.e. the publishing project's name (required unless --all)
    --dev-data    read <publisher>_devData (UI captures) instead of the generator collection
    --function    consumeFunctionName filter, e.g. createUnit_accounting
    --index       the message's `index` field
    --all         every message in --publisher (or in every generator collection) addressed to --consumer
    --consumer    the receiving project (default: this repo's project); picks the URL when --url is not given
    --url         consumer base URL override, loopback only (default: the consumer's local dev URL)
    --message-id  fixed messageId (single message only); default is a fresh random id per invocation
    --repeat N    send the same messageId N times: the 2nd+ attempt should answer
                  "Message already processed" (the consumer's idempotency marker)
    --dry-run     print the envelope(s), POST nothing
    --timeout     HTTP timeout in seconds

A fresh messageId per invocation means a message can be replayed as often as
needed while a handler is being fixed. Exit status: 0 when every POST answered
2xx, 1 otherwise, 2 for usage / environment errors.
"""
import argparse
import base64
import datetime
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

# server/tools/<this file> -> server/ -> repo root -> parent directory that
# holds every sibling checkout. server/ goes on sys.path so the owning repo's
# AppConfig, mongoDb, pubSub, utils, servicePortCheck and services.* import.
SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
REPO_PARENT = REPO_ROOT.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# Snapshot of the shell environment taken before AppConfig's load_dotenv merges
# this repo's server/.env into os.environ. A spawned sibling server must start
# from THIS environment: the sibling's own load_dotenv uses override=False, so
# this repo's JWT secret, project id, API keys and *_PROD_URL values would
# otherwise win over the sibling's .env. requireLocalDev() is what triggers
# that load, and nothing above this line imports AppConfig.
BASE_ENVIRON = dict(os.environ)

DEV_DATA_SUFFIX = '_devData'
HTTP_TIMEOUT_SECONDS = 120
REACHABILITY_TIMEOUT_SECONDS = 0.5

STATE_DATABASE_NAME = 'pubSubDevDispatcher'
UNREACHABLE_WARNING_INTERVAL_SECONDS = 30
SPAWN_WAIT_SECONDS = 15
# MockData stamps dateOfCreation before inspect.stack() runs and the insert
# lands, so a capture can appear in Mongo slightly BELOW a watermark that a
# concurrent poll already advanced. Re-reading this much overlap catches it;
# the insert-only upsert on deliveries keeps it from being enqueued twice.
WATERMARK_OVERLAP = datetime.timedelta(seconds=2)
CLOCK_SKEW_TOLERANCE = datetime.timedelta(seconds=60)
DEV_DATA_INDEX = [('dateOfCreation', 1), ('index', 1)]
DELIVERIES_INDEX = [('status', 1), ('dateOfCreation', 1), ('index', 1)]
LOCK_ID = 'lock'
LOCK_MIN_TTL_SECONDS = 15
LOCK_TTL_INTERVALS = 5

# generator messages are addressed by index; the dispatcher's date-first order is intentional
MESSAGE_ORDER = [('index', 1), ('dateOfCreation', 1)]


# ======================================================================
# transport helpers
# ======================================================================

def requireLocalDev():
    """Exit with status 2 unless ENVIRONMENT resolves to localdev.

    Call this before importing mongoDb / pubSub so a misconfigured shell never
    opens a database connection or loads the app. Only AppConfig's Environment
    mixin is built: the full AppConfig() would exec every services/*.py (each
    pinging its URL) and demand every PUBSUB_ENABLED_* variable. Importing
    AppConfig reads server/.env only when the shell already has an ENVIRONMENT
    starting with 'local', and load_dotenv never overrides a shell value.
    """
    try:
        from AppConfig import Environment
        isLocalDev = Environment().getisLocalDevEnvironment()
    except Exception as error:
        isLocalDev = False
        reason = str(error)
    else:
        reason = f"ENVIRONMENT is {os.getenv('ENVIRONMENT')!r}"
    if not isLocalDev:
        print(
            'This tool only runs in the localdev environment '
            f'({reason}). Run it as: ENVIRONMENT=localdev server/venv/Scripts/python server/tools/devPubSub.py <dispatch|push> ...',
            file=sys.stderr)
        raise SystemExit(2)


def assertLoopbackUrl(url, flagName):
    """Exit 2 unless the URL's host is localhost, 127.0.0.0/8 or ::1.

    The tools replay mock payloads; a --url / --target pointing at a Cloud Run
    host would push them into a deployed service.
    """
    host = urlsplit(url).hostname
    isLoopback = False
    if host is not None:
        if host.lower() == 'localhost':
            isLoopback = True
        else:
            try:
                isLoopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                isLoopback = False
    if not isLoopback:
        print(f'{flagName} must point at a loopback address (localhost, 127.x.x.x or ::1), got {url!r}',
              file=sys.stderr)
        raise SystemExit(2)


class Consumer:
    """A local dev server that can receive push messages."""

    def __init__(self, projectName, baseUrl, repoRoot, flagName='--target'):
        assertLoopbackUrl(baseUrl, flagName)
        self.projectName = projectName
        self.baseUrl = baseUrl.rstrip('/') + '/'
        self.repoRoot = str(repoRoot)
        self._python = None

    def getPython(self):
        """Interpreter that can run this repo's server/app.py.

        Prefers <repo>/server/venv/Scripts/python.exe when it exists and can
        import flask (SalesApp's venv cannot), otherwise the global `python`.
        Resolved lazily because probing spawns a subprocess.
        """
        if self._python is None:
            self._python = resolvePython(self.repoRoot)
        return self._python

    def __repr__(self):
        return f'Consumer({self.projectName!r}, {self.baseUrl!r}, {self.repoRoot!r})'


def resolvePython(repoRoot):
    venvPython = os.path.join(repoRoot, 'server', 'venv', 'Scripts', 'python.exe')
    if os.path.isfile(venvPython):
        try:
            probe = subprocess.run([venvPython, '-c', 'import flask'],
                                   capture_output=True,
                                   timeout=30)
            if probe.returncode == 0:
                return venvPython
        except (OSError, subprocess.SubprocessError):
            pass
    return 'python'


def parseOverrides(pairs, label):
    """['name=value', ...] -> {name: value}; exits 2 on a malformed pair."""
    overrides = {}
    for pair in pairs or []:
        if '=' not in pair:
            print(f'--{label} expects name=value, got {pair!r}', file=sys.stderr)
            raise SystemExit(2)
        name, value = pair.split('=', 1)
        overrides[name.strip()] = value.strip()
    return overrides


def requireKnownConsumer(consumers, name):
    """Exit 2 when `name` is not a resolved consumer; returns the Consumer."""
    if name not in consumers:
        print(f'unknown consumer {name!r}; known: {", ".join(sorted(consumers))}',
              file=sys.stderr)
        raise SystemExit(2)
    return consumers[name]


def ownDevPort():
    """The PORT this repo's generated app.py binds, from its AppCreator.py."""
    from servicePortCheck import readDeclaredPort, APP_CREATOR_RELATIVE_PATH
    appCreatorPath = REPO_ROOT / APP_CREATOR_RELATIVE_PATH
    port = readDeclaredPort(appCreatorPath) if appCreatorPath.is_file() else None
    if port is None:
        print(f'cannot read the `PORT: int = ...` line from {appCreatorPath}; '
              'this repo\'s own dev URL is unknown', file=sys.stderr)
        raise SystemExit(2)
    return port


def resolveConsumers(urlOverrides=None, repoOverrides=None):
    """{projectName: Consumer} for every sibling service plus this project.

    Sibling services come from `services.AllServices.serviceGetter`
    (`getProjectName()` -> `path.dev`, repo root = <parent of this repo>/
    <localRepoDirName>). This project listens on the PORT declared in its own
    AppCreator.py. `urlOverrides` / `repoOverrides` are {projectName: value}
    maps from `--target name=url` / `--repo name=path`.
    """
    from AppConfig import AppConfig
    from services.AllServices import serviceGetter

    urlOverrides = urlOverrides or {}
    repoOverrides = repoOverrides or {}
    consumers = {}
    for service in serviceGetter.listOfServices:
        projectName = service.getProjectName()
        consumers[projectName] = Consumer(
            projectName,
            urlOverrides.get(projectName) or service.path.dev,
            repoOverrides.get(projectName) or REPO_PARENT / service.localRepoDirName)

    ownProjectName = AppConfig().projectName
    consumers[ownProjectName] = Consumer(
        ownProjectName,
        urlOverrides.get(ownProjectName) or f'http://127.0.0.1:{ownDevPort()}/',
        repoOverrides.get(ownProjectName) or REPO_ROOT)

    for projectName in list(urlOverrides) + list(repoOverrides):
        requireKnownConsumer(consumers, projectName)
    return consumers


def readMockDocs(collection, query, sortKeys, projection=None):
    """Mock docs from a pubSubMockDb collection, sorted by the given keys.

    Uses the collection handle directly instead of `pubSubMockDb.read()`: that
    wrapper has no sort parameter and prints a timing line per call, which
    would flood the dispatcher's console once a second.
    """
    from mongoDb import pubSubMockDb
    return list(pubSubMockDb.db[collection].find(query, projection).sort(sortKeys))


def mockFromDoc(doc):
    """Rebuild the pubSub.MockData model from a stored mock document."""
    from pubSub import MockData
    return MockData(**doc)


def isoZ(moment):
    """RFC 3339 UTC timestamp with a Z suffix, as Pub/Sub emits publishTime."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    moment = moment.astimezone(datetime.timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def buildPushEnvelope(payload, consumeFunctionName, messageId, projectId, publishTime=None):
    """The JSON body a Pub/Sub push subscription POSTs to a consumer.

    `payload` is what `MockData.getData()` returns: a dict or a plain string.
    Both `messageId`/`publishTime` and their snake_case twins are set because
    Pub/Sub sends both.
    """
    from pubSub import pubSub
    # The publisher's own serialization boundary (dict -> JSON bytes with the
    # datetime-safe encoder, str -> UTF-8, anything else -> TypeError). It is
    # underscore-prefixed in pubSub.py; making it public would mean editing
    # that file, which these tools must not do.
    raw = pubSub._serialize_for_pubsub(payload)
    publishedAt = isoZ(publishTime or datetime.datetime.now(datetime.timezone.utc))
    return {
        'message': {
            'data': base64.b64encode(raw).decode('ascii'),
            'messageId': messageId,
            'message_id': messageId,
            'publishTime': publishedAt,
            'publish_time': publishedAt,
            'attributes': {
                'originalTopicPath': f'projects/{projectId}/topics/{consumeFunctionName}',
            },
        },
        'subscription': f'projects/{projectId}/subscriptions/{consumeFunctionName}',
    }


def envelopeFromMock(mock, messageId, projectId):
    """Push envelope for a MockData model, stamped with its dateOfCreation."""
    return buildPushEnvelope(mock.getData(), mock.consumeFunctionName, messageId,
                             projectId, publishTime=mock.dateOfCreation)


def pushUrl(baseUrl, consumeFunctionName):
    return baseUrl.rstrip('/') + '/' + consumeFunctionName


def postPushMessage(baseUrl, consumeFunctionName, envelope, timeout=HTTP_TIMEOUT_SECONDS):
    """POST the envelope to <baseUrl>/<consumeFunctionName>.

    Returns (statusCode, body) where body is the parsed JSON when the server
    sent JSON, else the raw text. `requests.ConnectionError` propagates so the
    caller can decide between retrying and offering to start the server.
    """
    response = requests.post(pushUrl(baseUrl, consumeFunctionName), json=envelope, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body


def isReachable(baseUrl, timeout=REACHABILITY_TIMEOUT_SECONDS):
    """True when something accepts TCP connections on the URL's host:port."""
    parts = urlsplit(baseUrl)
    host = parts.hostname or '127.0.0.1'
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def startCommand(consumer):
    """The two shell lines that start this consumer's dev server on Windows."""
    return [
        f'cd {consumer.repoRoot}',
        f'set ENVIRONMENT=localdev && {consumer.getPython()} server\\app.py',
    ]


def printStartHint(consumer, baseUrl=None):
    """'consumer X at <url> is not reachable.' followed by its start command."""
    print(f'consumer {consumer.projectName} at {baseUrl or consumer.baseUrl} is not reachable.')
    print('Start it with:')
    for line in startCommand(consumer):
        print(f'  {line}')


def spawnEnvironment():
    """Environment for a spawned sibling server: the pre-.env shell plus localdev."""
    return {**BASE_ENVIRON, 'ENVIRONMENT': 'localdev'}


def spawnServer(consumer):
    """Start the consumer's server/app.py in a new console window.

    Runs app.py directly (not via AppCreator) so each repo keeps its own
    `__main__` extras, with cwd at that repo's root because app.py resolves
    relative paths such as server/keys/ from there. The child gets the shell
    environment captured before this repo's .env was loaded (see BASE_ENVIRON).
    """
    return subprocess.Popen(
        [consumer.getPython(), os.path.join('server', 'app.py')],
        cwd=consumer.repoRoot,
        env=spawnEnvironment(),
        creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))


def responseMessage(body):
    """ApiResponse.message when the body is one, else a short text form."""
    if isinstance(body, dict):
        message = body.get('message')
        if message is not None:
            return str(message)
        return json.dumps(body)[:200]
    return str(body)[:200]


def describeResponse(status, body):
    """'200 Unit received from accounting' style one-liner for log lines."""
    return f'{status} {responseMessage(body)}'


def mockValidityWarning(mock):
    """The 3-day-rule message from MockData.checkIfValid, or None when fresh."""
    try:
        mock.checkIfValid()
    except Exception as error:
        return str(error)
    return None


# ======================================================================
# dispatch
# ======================================================================

def utcNow():
    return datetime.datetime.now(datetime.timezone.utc)


def parseSince(value):
    if value is None or value.lower() == 'now':
        return utcNow()
    try:
        moment = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        print(f'--since expects an ISO datetime or "now", got {value!r}', file=sys.stderr)
        raise SystemExit(2)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def timestamp():
    return datetime.datetime.now().strftime('%H:%M:%S')


def log(line):
    print(f'{timestamp()}  {line}', flush=True)


class Dispatcher:

    def __init__(self, args):
        from AppConfig import AppConfig
        from mongoDb import pubSubMockDb

        self.args = args
        self.projectId = AppConfig().projectId
        self.projectName = AppConfig().projectName
        self.environment = AppConfig().getEnvironment()
        self.mockDb = pubSubMockDb
        self.stateDb = pubSubMockDb.client[STATE_DATABASE_NAME]
        self.watermarks = self.stateDb['watermarks']
        self.deliveries = self.stateDb['deliveries']
        self.locks = self.stateDb['locks']
        self.lockHolder = f'{self.projectName}:{os.getpid()}'
        self.lockHeld = False
        self.consumers = resolveConsumers(parseOverrides(args.target, 'target'),
                                          parseOverrides(args.repo, 'repo'))
        # --dry-run must not write state: watermarks and enqueued row ids live here instead
        self.dryRunWatermarks = {}
        self.dryRunRowIds = set()
        self.seedSince = parseSince(args.since)
        self.explicitSince = args.since is not None
        self.unknownConsumersWarned = set()
        self.skewWarned = set()
        self.askedConsumers = set()
        self.lastUnreachableWarning = {}
        self.wasUnreachable = set()

    # ---- startup -----------------------------------------------------

    def prepare(self):
        if not self.args.dry_run:
            self.acquireLock()
        if self.args.reset and not self.args.dry_run:
            self.mockDb.client.drop_database(STATE_DATABASE_NAME)
            log(f'dropped state database {STATE_DATABASE_NAME}')
            self.acquireLock()  # the drop took the lock document with it
        if self.args.retry_failed and not self.args.dry_run:
            result = self.deliveries.update_many(
                {'status': 'failed'},
                {'$set': {'status': 'pending', 'updatedAt': utcNow()}})
            log(f'--retry-failed: {result.modified_count} failed deliveries re-queued')
        if not self.args.dry_run:
            self.ensureIndexes()
            self.adoptSkippedRows()
        if self.explicitSince:
            for collection in self.devDataCollections():
                self.setWatermark(collection, self.seedSince)
        self.printBanner()

    def adoptSkippedRows(self):
        """Take over rows another repo's copy skipped for a consumer this copy knows.

        Rows already carry collection/docId/consumer, so flipping them to
        pending is enough for deliver() to send them.
        """
        result = self.deliveries.update_many(
            {'status': 'skipped', 'consumer': {'$in': list(self.consumers)}},
            {'$set': {'status': 'pending', 'updatedAt': utcNow()}})
        if result.modified_count > 0:
            log(f'adopted {result.modified_count} deliveries another dispatcher copy skipped '
                f'(consumers known here: {", ".join(sorted(self.consumers))})')

    # ---- single-dispatcher lock ---------------------------------------

    def lockTtl(self):
        return datetime.timedelta(
            seconds=max(LOCK_MIN_TTL_SECONDS, LOCK_TTL_INTERVALS * self.args.interval))

    def acquireLock(self):
        """Exit 2 while another dispatcher's heartbeat is fresh; else take the lock."""
        existing = self.locks.find_one({'_id': LOCK_ID})
        if existing is not None and existing.get('holder') != self.lockHolder:
            age = utcNow() - existing['heartbeat']
            if age < self.lockTtl():
                print(f"another dispatcher ({existing.get('holder')}) is running; stop it or wait "
                      f'for its heartbeat to expire (last seen {int(age.total_seconds())} s ago, '
                      f'expires after {int(self.lockTtl().total_seconds())} s)', file=sys.stderr)
                raise SystemExit(2)
            log(f"taking over the stale lock of {existing.get('holder')} "
                f'(heartbeat {int(age.total_seconds())} s old)')
        self.locks.update_one(
            {'_id': LOCK_ID},
            {'$set': {'holder': self.lockHolder, 'heartbeat': utcNow(), 'startedAt': utcNow()}},
            upsert=True)
        self.lockHeld = True

    def refreshLock(self):
        """Renew the heartbeat; exit 2 when another copy has taken the lock meanwhile.

        A copy can block past the TTL (a y/N prompt, the spawn wait, a slow
        handler under the HTTP timeout) and lose the lock to another copy;
        it must not keep delivering unlocked. releaseLock() is holder-scoped,
        so the other copy's lock survives this exit.
        """
        result = self.locks.update_one({'_id': LOCK_ID, 'holder': self.lockHolder},
                                       {'$set': {'heartbeat': utcNow()}})
        if result.matched_count == 0:
            current = self.locks.find_one({'_id': LOCK_ID})
            holder = current.get('holder') if current else 'nobody (lock document gone)'
            self.lockHeld = False
            print(f'lock lost to {holder}; this dispatcher stops so only one copy delivers',
                  file=sys.stderr)
            raise SystemExit(2)

    def releaseLock(self):
        if self.lockHeld:
            self.locks.delete_one({'_id': LOCK_ID, 'holder': self.lockHolder})
            self.lockHeld = False

    def ensureIndexes(self):
        # create_index never creates a collection, so only EXISTING *_devData
        # collections are touched and pubSubMockDb gains nothing new.
        for collection in self.devDataCollections():
            self.mockDb.db[collection].create_index(DEV_DATA_INDEX)
        self.deliveries.create_index(DELIVERIES_INDEX)

    def printBanner(self):
        print(f'devPubSub dispatch  project={self.projectName}  environment={self.environment}  '
              f'projectId={self.projectId}  interval={self.args.interval}s'
              + ('  DRY RUN' if self.args.dry_run else ''))
        print(f'repo: {REPO_ROOT}')
        print(f'state database: {STATE_DATABASE_NAME} (watermarks, deliveries)')
        print('consumers:')
        for name in sorted(self.consumers):
            consumer = self.consumers[name]
            state = 'reachable' if isReachable(consumer.baseUrl) else 'NOT reachable'
            print(f'  {name:<28} {consumer.baseUrl:<28} {consumer.repoRoot}  [{state}]')
        print('watermarks:')
        for collection in self.devDataCollections():
            print(f'  {collection:<40} {self.getWatermark(collection).isoformat()}')
        pending = self.deliveries.count_documents({'status': 'pending'})
        failed = self.deliveries.count_documents({'status': 'failed'})
        print(f'deliveries: {pending} pending, {failed} failed')
        print('watching... (Ctrl-C to stop)', flush=True)

    # ---- state -------------------------------------------------------

    def devDataCollections(self):
        names = [x for x in self.mockDb.getAllCollectionNames() if x.endswith(DEV_DATA_SUFFIX)]
        if self.args.only_publisher:
            names = [x for x in names
                     if any(x.startswith(prefix) for prefix in self.args.only_publisher)]
        return sorted(names)

    def getWatermark(self, collection):
        if self.args.dry_run and collection in self.dryRunWatermarks:
            return self.dryRunWatermarks[collection]
        row = self.watermarks.find_one({'_id': collection})
        if row is not None:
            return row['dateOfCreation']
        seed = self.seedSince if self.explicitSince else self.defaultSeed(collection)
        self.setWatermark(collection, seed)
        return seed

    def defaultSeed(self, collection):
        """Now, or just past the newest stored capture if that is later.

        SalesApp's MockData stamps naive local time (GMT+8), so its captures
        sit up to 8 h in the future; seeding with plain "now" would deliver
        that whole window as new. When the newest doc wins, the seed is its
        dateOfCreation + WATERMARK_OVERLAP + 1 ms: enqueue re-reads from
        watermark - WATERMARK_OVERLAP with an inclusive $gte, and with no
        delivery row yet to dedupe against, a lower bound equal to that doc's
        (millisecond-precision) stamp would deliver it, and any capture within
        2 s before it, on every cold start or --reset.
        """
        seed = self.seedSince
        newest = self.mockDb.db[collection].find_one(
            {}, {'dateOfCreation': 1}, sort=[('dateOfCreation', -1)])
        if newest is not None and newest['dateOfCreation'] > seed:
            seed = newest['dateOfCreation'] + WATERMARK_OVERLAP + datetime.timedelta(milliseconds=1)
        return seed

    def setWatermark(self, collection, moment):
        if self.args.dry_run:
            self.dryRunWatermarks[collection] = moment
            return
        self.watermarks.update_one(
            {'_id': collection},
            {'$set': {'dateOfCreation': moment, 'updatedAt': utcNow()}},
            upsert=True)

    # ---- enqueue -----------------------------------------------------

    def enqueue(self):
        """Turn new mock docs into pending delivery rows; returns the new rows."""
        newRows = []
        for collection in self.devDataCollections():
            watermark = self.getWatermark(collection)
            docs = readMockDocs(collection,
                                {'dateOfCreation': {'$gte': watermark - WATERMARK_OVERLAP}},
                                DEV_DATA_INDEX,
                                projection={'data': 0})
            for doc in docs:
                self.warnOnClockSkew(collection, doc)
                for consumerName in doc.get('projectNameConsumers', []):
                    if consumerName == doc.get('projectName'):
                        continue
                    row = self.deliveryRow(collection, doc, consumerName)
                    if consumerName not in self.consumers:
                        row['status'] = 'skipped'
                        row['message'] = 'unknown consumer'
                        if consumerName not in self.unknownConsumersWarned:
                            self.unknownConsumersWarned.add(consumerName)
                            log(f'{collection}  consumer {consumerName!r} is unknown here; its deliveries '
                                f'are skipped. Run the dispatcher from a repo whose server/services has '
                                f'{consumerName}')
                    if self.rememberRow(row):
                        newRows.append(row)
                if doc['dateOfCreation'] > watermark:
                    watermark = doc['dateOfCreation']
            if docs:
                self.setWatermark(collection, watermark)
        return newRows

    def rememberRow(self, row):
        """Record a delivery row once; True when it was not known before."""
        if self.args.dry_run:
            if row['_id'] in self.dryRunRowIds or self.deliveries.find_one({'_id': row['_id']}):
                return False
            self.dryRunRowIds.add(row['_id'])
            return True
        result = self.deliveries.update_one({'_id': row['_id']}, {'$setOnInsert': row}, upsert=True)
        return result.upserted_id is not None

    def warnOnClockSkew(self, collection, doc):
        if collection in self.skewWarned:
            return
        if doc['dateOfCreation'] > utcNow() + CLOCK_SKEW_TOLERANCE:
            self.skewWarned.add(collection)
            log(f'{collection}  dateOfCreation {doc["dateOfCreation"].isoformat()} is in the future: '
                'publisher clock/tz skew (SalesApp stamps naive local time)')

    def deliveryRow(self, collection, doc, consumerName):
        return {
            '_id': f"{doc['_id']}|{consumerName}",
            'collection': collection,
            'docId': doc['_id'],
            'consumeFunctionName': doc['consumeFunctionName'],
            'consumer': consumerName,
            'dateOfCreation': doc['dateOfCreation'],
            'index': doc.get('index', 0),
            'status': 'pending',
            'attempts': 0,
            'httpStatus': None,
            'message': None,
            'updatedAt': utcNow(),
        }

    # ---- deliver -----------------------------------------------------

    def deliver(self, newRows):
        if self.args.dry_run:
            # no reachability gate, no prompts, no spawns: just print the envelopes
            for row in newRows:
                if row['status'] == 'pending':
                    self.deliverRow(row, self.consumers[row['consumer']])
            return
        rows = list(self.deliveries.find({'status': 'pending'}).sort(DEV_DATA_INDEX))
        reachableNow = {}
        for row in rows:
            consumer = self.consumers[row['consumer']]
            name = consumer.projectName
            if name not in reachableNow:
                reachableNow[name] = self.probeConsumer(consumer)
            if not reachableNow[name]:
                continue
            if not self.deliverRow(row, consumer):
                # ConnectionError mid-pass: skip this consumer's remaining rows
                reachableNow[name] = False

    def probeConsumer(self, consumer):
        """One TCP probe per consumer per pass; handles the unreachable case."""
        name = consumer.projectName
        if isReachable(consumer.baseUrl):
            if name in self.wasUnreachable:
                self.wasUnreachable.discard(name)
                log(f'consumer {name} is up at {consumer.baseUrl}')
            return True
        spawned = self.handleUnreachable(consumer)
        # only a fresh spawn justifies a second probe in the same pass
        return spawned and isReachable(consumer.baseUrl)

    def deliverRow(self, row, consumer):
        """POST one row; returns False only when the consumer refused the connection."""
        target = f"{row['consumeFunctionName']} -> {consumer.projectName} ({consumer.baseUrl})"
        doc = self.mockDb.db[row['collection']].find_one({'_id': row['docId']})
        if doc is None:
            self.failRow(row, target, f"mock document {row['docId']} no longer exists")
            return True
        try:
            envelope = envelopeFromMock(mockFromDoc(doc), row['docId'], self.projectId)
        except Exception as error:
            # a poison capture (validation or serialization error) must not
            # wedge the loop: mark it failed and move on
            self.failRow(row, target, f'{type(error).__name__}: {error}')
            return True
        if self.args.dry_run:
            log(f"{row['collection']}  {target}  DRY RUN")
            print(json.dumps(envelope, indent=2))
            return True
        try:
            status, body = postPushMessage(consumer.baseUrl, row['consumeFunctionName'],
                                           envelope, timeout=self.args.timeout)
        except requests.ConnectionError:
            self.deliveries.update_one({'_id': row['_id']},
                                       {'$inc': {'attempts': 1},
                                        '$set': {'updatedAt': utcNow()}})
            self.handleUnreachable(consumer)
            return False
        except Exception as error:
            self.failRow(row, target, f'{type(error).__name__}: {error}')
            return True
        outcome = 'delivered' if 200 <= status < 300 else 'failed'
        self.finishRow(row, outcome, status, responseMessage(body))
        log(f"{row['collection']}  {target}  {describeResponse(status, body)}")
        return True

    def failRow(self, row, target, message):
        log(f"{row['collection']}  {target}  FAILED {message}")
        if not self.args.dry_run:
            self.finishRow(row, 'failed', None, message)

    def finishRow(self, row, status, httpStatus, message):
        self.deliveries.update_one(
            {'_id': row['_id']},
            {'$inc': {'attempts': 1},
             '$set': {'status': status, 'httpStatus': httpStatus,
                      'message': message, 'updatedAt': utcNow()}})

    # ---- unreachable consumers ---------------------------------------

    def handleUnreachable(self, consumer):
        """Warn (throttled) or offer to start the consumer; True when it was spawned."""
        name = consumer.projectName
        self.wasUnreachable.add(name)
        now = time.monotonic()
        firstTime = name not in self.askedConsumers
        if firstTime and (self.args.auto_start or not self.args.no_prompt):
            self.askedConsumers.add(name)
            return self.offerToStart(consumer)
        lastWarned = self.lastUnreachableWarning.get(name)
        if lastWarned is not None and now - lastWarned < UNREACHABLE_WARNING_INTERVAL_SECONDS:
            return False
        self.lastUnreachableWarning[name] = now
        if firstTime:
            self.askedConsumers.add(name)
            printStartHint(consumer)
        log(f'consumer {name} at {consumer.baseUrl} is not reachable, will retry')
        return False

    def offerToStart(self, consumer):
        printStartHint(consumer)
        if self.args.auto_start:
            answer = 'y'
            print('--auto-start: starting it now in a new window')
        else:
            self.refreshLock()  # the prompt can block past the lock TTL
            try:
                answer = input('Start it now in a new window? [y/N] ')
            except EOFError:
                answer = ''
        if answer.strip().lower() not in ('y', 'yes'):
            self.lastUnreachableWarning[consumer.projectName] = time.monotonic()
            log(f'consumer {consumer.projectName} at {consumer.baseUrl} is not reachable, will retry')
            return False
        spawnServer(consumer)
        self.refreshLock()  # the wait below can block past the lock TTL
        deadline = time.monotonic() + SPAWN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if isReachable(consumer.baseUrl):
                self.wasUnreachable.discard(consumer.projectName)
                log(f'consumer {consumer.projectName} is up at {consumer.baseUrl}')
                return True
            time.sleep(0.5)
        log(f'consumer {consumer.projectName} started but not reachable on {consumer.baseUrl}; '
            "check that repo's AppCreator PORT (servicePortCheck.py)")
        return True

    # ---- loop --------------------------------------------------------

    def runOnce(self):
        if not self.args.dry_run:
            self.refreshLock()
            self.adoptSkippedRows()
        newRows = self.enqueue()
        self.deliver(newRows)

    def run(self):
        self.prepare()
        try:
            while True:
                self.runOnce()
                if self.args.once:
                    break
                time.sleep(self.args.interval)
        except KeyboardInterrupt:
            print()
            log('dispatcher stopped')
        finally:
            self.releaseLock()


# ======================================================================
# push
# ======================================================================

def collectionName(publisher, devData):
    return publisher + DEV_DATA_SUFFIX if devData else publisher


def describeDoc(collection, doc):
    return (f"{collection}  index={doc.get('index')}  {doc['consumeFunctionName']}  "
            f"_id={doc['_id']}  created={doc['dateOfCreation'].isoformat()}  "
            f"consumers={','.join(doc.get('projectNameConsumers', []))}")


def pickDoc(collection, docs):
    """One document: the single match, or a numbered menu when several match."""
    if len(docs) == 1:
        return docs[0]
    print(f'{len(docs)} messages in {collection} match; choose one:')
    for position, doc in enumerate(docs, start=1):
        print(f'  [{position}] {describeDoc(collection, doc)}')
    while True:
        try:
            answer = input(f'message number [1-{len(docs)}]: ')
        except EOFError:
            print('no selection; aborting', file=sys.stderr)
            raise SystemExit(2)
        if answer.strip().isdigit() and 1 <= int(answer) <= len(docs):
            return docs[int(answer) - 1]
        print('please enter a number from the list')


def sendDoc(collection, doc, consumer, url, args, projectId):
    """POST one mock doc --repeat times; returns (allSucceeded, lastStatus, lastBody)."""
    from utils import generateRandomString
    mock = mockFromDoc(doc)
    warning = mockValidityWarning(mock)
    if warning:
        print(f'WARNING: {warning}')
    messageId = args.message_id or generateRandomString()
    envelope = envelopeFromMock(mock, messageId, projectId)
    target = pushUrl(url, doc['consumeFunctionName'])
    print(f'message: {describeDoc(collection, doc)}')
    print(f'messageId: {messageId}')
    if args.dry_run:
        print(f'DRY RUN  POST {target}')
        print(json.dumps(envelope, indent=2))
        return True, None, None
    allSucceeded = True
    status, body = None, None
    for attempt in range(1, args.repeat + 1):
        try:
            status, body = postPushMessage(url, doc['consumeFunctionName'], envelope,
                                           timeout=args.timeout)
        except requests.ConnectionError:
            print(f'[attempt {attempt}/{args.repeat}] POST {target} -> connection refused')
            printStartHint(consumer, url)
            raise SystemExit(1)
        print(f'[attempt {attempt}/{args.repeat}] POST {target} -> {status}')
        print(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body))
        if not 200 <= status < 300:
            allSucceeded = False
    return allSucceeded, status, body


def listAddressedDocs(args, consumerName):
    """[(collection, doc)] addressed to consumerName, in index order per collection.

    The consumer's own collection is excluded (a project never consumes its
    own messages); every other generator collection is searched.
    """
    from mongoDb import pubSubMockDb
    if args.publisher:
        collections = [collectionName(args.publisher, args.dev_data)]
    else:
        collections = sorted(
            x for x in pubSubMockDb.getAllCollectionNames()
            if not x.endswith(DEV_DATA_SUFFIX) and x != consumerName)
    addressed = []
    for collection in collections:
        query = {'projectNameConsumers': consumerName}
        if args.function:
            query['consumeFunctionName'] = args.function
        for doc in readMockDocs(collection, query, MESSAGE_ORDER):
            addressed.append((collection, doc))
    return addressed


def runPush(args):
    from AppConfig import AppConfig
    consumers = resolveConsumers()
    projectId = AppConfig().projectId
    consumerName = args.consumer or AppConfig().projectName
    consumer = requireKnownConsumer(consumers, consumerName)
    if args.url:
        assertLoopbackUrl(args.url, '--url')
    url = (args.url or consumer.baseUrl).rstrip('/') + '/'

    if args.all:
        addressed = listAddressedDocs(args, consumerName)
        if not addressed:
            print(f'no mock messages addressed to {consumerName} found')
            raise SystemExit(1)
        print(f'{len(addressed)} messages addressed to {consumerName} -> {url}')
        results = []
        for collection, doc in addressed:
            print('-' * 78)
            succeeded, status, body = sendDoc(collection, doc, consumer, url, args, projectId)
            results.append((collection, doc, succeeded, status, body))
        print('=' * 78)
        print(f"{'collection':<30} {'idx':>4}  {'function':<40} result")
        for collection, doc, succeeded, status, body in results:
            result = 'DRY RUN' if args.dry_run else describeResponse(status, body)
            print(f"{collection:<30} {doc.get('index'):>4}  {doc['consumeFunctionName']:<40} {result}")
        raise SystemExit(0 if all(r[2] for r in results) else 1)

    collection = collectionName(args.publisher, args.dev_data)
    query = {}
    if args.function:
        query['consumeFunctionName'] = args.function
    if args.index is not None:
        query['index'] = args.index
    docs = readMockDocs(collection, query, MESSAGE_ORDER)
    if not docs:
        print(f'no message in {collection} matches {query}', file=sys.stderr)
        raise SystemExit(1)
    doc = pickDoc(collection, docs)
    succeeded, status, body = sendDoc(collection, doc, consumer, url, args, projectId)
    if not args.dry_run:
        print(f'result: {describeResponse(status, body)}  ({"ok" if succeeded else "FAILED"})')
    raise SystemExit(0 if succeeded else 1)


# ======================================================================
# parser + main
# ======================================================================

def addDispatchArguments(parser):
    parser.add_argument('--interval', type=float, default=1.0,
                        help='seconds between polls (default 1.0)')
    parser.add_argument('--since', default=None,
                        help='set every watermark to this ISO datetime (or "now") so captures '
                             'stored after it are enqueued too; a naive value is UTC. '
                             'Default: keep persisted watermarks, seed missing ones with now')
    parser.add_argument('--target', action='append', metavar='NAME=URL',
                        help='override a consumer base URL, loopback only (repeatable)')
    parser.add_argument('--repo', action='append', metavar='NAME=PATH',
                        help='override a consumer repo root used for the start command (repeatable)')
    parser.add_argument('--only-publisher', action='append', metavar='PREFIX',
                        help='only watch collections starting with this publisher prefix (repeatable)')
    parser.add_argument('--retry-failed', action='store_true',
                        help='flip failed deliveries back to pending at startup')
    parser.add_argument('--reset', action='store_true',
                        help=f'drop the {STATE_DATABASE_NAME} state database at startup')
    parser.add_argument('--once', action='store_true', help='one pass, then exit')
    parser.add_argument('--dry-run', action='store_true',
                        help='print envelopes; no POST, no state writes, no prompts')
    parser.add_argument('--no-prompt', action='store_true',
                        help='never ask to start an unreachable consumer')
    parser.add_argument('--auto-start', action='store_true',
                        help='start an unreachable consumer in a new window without asking')
    parser.add_argument('--timeout', type=float, default=HTTP_TIMEOUT_SECONDS,
                        help=f'HTTP timeout per delivery in seconds (default {HTTP_TIMEOUT_SECONDS})')


def addPushArguments(parser):
    parser.add_argument('--publisher', help='pubSubMockDb collection (publishing project name)')
    parser.add_argument('--function', help='consumeFunctionName filter')
    parser.add_argument('--index', type=int, help='message index filter')
    parser.add_argument('--dev-data', action='store_true',
                        help='read <publisher>_devData instead of the generator collection')
    parser.add_argument('--all', action='store_true',
                        help='push every message addressed to --consumer')
    parser.add_argument('--consumer', default=None,
                        help="receiving project name (default: this repo's project)")
    parser.add_argument('--url', default=None, help='consumer base URL override (loopback only)')
    parser.add_argument('--message-id', default=None, help='fixed messageId (not with --all)')
    parser.add_argument('--repeat', type=int, default=1,
                        help='send the same messageId this many times (default 1)')
    parser.add_argument('--dry-run', action='store_true', help='print envelopes, POST nothing')
    parser.add_argument('--timeout', type=float, default=HTTP_TIMEOUT_SECONDS,
                        help=f'HTTP timeout in seconds (default {HTTP_TIMEOUT_SECONDS})')


def validatePushArguments(parser, args):
    if not args.all and not args.publisher:
        parser.error('--publisher is required unless --all is given')
    if args.all and args.message_id:
        parser.error('--message-id cannot be combined with --all: every message would share '
                     'one id and all but the first would answer "Message already processed"')
    if args.repeat < 1:
        parser.error('--repeat must be at least 1')


def parseArgs(argv=None):
    parser = argparse.ArgumentParser(
        description='Local Pub/Sub dev tool: forward *_devData captures (dispatch) or replay a mock message (push).')
    subparsers = parser.add_subparsers(dest='command', required=True)
    dispatchParser = subparsers.add_parser(
        'dispatch', help='forward new *_devData mock messages to the local consumer dev servers')
    addDispatchArguments(dispatchParser)
    pushParser = subparsers.add_parser(
        'push', help='push a Pub/Sub mock message to a local consumer dev server')
    addPushArguments(pushParser)
    args = parser.parse_args(argv)
    if args.command == 'push':
        validatePushArguments(pushParser, args)
    return args


def main(argv=None):
    args = parseArgs(argv)
    requireLocalDev()
    if args.command == 'dispatch':
        Dispatcher(args).run()
    else:
        runPush(args)


if __name__ == '__main__':
    main()

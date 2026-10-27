"""The deploy gate: exits 1 while a prod-enabled publisher's topic is missing
from GCP, or while it cannot be looked up. job_predeploy.py runs main() in the
Cloud Run job <service>-predeploy, which cloudbuild.yaml runs before it
deploys the new image, so a failure stops the build and the old revision
keeps serving. `python checkPubSubTopics.py` runs it alone.

It reads the topics from the committed pubsub_models.json (AppCreator writes
it; test_pubSubModels.py keeps it fresh) instead of importing every publisher
module, which loaded the whole app once here and again in gunicorn. The build
runs no tests, so it also scans the shipped sources the way the Pub/Sub map
does (AST only, no import) and exits 1 when the publishers or their prod flag
differ from the manifest: a stale manifest would check too few topics. The
check stays out of the service's containers: in app.py it would run on
every cold start, and gunicorn binds the port before a worker imports the
app, so the startup probe would pass before it ran.
"""
import sys
from pathlib import Path

from AppConfig import AppConfig
from pubSub import checkPubSubTopics
from pubSubMapScan import readServerDir
from pubSubMapSection import publisherProdFlags
from pubSubModels import MANIFEST, parseManifest

SERVER_DIR = Path(__file__).resolve().parent


def manifestTopics(path: Path = SERVER_DIR / MANIFEST) -> list[tuple[str, bool]]:
    """(function name, publishes to prod) for every topic the manifest lists,
    the shape of pubSub._pubsubTopicRegistry. Raises ModelsError on a
    manifest that is not valid for this project, OSError on a missing one."""
    topics = parseManifest(Path(path).read_text(encoding='utf-8'),
                           AppConfig().projectName, f'server/{MANIFEST}')['topics']
    return [(topic['fn'], topic['prodPublish']) for topic in topics.values()]


def scannedTopics(server: Path = SERVER_DIR) -> list[tuple[str, bool]]:
    """(function name, publishes to prod) for every publisher the Pub/Sub
    map's AST scan finds in `server`. The image has no git, so the sources
    are walked, not listed by git."""
    return list(publisherProdFlags(readServerDir(server)).items())


def driftProblems(manifest: list, scanned: list) -> list[str]:
    """One line per publisher whose manifest entry differs from the scan."""
    listed, found = dict(manifest), dict(scanned)
    return [f'{fn}: {MANIFEST} has {_describe(listed.get(fn))}, the sources have {_describe(found.get(fn))}'
            for fn in sorted(set(listed) | set(found)) if listed.get(fn) != found.get(fn)]


def _describe(prodPublish) -> str:
    if prodPublish is None:
        return 'no such publisher'
    return f'publishToProdPubSub={prodPublish}'


def main():
    manifest = manifestTopics()
    problems = driftProblems(manifest, scannedTopics())
    if problems:
        print('\033[91m' + '=' * 60)
        print(f'ERROR: server/{MANIFEST} is stale, so the topic check would miss topics:')
        for problem in problems:
            print(f'  - {problem}')
        print('Run `cd server && ENVIRONMENT=localdev python AppCreator.py` and commit it.')
        print('=' * 60 + '\033[0m')
        sys.exit(1)
    checkPubSubTopics(manifest)


if __name__ == '__main__':
    main()

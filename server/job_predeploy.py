"""The checks a new image must pass before the service serves it: the Cloud
Run job <service>-predeploy runs this file. cloudbuild.yaml gives the job the
new image and the service's env vars, runs it (`gcloud run jobs execute
--wait`) and deploys the service only when it exits 0, so on any failure the
build stops and the old revision keeps serving. The service's containers run
neither check: a cold start goes straight to gunicorn.

In order, and exiting non-zero on the first failure:
  0. the image check: cloudbuild.yaml runs the job with EXPECTED_COMMIT_SHA,
     and the image carries the commit it was built from (BUILD_COMMIT_SHA,
     server/Dockerfile). They differ when an overlapping build re-imaged the
     job between this build's update and its run: the job then checked
     another build's image, so it fails. Without EXPECTED_COMMIT_SHA (a run
     by hand) there is nothing to compare;
  1. the Pub/Sub topic check (checkPubSubTopics.main): a pubsub_models.json
     that is stale against the shipped sources, a prod-enabled topic missing
     from GCP, or a lookup that could not be made;
  2. buildIndexes: this repo's index builds against its main database (the
     db singleton) and the repo's own checks of the result (the per-repo
     block below says which). An exception (e.g. a unique index the data
     breaks) fails it too.

Create the job once per service with tools/create_or_update_job.bat (pick
job_predeploy.py, no schedule), before the first push that carries this
file: the build fails while the job is missing. A console deploy, a
manual `gcloud run deploy` or a revision rollback skips these checks. Locally (ENVIRONMENT=localdev
or localprod) the topic lookups are skipped and the indexes are built in the
local database.

Shared file (fleet/sharedFiles.json): only the per-repo block differs.
"""
import os
import sys

import checkPubSubTopics


def commitMismatch() -> str:
    """Why this image is not the one the build asked to check, or ''."""
    expected = os.environ.get('EXPECTED_COMMIT_SHA', '')
    built = os.environ.get('BUILD_COMMIT_SHA', '')
    if not expected or expected == built:
        return ''
    return (f'the job runs the image of commit {built or "<none>"}, not {expected}: '
            'another build re-imaged it in between. Rerun this build.')


# >>> star-app per-repo: indexes
def buildIndexes() -> int:
    """Builds the indexes of the main database; 0 when they are in place."""
    from mongoDb import db
    db.ensureIndexes()
    return 0
# <<< star-app per-repo: indexes


def main() -> int:
    mismatch = commitMismatch()
    if mismatch:
        print('ERROR: ' + mismatch)
        return 1
    checkPubSubTopics.main()
    print('Pub/Sub topic check passed.')
    result = buildIndexes()
    print('Index build ' + ('passed.' if result == 0 else 'FAILED.'))
    return result


if __name__ == '__main__':
    sys.exit(main())

"""Give every Orders document placed before order fulfillment its
completedAt, refundedAt and refundReference (null): the order rows declare
the three as required, so an order without them fails the response check
in localdev, and goes out without them everywhere else.

Run it once per database, after the deploy that adds the fields (new
orders store all three). Idempotent: it only touches an order missing one
of them and keeps a value already there (refundedAt, say), so a second
run backfills 0. It logs only how many it backfilled, never an order or a
value. Not part of job_predeploy.py.

To run it in the cloud, create the job once (it writes to GCP) with
tools/createOrUpdateJob.py: --job-file=job_backfillOrderFulfillmentFields.py,
plus the service's --project, --region and --service (star-app.json's cloud
section), and no --schedule. Locally (ENVIRONMENT=localdev) it backfills
the local database.
"""
import sys

# The orders one read takes; the job reads batches until none is left.
BACKFILL_BATCH = 500
FIELDS = ('completedAt', 'refundedAt', 'refundReference')
MISSING = {'$or': [{field: {'$exists': False}} for field in FIELDS]}
# A pipeline update: a field already there (an old order cancelled and
# refunded after the deploy has refundedAt) keeps its value, a missing one
# becomes null.
BACKFILL = [{'$set': {
    **{field: {'$ifNull': [f'${field}', None]} for field in FIELDS},
    '_version': {'$add': ['$_version', 1]},
}}]


def main() -> int:
    """Backfills every order without the fulfillment fields; returns how
    many."""
    from actions.OrderActions import ORDERS
    from mongoDb import db
    total = 0
    while True:
        orders = db.read(MISSING, ORDERS, projection={'_id': 1},
                         limit=BACKFILL_BATCH)
        if not orders:
            break
        # Each write filtered on a missing field again, so an order
        # backfilled meanwhile is not written twice.
        result = db.bulkUpdate([{
            'filter': {'_id': order['_id'], **MISSING},
            'update': BACKFILL,
        } for order in orders], ORDERS)
        total += result.modified_count
        if len(orders) < BACKFILL_BATCH:
            break
    print(f'Backfilled {total} orders.')
    return total


if __name__ == '__main__':
    main()
    # A count is no failure: any exception above exits non-zero.
    sys.exit(0)

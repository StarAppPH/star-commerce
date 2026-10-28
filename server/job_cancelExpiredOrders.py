"""Cancel the orders nobody paid in time, at every store: the Cloud Run job
<service>-cancelExpiredOrders runs this file every 5 minutes.

An order holds its units for ORDER_PAYMENT_MINUTES. The order routes cancel
a store's expired orders when they meet them (payOrder, the order lists,
createOrder), so this job is what frees the units at a store nobody is
visiting. It logs only how many it cancelled, never an order or a value.

Create the job and its schedule once (it writes to GCP), with
tools/createOrUpdateJob.py: --job-file=job_cancelExpiredOrders.py
--schedule="*/5 * * * *" --time-zone=Asia/Manila, plus the service's
--project, --region and --service (star-app.json's cloud section).
Locally (ENVIRONMENT=localdev) it sweeps the local database.
"""
import sys


def main() -> int:
    """Cancels every expired pending order; returns how many."""
    from actions.OrderActions import cancelExpiredOrders
    cancelled = cancelExpiredOrders()
    print(f'Cancelled {cancelled} expired orders.')
    return cancelled


if __name__ == '__main__':
    main()
    # A count is no failure: any exception above exits non-zero.
    sys.exit(0)

"""payments.py: the payment provider, a demo one for now.

Only an order paid through a provider (a PaymentOption with a provider:
cards, crypto, an e-wallet paid online) reaches these functions. One paid
by hand to the store (cash, a check, a bank transfer, the store's own
e-wallet account) is confirmed by the store's admin (confirmOrderPayment)
and never charged or refunded here. A real provider confirms a payment
through its webhook to us, not in payOrder's call.

These functions are the one place a real gateway (PayMongo, Stripe)
plugs in. They are pure logic (no db, no route): models and actions never
import a gateway directly.

payOrder claims the order first (pending to paid, with the reference
newPaymentReference made) and only then charges, so an order that expired
or was paid by a racing call is never charged. A real gateway charges
after that claim the same way, and voids its charge when the charge fails
part way: payOrder then puts the order back to pending. A charge that
fails after the order's payment window puts it back to pending too, and
the next expiry sweep cancels it.

A real gateway also needs a claim state between pending and paid (say
'paying', resolved by a timeout or the gateway's webhook): today a process
that dies between the claim and the charge leaves the order paid with no
charge made. An admin who completes or cancels the order between the claim
and the charge is another reason: payOrder's revert of a failed charge
matches a paid order only, so that order stays completed or cancelled with
no charge made.

cancelStoreOrder refunds a paid order only after it turned it cancelled,
so an order is never paid and refunded at once. A refund that fails
leaves it cancelled with no refundedAt: a real gateway needs a retry of
those (out of scope for the demo).

receiveReturn refunds a return the same way (FulfillmentActions.
recordedRefund): only after it turned it received, against the order's
payment, whatever the store's refundMethod says. A failed refund leaves it
received with no refundedAt.
"""

import secrets

from objects import PAYMENT_REFERENCE_PREFIX, REFUND_REFERENCE_PREFIX


def newPaymentReference() -> str:
    """The reference of a payment about to be made, charging nothing: what
    payOrder stores when it claims the order."""
    return PAYMENT_REFERENCE_PREFIX + secrets.token_hex(8)


def chargeDemoPayment(orderId: str, reference: str, amount: float,
                      currency: str, method: str) -> None:
    """Charge `amount` (in `currency`) for the order, paid by `method`,
    under `reference`; raises when the payment does not go through. The
    demo approves every method and charges nothing.

    A real gateway would send `orderId` as its idempotency key, so a
    retried payment of one order never charges twice."""


def newRefundReference() -> str:
    """The reference of a refund about to be made, refunding nothing: what
    cancelStoreOrder and receiveReturn store once the refund went
    through."""
    return REFUND_REFERENCE_PREFIX + secrets.token_hex(8)


def refundDemoPayment(orderId: str, paymentReference: str,
                      refundReference: str, amount: float,
                      currency: str) -> None:
    """Refund `amount` (in `currency`) of the order's payment; raises when
    the refund does not go through. The demo refunds every payment and
    moves nothing.

    A real gateway refunds against `paymentReference` (the charge) and
    sends `refundReference` as its idempotency key, so a retried refund of
    one order never refunds twice."""

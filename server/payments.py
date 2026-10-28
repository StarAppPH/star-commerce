"""payments.py: the payment provider, a demo one for now.

These two functions are the one place a real gateway (PayMongo, Stripe)
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
charge made.
"""

import secrets

from objects import PAYMENT_REFERENCE_PREFIX


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

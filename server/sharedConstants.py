"""sharedConstants.py — values BOTH the server and the browser need.

EVERY public value in this file is copied to the client by AppCreator into
client/lib/generated/serverConstants.ts, and therefore ships to every browser.
There is no opt-in list to maintain: define it here and it is shared.

  * To keep a value server-only, prefix it with an underscore (_LIKE_THIS),
    or put it in another module. Underscore names are never exported.
  * Values must be JSON-able: str, int, float, bool, None, list, tuple, dict.
    Anything else (datetime, Decimal, set, regex, pydantic model) fails
    generation with a message naming the constant.
  * Import NOTHING from the app. objects.py imports this module — never the
    reverse — and that one-way direction is what keeps it cycle-free.
    A test asserts this file has zero imports.
  * Never put credentials, API keys, bank details or internal limits here.
    This file is public by construction. AppCreator refuses to generate when a
    name or a value looks like a secret, but that is a tripwire, not a proof of
    absence — the judgement is yours.

Client code reads these through the generated module:

    import { VAT_RATE } from '@/lib/generated/serverConstants';

Prefer re-exporting from the hand-written lib module that owns the related
logic, so existing import sites keep working:

    // client/lib/taxUtils.ts
    import { VAT_RATE } from '@/lib/generated/serverConstants';
    export { VAT_RATE } from '@/lib/generated/serverConstants';

Regenerate with:  cd server && python AppCreator.py --constants-only
(or a full `python AppCreator.py`, which runs this step first)
"""

# ---------------------------------------------------------------------------
# Example — replace with your project's own shared values, and delete this one
# if your app has no tax rate. An empty file is valid: the generated module is
# then just its banner.
# ---------------------------------------------------------------------------

VAT_RATE = 0.12

# The Firebase project users sign in with (Firebase Console > Project
# settings > Project ID). client/lib/firebase.ts signs in to it, and the
# server only accepts ID tokens issued for it (AuthHandler.py; the
# FIREBASE_PROJECT_ID environment variable overrides it per environment).
# A new project starts with a "demo-" id: Firebase runs it in the local
# Emulator Suite only (create-star-app dev), with no account, and
# cloud-setup replaces it with the real project's id. A project id is
# public: it is in every page that uses Firebase.
FIREBASE_PROJECT_ID = 'star-commerce-6797'

# The end of store reset (resetMyStore, the admin's "Reset my store"): from
# this instant on (UTC+8) the route refuses and the client hides the button,
# in every environment. The server is what enforces it.
STORE_RESET_ENDS_AT = '2026-11-05T00:00:00+08:00'

# How near a delivery zone's side, in degrees (about 0.1 mm), a point counts
# as on it (objects/store.py, client deliveryZones.ts): the middle of a side
# two zones share is rarely exactly in line with it in floating point, and
# must not count as inside the other zone.
DELIVERY_ZONE_BORDER_TOLERANCE = 1e-9

# The most items getWaitingOnYou counts in each of its counts (payments to
# confirm, orders to hand over, returns to decide): the admin's new-chat
# screen shows a count that reaches it as "50+".
WAITING_COUNT_CAP = 50

# The order statuses that count as a sale in storeReport (its sales kinds:
# salesByProduct, salesByDay, salesByFulfillment), from the order lifecycle
# (objects/order.py OrderStatus):
#   paid: the money is in (payOrder through a provider, or
#     confirmOrderPayment for one paid by hand);
#   completed: paid and handed over (completeOrder moves only a paid order).
# Left out:
#   pending and awaitingPayment: nothing is paid yet (an awaitingPayment
#     order is only the customer's promise to pay by hand);
#   cancelled: whatever happened before, no sale stands. A paid order
#     cancelled later (by the store, or by the customer as the policy
#     allows) was refunded through its provider, or is given back by hand by
#     the store, so it is out of sales, and it is NOT in storeReport's
#     `refunds` either (that total is the received Returns only), so it is
#     never subtracted twice.
# A completed order with a return keeps its gross revenue here: the return's
# refund is counted in `refunds`, in the range its refundedAt falls in.
SALES_STATUSES = ('paid', 'completed')

# The longest date range (in whole days, both ends included) the admin's
# filtered lists (getStoreOrders, getStoreReturns) and storeReport take: a
# year, a leap year's included.
REPORT_RANGE_DAYS_MAX = 366

# Product photos (objects/photo.py, PhotoActions), which the admin's photo
# forms and cards check before sending:
#   PHOTOS_PER_CALL: the photos every photo call makes, all from its one
#     prompt;
#   PHOTO_PROMPT_MAX_LENGTH: the longest prompt the admin may write (or edit
#     from the default one) on a photo form or the photo offer card;
#   PHOTO_NOTES_MAX_LENGTH: the longest direction typed in chat, added to the
#     prompt as its special request;
#   PHOTO_REFERENCES_MAX: the most photos of one item a photo call takes;
#   PHOTO_UPLOAD_MAX_BYTES: the largest photo or file the admin or a
#     customer may upload (storage.rules allows no larger one).
PHOTOS_PER_CALL = 3
PHOTO_PROMPT_MAX_LENGTH = 2000
PHOTO_NOTES_MAX_LENGTH = 300
PHOTO_REFERENCES_MAX = 10
PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

# The store policy's questions (objects/policy.py unansweredPolicyQuestions,
# the admin's policy card and new-chat screen):
#   POLICY_OPTIONAL_QUESTIONS: asked once at most and never required, so
#     never counted as not answered (the store opens without them);
#   POLICY_RETURN_QUESTIONS: apply only while the store accepts returns
#     (acceptsReturns true); acceptsReturns false clears them;
#   POLICY_ZONES_QUESTION: the one item a store that delivers but has no
#     delivery zone yet counts as, beside the policy's own fields.
POLICY_OPTIONAL_QUESTIONS = ('deliveryMethod', 'deliveryAreas',
                             'additionalTerms')
POLICY_RETURN_QUESTIONS = ('returnWindowDays', 'returnConditions',
                           'nonReturnableItems', 'refundMethod',
                           'returnShippingPaidBy')
POLICY_ZONES_QUESTION = 'deliveryZones'

# Everything above this line ships to every browser. No credentials, no API
# keys, no bank details, no internal limits.

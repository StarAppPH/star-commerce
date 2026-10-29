"""Returns (a customer sending back items of a completed order).

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field
from typing import Literal

from AppConfig import Timezone
from objects.base import (MongoModel, AppModel, StoredDocumentKeys,
                          ResponseModel)
from objects.order import ORDER_ITEMS_MAX, OrderItem, OrderItemRow
from objects.policy import Policy
from objects.store import PHONE_NUMBER_PATTERN, StoreCurrency
from utils import appDayStart


# ──────────────────────────────────────────────
# Returns (items of a completed order sent back)
# ──────────────────────────────────────────────

# Where a return is:
#   requested: the customer asked for it (they may still cancel it);
#   approved: the store's admin agreed to take the items back;
#   rejected: the admin refused it (rejectReason is why);
#   received: the store got the items back: it was refunded then, and its
#     units went back in stock when restocked;
#   cancelled: the customer took the request back while it was requested.
# A rejected or cancelled return frees its quantities for a later request.
ReturnStatus = Literal['requested', 'approved', 'rejected', 'received',
                       'cancelled']
# Why an item goes back: the customer changed their mind (the store's
# return window), or it is defective or the wrong item (its defect window).
ReturnReason = Literal['changedMind', 'defective', 'wrongItem']
# The returns whose quantities count against what is left to return.
RETURN_HOLDING_STATUSES = ('requested', 'approved', 'received')
RETURN_NOTE_MAX_LENGTH = 300
RETURN_REJECT_REASON_MAX_LENGTH = 300
# The most return requests one order takes, cancelled and rejected ones
# included: it bounds request and cancel spam.
RETURN_REQUESTS_PER_ORDER_MAX = 10


class NewReturnItem(AppModel):
    """One item of a requestReturn call. Types only, no limits: the route
    checks them, so a refusal names the item's position and never quotes
    the value (NewOrderItem's reason)."""
    item: int = Field(..., description="The item's position in the order "
                      '(1 is its first item)')
    quantity: int = Field(..., description='Units to return')


class ReturnItem(OrderItem):
    """One item of a return: a snapshot of the order's item (its productId,
    name and unit price paid), with the units returned as its quantity."""
    quantity: int = Field(..., ge=1, description='Units returned')


class Return(MongoModel):
    """A Returns document, read newest first: the (storeId, status,
    createdAt) and (customerId, storeId, createdAt) indexes
    (mongoDb.ensureIndexes) serve the admin's and the customer's lists,
    (orderId, status) the sum of an order's units already returned.

    Receiving a restocked return $incs each product's inventory and its
    _version (the Order docstring's rule)."""
    storeId: str = Field(..., min_length=1, description='The Stores _id')
    customerId: str = Field(..., min_length=1,
                            description='The Customers _id')
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    items: list[ReturnItem] = Field(..., min_length=1,
                                    max_length=ORDER_ITEMS_MAX,
                                    description='What goes back')
    reason: ReturnReason = Field(..., description='Why it goes back')
    note: str | None = Field(None, max_length=RETURN_NOTE_MAX_LENGTH,
                             description="The customer's note")
    refundAmount: float = Field(..., ge=0,
                                description='What the return refunds')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the order's currency")
    # A copy of the order's: the store arranges the handback with it.
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    status: ReturnStatus = Field(..., description='Where the return is')
    createdAt: datetime.datetime = Field(...,
                                         description='When it was requested')
    decidedAt: datetime.datetime | None = Field(
        None, description='When the store approved or rejected it')
    rejectReason: str | None = Field(
        None, max_length=RETURN_REJECT_REASON_MAX_LENGTH,
        description='Why the store rejected it')
    cancelledAt: datetime.datetime | None = Field(
        None, description='When the customer cancelled it')
    receivedAt: datetime.datetime | None = Field(
        None, description='When the store got the items back')
    restocked: bool | None = Field(
        None, description='Whether the units went back in stock')
    refundedAt: datetime.datetime | None = Field(
        None, description='When it was refunded')
    refundReference: str | None = Field(
        None, description="The payment provider's id of the refund")


class ReturnView(StoredDocumentKeys, Return):
    """A Returns document as stored."""


class ReturnItemRow(OrderItemRow):
    """One item of a return as a route sends it: ReturnItem without its
    productId."""
    quantity: int = Field(..., ge=1, description='Units returned')


class MyReturnRow(ResponseModel):
    """One return as the customer who asked for it sees it: no productIds,
    no refund reference, no contact number. Each field restates Return's,
    which test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='Returns _id')
    orderId: str = Field(..., min_length=1, description='The Orders _id')
    status: ReturnStatus = Field(..., description='Where the return is')
    items: list[ReturnItemRow] = Field(..., min_length=1,
                                       max_length=ORDER_ITEMS_MAX,
                                       description='What goes back')
    reason: ReturnReason = Field(..., description='Why it goes back')
    note: str | None = Field(..., max_length=RETURN_NOTE_MAX_LENGTH,
                             description="The customer's note")
    refundAmount: float = Field(..., ge=0,
                                description='What the return refunds')
    currency: StoreCurrency = Field(
        ..., description="ISO 4217 code of the order's currency")
    createdAt: datetime.datetime = Field(...,
                                         description='When it was requested')
    decidedAt: datetime.datetime | None = Field(
        ..., description='When the store approved or rejected it')
    rejectReason: str | None = Field(
        ..., max_length=RETURN_REJECT_REASON_MAX_LENGTH,
        description='Why the store rejected it')
    cancelledAt: datetime.datetime | None = Field(
        ..., description='When the customer cancelled it')
    receivedAt: datetime.datetime | None = Field(
        ..., description='When the store got the items back')
    refundedAt: datetime.datetime | None = Field(
        ..., description='When it was refunded')


class StoreReturnRow(MyReturnRow):
    """One return as its store's admin sees it: MyReturnRow with the
    customer's contact number (to arrange the handback), whether it was
    restocked and the refund's reference."""
    contactNumber: str = Field(..., pattern=PHONE_NUMBER_PATTERN,
                               description="The customer's verified number")
    restocked: bool | None = Field(
        ..., description='Whether the units went back in stock')
    refundReference: str | None = Field(
        ..., description="The payment provider's id of the refund")


class MyReturnListResponse(ResponseModel):
    returns: list[MyReturnRow] = Field(
        ..., description='The returns, newest first')


class StoreReturnListResponse(ResponseModel):
    returns: list[StoreReturnRow] = Field(
        ..., description='The returns, newest first')


# ──────────────────────────────────────────────
# The return windows of a store's policy
# ──────────────────────────────────────────────

def returnDeadline(handedOverAt: datetime.datetime,
                   days: int) -> datetime.datetime:
    """The end of a window of `days` after a handover at `handedOverAt`:
    the first instant after the `days`-th app day (UTC+8) that follows the
    handover's app day (exclusive), as usageCounters.usageExpiry counts app
    days. Handed over on the 1st with 7 days, the window ends when the 8th
    does."""
    handoverDay = handedOverAt.astimezone(Timezone().getTimezone()).date()
    return (appDayStart(handoverDay.isoformat()) +
            datetime.timedelta(days=days + 1)).astimezone(
                datetime.timezone.utc)


def returnRefusal(policy: Policy | None, reason: ReturnReason,
                  completedAt: datetime.datetime,
                  now: datetime.datetime) -> str | None:
    """Why the store's `policy` refuses a return for `reason` of an order
    handed over at `completedAt`, asked at `now`; None when it takes it.
    No policy (None) is a store that has not said anything yet.

    changedMind needs acceptsReturns and the returnWindowDays window.
    defective and wrongItem are always taken within defectReportDays,
    falling back to returnWindowDays when returns are accepted. What the
    store has not said is refused with "ask the store". nonReturnableItems
    is free text no code can check: the store's admin rejects those."""
    acceptsReturns = None if policy is None else policy.acceptsReturns
    returnWindowDays = None if policy is None else policy.returnWindowDays
    if reason == 'changedMind':
        if acceptsReturns is None:
            return ("This store hasn't said whether it takes returns: ask "
                    'the store')
        if acceptsReturns is False:
            return ("This store doesn't take returns of items that aren't "
                    'defective')
        if returnWindowDays is None:
            return ("This store hasn't said how long you have to return an "
                    'item: ask the store')
        if now >= returnDeadline(completedAt, returnWindowDays):
            return (f'The return window of {returnWindowDays} days after '
                    'handover has passed')
        return None
    days = None if policy is None else policy.defectReportDays
    if days is None and acceptsReturns:
        days = returnWindowDays
    if days is None:
        return ("This store hasn't said how long you have to report a "
                'defective or wrong item: ask the store')
    if now >= returnDeadline(completedAt, days):
        return f'You had {days} days after handover to report this'
    return None

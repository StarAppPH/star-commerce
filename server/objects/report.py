"""The admin's store reports (ReportActions.storeReport): totals and
groupings of the store's orders over a date range, each kind one fixed
server-written pipeline.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import typing

from typing import ClassVar
from pydantic import Field
from typing import Literal

from objects.base import ResponseModel
from objects.store import StoreCurrency

# What a report groups the store's orders by:
#   salesByProduct: units and revenue per product (its items' value, no
#     delivery fees), top revenue first;
#   salesByDay: orders and revenue per UTC+8 day, oldest day first;
#   ordersByStatus: how many orders of each status, any status;
#   salesByFulfillment: pickup against delivery orders, with their revenue
#     and delivery fees.
# The sales kinds count only SALES_STATUSES (sharedConstants.py).
StoreReportKind = Literal['salesByProduct', 'salesByDay', 'ordersByStatus',
                          'salesByFulfillment']
STORE_REPORT_KINDS = typing.get_args(StoreReportKind)
# How many rows a report sends by default, and at most.
STORE_REPORT_DEFAULT_LIMIT = 10
STORE_REPORT_MAX_LIMIT = 50


class StoreReportRow(ResponseModel):
    """One row of a store report (a product, a day, a status, a way to get
    the order), or the report's total. One model for every kind: a field a
    kind does not count is null."""
    AI_READS: ClassVar[dict[str, str]] = {
        'label': 'what the row counts: a product, a day, a status, pickup '
                 'or delivery',
        'orders': 'how many orders the row counts',
        'units': 'how many units those orders hold',
        'revenue': 'what those orders sold for, the answer to "how much"',
        'deliveryFees': 'the part of the revenue that is delivery fees',
    }
    label: str = Field(..., description='What the row counts: the product '
                       'name, the UTC+8 day (YYYY-MM-DD), the status, pickup '
                       'or delivery, or "total"')
    orders: int = Field(..., ge=0, description='Orders counted')
    units: int | None = Field(..., ge=0,
                              description='Units in those orders; null when '
                              'the kind does not count them')
    revenue: float | None = Field(
        ..., ge=0, description='What they sold for, in the currency; null '
        'when the kind does not count it')
    deliveryFees: float | None = Field(
        ..., ge=0, description='The delivery fees in that revenue; null when '
        'the kind does not count them')


class StoreReportResponse(ResponseModel):
    """What storeReport sends: the kind's rows over the whole UTC+8 days
    from startDate to endDate, the total of the whole range (never cut by
    the limit), and the store's refunds in that range."""
    AI_READS: ClassVar[dict[str, str]] = {
        'kind': 'which report this is',
        'startDate': 'the first day counted, so the AI names the range',
        'endDate': 'the last day counted',
        'currency': 'the amounts are in it',
        'rows': 'the report the AI answers from',
        'total': 'the whole range, when the rows are cut by the limit',
        'truncated': 'more rows than the limit: say the list is cut',
        'refunds': 'what returns gave back, kept apart from revenue',
    }
    kind: StoreReportKind = Field(..., description='The report')
    startDate: str = Field(..., description='The first day counted, '
                           'YYYY-MM-DD in UTC+8')
    endDate: str = Field(..., description='The last day counted, YYYY-MM-DD '
                         'in UTC+8')
    currency: StoreCurrency = Field(
        ..., description='ISO 4217 code of every amount')
    rows: list[StoreReportRow] = Field(
        ..., max_length=STORE_REPORT_MAX_LIMIT,
        description="The kind's rows, in its order")
    total: StoreReportRow = Field(
        ..., description='Every order the report counts, labelled "total"')
    truncated: bool = Field(
        ..., description='Whether there were more rows than the limit')
    refunds: float | None = Field(
        ..., ge=0, description='What the returns refunded through the app in '
        'the range (by refundedAt); null for ordersByStatus')

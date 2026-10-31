"""What the admin's chat has told the model the store's data lacks
(toolWarnings.py): a product with no photo, an order paid a week ago and
not handed over, a policy answer not given.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
import datetime

from pydantic import Field
from typing import Literal

from objects.base import MongoModel

# Every warning a read tool's result can carry (toolWarnings.WARNINGS).
ToolWarningCode = Literal['noPhoto', 'outOfStock', 'lowStock', 'hidden',
                          'staleUnfulfilled', 'staleAwaitingPayment',
                          'refundMissing', 'unanswered', 'staleApproved',
                          'noPaymentMethods', 'noDeliveryZones',
                          'policyUnset', 'storeDetailMissing']


class ToolWarningShown(MongoModel):
    """A ToolWarningsShown document: one item of one warning, last told to
    the model at lastShownAt, so it is not told again before expireAt (the
    warning's cooldown). Its _id is "<storeId>:<code>:<itemId>"; a TTL
    index on expireAt deletes it once the cooldown is over."""
    storeId: str = Field(..., description='The Stores _id')
    code: ToolWarningCode = Field(..., description='The warning')
    itemId: str = Field(..., min_length=1,
                        description='What it is about: a Products, Orders '
                        'or Returns _id, or a policy or store field name')
    lastShownAt: datetime.datetime = Field(
        ..., description='When the model was last told')
    expireAt: datetime.datetime = Field(
        ..., description='Until then it is not told again')

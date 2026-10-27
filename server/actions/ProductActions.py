"""ProductActions: a store's products."""

import re

from actions.CustomerActions import requireCustomer
from mcpTools import mcp_tool
from mongoDb import db
from objects import ProductListResponse, ProductRow
from route_config import route_config

PRODUCTS = 'Products'
STORES = 'Stores'
PRODUCT_LIST_DEFAULT_LIMIT = 10
PRODUCT_LIST_MAX_LIMIT = 50
PRODUCT_SEARCH_MAX_LENGTH = 80


def storeCurrency(storeId: str) -> str:
    """The currency of the store; refuses an unknown store."""
    store = db.read({'_id': storeId}, STORES, projection={'currency': 1},
                    findOne=True)
    if store is None:
        raise ValueError('Store not found')
    return store['currency']


def _validLimit(limit: int) -> int:
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or not 1 <= limit <= PRODUCT_LIST_MAX_LIMIT):
        raise ValueError(
            f'limit is a whole number from 1 to {PRODUCT_LIST_MAX_LIMIT}')
    return limit


def _nameSearch(search: str | None) -> dict:
    """The name filter of a search: case-insensitive, the text matched
    literally (a regex character in it is no operator)."""
    if search is None or not search.strip():
        return {}
    if len(search) > PRODUCT_SEARCH_MAX_LENGTH:
        raise ValueError(
            f'A search is at most {PRODUCT_SEARCH_MAX_LENGTH} characters')
    return {'name': {'$regex': re.escape(search.strip()), '$options': 'i'}}


class ProductActions():

    @mcp_tool('customer')
    @route_config(httpMethod='POST',
                  jwtRequired=True,
                  successMessage='Products fetched',
                  responseModel=ProductListResponse)
    def listStoreProducts(self,
                          userId: str,
                          storeId: str,
                          search: str | None = None,
                          limit: int = PRODUCT_LIST_DEFAULT_LIMIT) -> dict:
        """List the products this store sells, best sellers first. Pass
        `search` to find products whose name contains that text, and
        `limit` (1 to 50, default 10) for how many to return."""
        requireCustomer(userId)
        # The generated HTTP handler passes a missing body key as None, so
        # the default above applies to direct and MCP calls only.
        if limit is None:
            limit = PRODUCT_LIST_DEFAULT_LIMIT
        currency = storeCurrency(storeId)
        pipeline = [
            {'$match': {'storeId': storeId, 'isAvailable': True,
                        **_nameSearch(search)}},
            {'$sort': {'soldCount': -1, 'name': 1}},
            {'$limit': _validLimit(limit)},
        ]
        return {
            'currency': currency,
            'products': db.aggregateResponses(ProductRow, pipeline, PRODUCTS),
        }

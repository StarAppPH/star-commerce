"""Sample todo models: no route uses them any more. The framework's model
tests use them as fixtures until the product models replace them.

Package rule (every objects module): no validator may EVER mutate a value it
receives (see objects/__init__.py)."""
from pydantic import Field

from objects.base import MongoModel, StoredDocumentKeys, ResponseModel


# ──────────────────────────────────────────────
# Sample todo models: no route uses them any more. The framework's model
# tests use them as fixtures until the product models replace them.
# ──────────────────────────────────────────────


class SampleTodo(MongoModel):
    """A SampleTodos document, as createTodo builds it."""
    title: str = Field(..., min_length=1, description='What to do')
    completed: bool = Field(False, description='Whether it is done')


class SampleTodoView(StoredDocumentKeys, SampleTodo):
    """A SampleTodos document as stored."""


class SampleTodoRow(ResponseModel):
    """One row of getTodos: the fields its projection reads
    (projection_of(SampleTodoRow)). Each field restates SampleTodo's, which
    test_route_response_models.py's MIRRORS keeps in step."""
    id: str = Field(..., alias='_id', description='SampleTodos _id')
    title: str = Field(..., min_length=1, description='What to do')
    # Required: every stored todo has it, so the row always carries it.
    completed: bool = Field(..., description='Whether it is done')


class SampleTodoListResponse(ResponseModel):
    todos: list[SampleTodoRow] = Field(..., description='Every todo')

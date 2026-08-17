"""后端测试的共享匿名访客依赖。"""

from collections.abc import Iterator

import pytest

from app.main import app, get_visitor_id
from tests.sqlite_repository import TEST_VISITOR_ID


@pytest.fixture(autouse=True)
def isolated_test_visitor() -> Iterator[None]:
    """普通单元测试不依赖真实 PostgreSQL 身份表。"""

    app.dependency_overrides[get_visitor_id] = lambda: TEST_VISITOR_ID
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_visitor_id, None)

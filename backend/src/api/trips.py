"""最终行程在 LangGraph Store 中的只读查询与删除服务。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from langgraph.store.base import BaseStore
from pydantic import BaseModel, ConfigDict

from domain.models import TripRecord


class TripNotFoundError(LookupError):
    """当前用户命名空间内不存在指定行程。"""


class TripSummary(BaseModel):
    """历史抽屉使用的稳定行程摘要。"""

    model_config = ConfigDict(extra="forbid")

    trip_id: UUID
    destination: str
    start_date: date
    end_date: date
    summary: str
    created_at: datetime
    warnings: list[str]


class TripStoreService:
    """只通过固定用户命名空间访问最终行程，不接触 Thread 检查点。"""

    def __init__(self, store: BaseStore) -> None:
        self._store = store

    async def list(self, user_id: str) -> list[TripSummary]:
        """按创建时间倒序返回当前用户的行程摘要。"""

        items = await self._store.asearch(self._namespace(user_id), limit=100)
        records = [TripRecord.model_validate(item.value) for item in items]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return [self._summary(record) for record in records]

    async def get(self, user_id: str, trip_id: UUID) -> TripRecord:
        """读取完整行程；跨用户查询与不存在统一表现为未找到。"""

        item = await self._store.aget(self._namespace(user_id), str(trip_id))
        if item is None:
            raise TripNotFoundError(str(trip_id))
        return TripRecord.model_validate(item.value)

    async def delete(self, user_id: str, trip_id: UUID) -> None:
        """仅在确认当前用户拥有行程后删除，避免泄露资源是否属于他人。"""

        await self.get(user_id, trip_id)
        await self._store.adelete(self._namespace(user_id), str(trip_id))

    @staticmethod
    def _namespace(user_id: str) -> tuple[str, str]:
        return user_id, "trips"

    @staticmethod
    def _summary(record: TripRecord) -> TripSummary:
        if record.requirements.start_date is None or record.requirements.end_date is None:
            # 最终保存节点应先完成结构校验；这里再次守住历史接口的数据契约。
            raise ValueError("已保存行程缺少开始或结束日期")
        return TripSummary(
            trip_id=record.trip_id,
            destination=record.city.name,
            start_date=record.requirements.start_date,
            end_date=record.requirements.end_date,
            summary=record.draft.summary,
            created_at=record.created_at,
            warnings=record.warnings,
        )

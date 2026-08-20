"""最终行程保存节点。

这个文件只负责一个副作用：把已经通过 ``final_validate`` 的状态写入 LangGraph Store。
把保存从规划节点中单独拿出来，是为了让初学者一眼看出：

* Planner 只生成草稿；
* Validator 只检查，不写数据；
* Persistence 才拥有写 Store 的权限。

保存使用 ``user_id + thread_id`` 派生稳定 ``trip_id``，因此同一个 Thread 重试或从
Checkpoint 恢复时不会重复创建行程。
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.config import get_config
from langgraph.runtime import Runtime

from domain.errors import TravelGraphError
from domain.models import (
    BudgetBreakdown,
    PlaceSnapshot,
    TravelFacts,
    TravelSelection,
    TripRecord,
)
from travel_graph.state import TravelContext, TravelState
from travel_graph.utils import thread_id_from, user_id_from


class PersistenceNodes:
    """将最终状态转换为 Store 记录并幂等保存。"""

    async def save(
        self,
        state: TravelState,
        runtime: Runtime[TravelContext],
    ) -> dict[str, object]:
        """保存已校验行程；Store 中已有相同 key 时直接返回完成状态。

        输入：已通过 ``final_validate`` 的完整 TravelState。
        处理：从认证上下文取得用户、从 Checkpoint 配置取得 Thread，构造稳定主键并写 Store。
        输出：``trip_id`` 与 ``completed`` 阶段。
        下一跳：图边把节点连接到 ``END``。
        """

        store = runtime.store
        if store is None:
            raise TravelGraphError("store_unavailable", "保存行程需要 LangGraph Store")
        config = get_config()
        user_id = user_id_from(config, runtime)
        thread_id = thread_id_from(config)
        trip_id = uuid5(NAMESPACE_URL, f"openzltravel:{user_id}:{thread_id}")
        namespace = (user_id, "trips")
        existing = await store.aget(namespace, str(trip_id))
        if existing is not None:
            return {"trip_id": trip_id, "phase": "completed"}

        record = _trip_record(state, trip_id, user_id)
        await store.aput(namespace, str(trip_id), record.model_dump(mode="json"))
        return {"trip_id": trip_id, "phase": "completed"}


def _trip_record(state: TravelState, trip_id: UUID, user_id: str) -> TripRecord:
    """从最终校验后的状态构造 Store 记录，不重新解释 Agent 输出。

    展示快照必须从 Provider 事实水合，不能信任 Planner 文案；用户 ID 必须来自认证上下文，
    不能取自客户端。这里再次检查关键字段，防止未来调整拓扑时保存节点被错误前移。
    """

    facts = state.get("facts", TravelFacts())
    draft = state.get("draft")
    budget = state.get("budget")
    if facts.city is None or draft is None or not isinstance(budget, BudgetBreakdown):
        raise TravelGraphError("final_state_incomplete", "行程保存前状态不完整")
    return TripRecord(
        trip_id=trip_id,
        user_id=user_id,
        requirements=state["requirements"],
        city=facts.city,
        selection=state.get("selection", TravelSelection()),
        draft=draft,
        weather=facts.weather,
        routes=facts.routes,
        budget=budget,
        place_index=_place_index(facts),
        warnings=[item.message for item in state.get("warnings", [])],
    )


def _place_index(facts: TravelFacts) -> dict[str, PlaceSnapshot]:
    """仅从 Provider 事实生成历史展示快照。"""

    values: dict[str, PlaceSnapshot] = {}
    if facts.catalog:
        for poi in facts.catalog.all:
            values[poi.id] = PlaceSnapshot(
                fact_id=poi.id,
                name=poi.name,
                address=poi.address,
                category=poi.category,
                latitude=poi.latitude,
                longitude=poi.longitude,
                image_url=poi.image_url,
            )
    for hotel in facts.hotel_options:
        values[hotel.hotel_id] = PlaceSnapshot(
            fact_id=hotel.hotel_id,
            name=hotel.name,
            address=hotel.address,
            category="hotel",
            latitude=hotel.latitude,
            longitude=hotel.longitude,
            image_url=hotel.image_url,
        )
    return values

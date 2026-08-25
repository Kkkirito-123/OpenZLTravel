"""最终行程保存节点：只把工单校验通过的状态写入 Store。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.config import get_config
from langgraph.runtime import Runtime

from domain.errors import TravelGraphError
from domain.models import (
    BudgetBreakdown,
    PlaceSnapshot,
    TravelFacts,
    TripRecord,
)
from domain.planning import selected_rail
from travel_graph.state import TravelContext, TravelState
from travel_graph.utils import user_id_from


class PersistenceNodes:
    """将最终状态转换为 Store 记录并幂等保存。

    保存主键由当前用户和 ``order_id`` 稳定生成。相同工单重复运行时返回同一个
    ``trip_id``，不会产生重复历史行程；持久化层只接收已经通过路线、预算和选择校验的
    Graph 状态。
    """

    async def save(
        self,
        state: TravelState,
        runtime: Runtime[TravelContext],
    ) -> dict[str, object]:
        """按认证用户和工单构造稳定主键；已有相同 key 时直接返回完成状态。"""

        store = runtime.store
        if store is None:
            raise TravelGraphError("store_unavailable", "保存行程需要 LangGraph Store")
        config = get_config()
        user_id = user_id_from(config, runtime)
        order = state["order"]
        trip_id = uuid5(NAMESPACE_URL, f"openzltravel:{user_id}:{order.order_id}")
        namespace = (user_id, "trips")
        existing = await store.aget(namespace, str(trip_id))
        if existing is not None:
            return {"trip_id": trip_id, "phase": "completed"}

        record = _trip_record(state, trip_id, user_id)
        await store.aput(namespace, str(trip_id), record.model_dump(mode="json"))
        return {"trip_id": trip_id, "phase": "completed"}


def _trip_record(state: TravelState, trip_id: UUID, user_id: str) -> TripRecord:
    """从 Provider 事实水合 Store 记录，不信任 Planner 文案或客户端用户 ID。"""

    order = state["order"]
    facts = state.get("facts", order.facts)
    draft = state.get("draft")
    budget = state.get("budget")
    if facts.city is None or draft is None or not isinstance(budget, BudgetBreakdown):
        raise TravelGraphError("final_state_incomplete", "行程保存前状态不完整")
    selection = order.selection
    return TripRecord(
        trip_id=trip_id,
        user_id=user_id,
        requirements=order.requirements,
        city=facts.city,
        selection=selection,
        outbound_rail=selected_rail(selection.outbound, facts.outbound_options),
        return_rail=selected_rail(selection.return_trip, facts.return_options),
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

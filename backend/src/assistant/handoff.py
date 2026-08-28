"""Assistant 向 TravelGraph 交接工单的唯一边界。"""

from __future__ import annotations

from datetime import datetime, timezone

from assistant.fact_service import AssistantFactService
from assistant.models import AssistantHandoff, AssistantSnapshot
from assistant.selection import travel_choices_complete
from domain.errors import TravelGraphError
from domain.models import CandidateCatalog, TravelFacts, TravelOrder
from domain.validation import validate_selection
from runtime.config import Settings
from runtime.tokens import SignedPayloadCodec


class HandoffService:
    """刷新时间敏感事实、构造最小工单并签发短时令牌。"""

    def __init__(self, settings: Settings, codec: SignedPayloadCodec) -> None:
        self.settings = settings
        self.codec = codec

    async def submit(
        self,
        facts: AssistantFactService,
        user_id: str,
        incomplete_reply: str,
    ) -> tuple[str, AssistantHandoff | None]:
        """只有会话完整且刷新后的选择仍有效时才创建交接。"""

        snapshot = facts.snapshot
        if snapshot.status != "ready" or not travel_choices_complete(snapshot):
            return incomplete_reply, None
        requirements = snapshot.requirements
        assert requirements.origin and requirements.destination
        assert requirements.start_date and requirements.end_date
        await facts.search_rail(
            requirements.origin,
            requirements.destination,
            requirements.start_date,
            requirements.end_date,
        )
        if requirements.days_count > 1:
            await facts.search_hotels()
        await facts.get_weather()
        try:
            validate_selection(requirements, snapshot.facts, snapshot.selection)
        except TravelGraphError:
            snapshot.status = "collecting"
            return "价格刷新后原选择已不可用，请重新选择车次或酒店。", None

        order = build_order(snapshot)
        order_token = self.codec.issue(
            "travel_order",
            user_id,
            order,
            self.settings.travel_order_ttl_seconds,
        )
        snapshot.status = "submitted"
        return "事实已刷新，旅行工单已提交，开始生成最终规划。", AssistantHandoff(
            order=order,
            order_token=order_token,
        )


def build_order(snapshot: AssistantSnapshot) -> TravelOrder:
    """裁剪为只包含已选事实的最小工单，路线留给 TravelGraph 查询。"""

    issued_at = datetime.now(timezone.utc)
    catalog = snapshot.facts.catalog
    assert catalog is not None
    selected = set(snapshot.selection.attraction_ids)
    selected_catalog = CandidateCatalog(
        attractions=[item for item in catalog.attractions if item.id in selected],
        restaurants=catalog.restaurants[:6],
        hotels=catalog.hotels[:4],
        required_attraction_ids=snapshot.selection.attraction_ids,
    )
    outbound_ids = (
        {snapshot.selection.outbound.option_id}
        if snapshot.selection.outbound
        else set()
    )
    return_ids = (
        {snapshot.selection.return_trip.option_id}
        if snapshot.selection.return_trip
        else set()
    )
    hotel_ids = {snapshot.selection.hotel_id} if snapshot.selection.hotel_id else set()
    facts = TravelFacts(
        city=snapshot.facts.city,
        catalog=selected_catalog,
        outbound_options=[
            item for item in snapshot.facts.outbound_options if item.option_id in outbound_ids
        ],
        return_options=[
            item for item in snapshot.facts.return_options if item.option_id in return_ids
        ],
        hotel_options=[
            item for item in snapshot.facts.hotel_options if item.hotel_id in hotel_ids
        ],
        weather=snapshot.facts.weather,
    )
    return TravelOrder(
        created_at=issued_at,
        requirements=snapshot.requirements,
        facts=facts,
        selection=snapshot.selection,
        fact_metadata=snapshot.fact_metadata,
        facts_refreshed_at=issued_at,
    )

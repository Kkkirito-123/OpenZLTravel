"""Assistant 工单令牌的认证与事实校验入口。"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from openzltravel.domain.errors import TravelGraphError
from openzltravel.domain.models import TravelOrder
from openzltravel.domain.validation import validate_selection
from openzltravel.runtime.tokens import SignedPayloadCodec, TokenError
from openzltravel.travel_graph.state import TravelContext, TravelInput
from openzltravel.travel_graph.utils import user_id_from


class OrderNodes:
    """只接受绑定当前用户的短时 TravelOrderToken。

    该节点是 Graph 的信任边界：先检查令牌格式、签名、类型、所有者和过期时间，再检查
    工单中的选择是否属于已携带事实。任何失败都在进入规划节点前终止运行。
    """

    def __init__(self, codec: SignedPayloadCodec) -> None:
        self.codec = codec

    async def validate(
        self,
        state: TravelInput,
        config: RunnableConfig,
        runtime: Runtime[TravelContext],
    ) -> dict[str, object]:
        """验证工单并把安全载荷转换成 Graph 初始状态。"""

        token = state.get("order_token")
        if not isinstance(token, str) or not token:
            raise TravelGraphError("order_token_missing", "缺少旅行工单令牌")
        user_id = user_id_from(config, runtime)
        try:
            order = self.codec.verify(token, "travel_order", user_id, TravelOrder)
        except TokenError as error:
            raise TravelGraphError(error.code, error.message) from error
        validate_selection(order.requirements, order.facts, order.selection)
        return {
            "phase": "planning",
            "order": order,
            "facts": order.facts,
            "route_revision_instruction": None,
            "warnings": [],
            "errors": [],
        }

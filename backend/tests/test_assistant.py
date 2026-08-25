"""独立 Assistant 的会话、工具选择和工单交接测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage

import openzltravel.assistant.service as assistant_service_module
from openzltravel.assistant.models import (
    AssistantAction,
    AssistantDecision,
    AssistantSnapshot,
    AssistantTurnRequest,
)
from openzltravel.assistant.service import AssistantModelError, AssistantService
from openzltravel.assistant.tools import AssistantToolbox
from openzltravel.domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
    TravelOrder,
    TravelRequirements,
)
from openzltravel.infrastructure.providers.fakes import (
    FakeCatalogProvider,
    FakeHotelProvider,
    FakeRailProvider,
    FakeWeatherProvider,
)
from openzltravel.runtime.config import Settings
from openzltravel.runtime.contracts import AssistantDependencies
from openzltravel.runtime.tokens import SignedPayloadCodec, TokenError


class ScriptedAssistantService(AssistantService):
    """只用于领域流程测试；生产服务没有规则对话降级。"""

    async def _respond(
        self,
        snapshot: AssistantSnapshot,
        toolbox: AssistantToolbox,
    ) -> str:
        current = snapshot.requirements
        if current.destination and snapshot.facts.catalog is None:
            await toolbox.search_pois(current.destination)
        if (
            snapshot.selection.attraction_ids
            and not current.missing_fields()
            and current.budget is not None
        ):
            assert current.origin and current.destination
            assert current.start_date and current.end_date
            if not snapshot.facts.outbound_options:
                await toolbox.search_rail(
                    current.origin,
                    current.destination,
                    current.start_date,
                    current.end_date,
                )
            if current.days_count > 1 and not snapshot.facts.hotel_options:
                await toolbox.search_hotels()
            if not snapshot.facts.weather:
                await toolbox.get_weather()
        return "测试模型回复"

    async def _decide(
        self,
        snapshot: AssistantSnapshot,
        user_text: str,
    ) -> AssistantDecision:
        patch = (
            {
                "origin": "上海",
                "destination": "杭州",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "budget": 5000,
            }
            if "从上海去杭州" in user_text
            else {}
        )
        return AssistantDecision(
            reply="测试模型回复",
            patch=patch,
            attraction_ids=(
                ["poi-west-lake", "poi-lingyin"]
                if "西湖和灵隐寺" in user_text
                else []
            ),
            submit_requested=user_text == "开始规划",
        )


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return Settings.from_env()


def _dependencies(*, hotel_warning: str | None = None) -> AssistantDependencies:
    city = City(name="杭州", adcode="330100", latitude=30.27, longitude=120.15)
    catalog = CandidateCatalog(
        attractions=[
            Poi(
                id="poi-west-lake",
                name="西湖",
                category="attraction",
                latitude=30.25,
                longitude=120.14,
            ),
            Poi(
                id="poi-lingyin",
                name="灵隐寺",
                category="attraction",
                latitude=30.24,
                longitude=120.10,
            ),
        ],
        restaurants=[
            Poi(
                id="poi-food",
                name="杭帮菜馆",
                category="restaurant",
                latitude=30.26,
                longitude=120.15,
            )
        ],
        hotels=[
            Poi(
                id="poi-hotel",
                name="湖滨酒店",
                category="hotel",
                latitude=30.26,
                longitude=120.16,
            )
        ],
    )
    destinations = [
        DestinationCandidate(
            candidate_id="destination-hangzhou",
            city=city,
            score=0.95,
            reasons=["人文与自然景点丰富"],
        )
    ]
    return AssistantDependencies(
        catalog=FakeCatalogProvider(city, catalog, destinations),
        rail=FakeRailProvider(),
        hotels=FakeHotelProvider(
            [HotelOption(hotel_id="hotel-live-1", name="湖滨酒店", total_price=680)],
            warning=hotel_warning,
        ),
        weather=FakeWeatherProvider(),
    )


def _session(events: list[tuple[str, dict[str, object]]]) -> tuple[AssistantSnapshot, str]:
    payload = next(data for event, data in events if event == "session.updated")
    return (
        AssistantSnapshot.model_validate(payload["snapshot"]),
        str(payload["session_token"]),
    )


@pytest.mark.asyncio
async def test_assistant_collects_choices_and_issues_refreshed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    codec = SignedPayloadCodec(settings.signing_secret)
    service = ScriptedAssistantService(_dependencies(), settings, codec)
    user_id = "user-1"

    events = await service.turn(
        AssistantTurnRequest(
            message="从上海去杭州，2026年10月1日至2日，2人，预算5000元"
        ),
        user_id,
    )
    snapshot, token = _session(events)
    assert snapshot.facts.catalog is not None
    assert [event for event, _data in events if event == "tool.started"] == ["tool.started"]

    events = await service.turn(
        AssistantTurnRequest(
            session_token=token,
            action=AssistantAction(
                kind="select_attractions",
                attraction_ids=["poi-west-lake", "poi-lingyin"],
            ),
        ),
        user_id,
    )
    snapshot, token = _session(events)
    assert snapshot.facts.outbound_options
    assert snapshot.facts.return_options
    assert snapshot.facts.hotel_options
    assert snapshot.facts.weather

    outbound_id = snapshot.facts.outbound_options[0].option_id
    returning_id = snapshot.facts.return_options[0].option_id
    for action in (
        AssistantAction(kind="select_outbound", option_id=outbound_id, seat_type="二等座"),
        AssistantAction(kind="select_return", option_id=returning_id, seat_type="二等座"),
        AssistantAction(kind="select_hotel", hotel_id="hotel-live-1"),
    ):
        events = await service.turn(
            AssistantTurnRequest(session_token=token, action=action),
            user_id,
        )
        snapshot, token = _session(events)

    assert snapshot.status == "ready"
    submitted = await service.turn(
        AssistantTurnRequest(session_token=token, message="开始规划"),
        user_id,
    )
    handoff = next(data for event, data in submitted if event == "handoff.ready")
    order = codec.verify(
        str(handoff["order_token"]),
        "travel_order",
        user_id,
        TravelOrder,
    )
    assert order.facts_refreshed_at >= order.created_at
    assert order.selection.attraction_ids == ["poi-west-lake", "poi-lingyin"]
    assert [item.id for item in order.facts.catalog.attractions] == [
        "poi-west-lake",
        "poi-lingyin",
    ]
    assert len(order.facts.outbound_options) == 1
    assert len(order.facts.return_options) == 1
    assert len(order.facts.hotel_options) == 1


@pytest.mark.asyncio
async def test_natural_language_selection_uses_only_known_fact_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    service = ScriptedAssistantService(
        _dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )
    initial = await service.turn(
        AssistantTurnRequest(
            message="从上海去杭州，2026年10月1日至2日，预算5000元"
        ),
        "user-1",
    )
    _snapshot, token = _session(initial)

    selected = await service.turn(
        AssistantTurnRequest(session_token=token, message="我想去西湖和灵隐寺"),
        "user-1",
    )
    snapshot, _token = _session(selected)

    assert snapshot.selection.attraction_ids == ["poi-west-lake", "poi-lingyin"]
    with pytest.raises(ValueError, match="未知 ID"):
        await service.turn(
            AssistantTurnRequest(
                session_token=token,
                action=AssistantAction(
                    kind="select_attractions", attraction_ids=["invented-poi"]
                ),
            ),
            "user-1",
        )


@pytest.mark.asyncio
async def test_provider_degradation_is_exposed_as_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch)
    snapshot = AssistantSnapshot(
        requirements=TravelRequirements(
            origin="上海",
            destination="杭州",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 2),
            budget=5000,
        )
    )
    toolbox = AssistantToolbox(
        _dependencies(hotel_warning="实时价格不可用，已使用目录降级"), snapshot
    )

    await toolbox.search_pois("杭州")
    await toolbox.search_hotels()

    result = next(
        data
        for event, data in toolbox.events
        if event == "tool.result" and data["name"] == "search_hotels"
    )
    assert result["data"]["warning"] == "实时价格不可用，已使用目录降级"


def test_signed_tokens_reject_tampering_expiry_and_cross_user() -> None:
    codec = SignedPayloadCodec("s" * 32)
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    token = codec.issue(
        "assistant_session",
        "user-1",
        AssistantSnapshot(),
        60,
        now=now,
    )

    with pytest.raises(TokenError) as tampered:
        codec.verify(token + "x", "assistant_session", "user-1", AssistantSnapshot, now=now)
    assert tampered.value.code == "token_tampered"

    with pytest.raises(TokenError) as expired:
        codec.verify(
            token,
            "assistant_session",
            "user-1",
            AssistantSnapshot,
            now=now + timedelta(seconds=61),
        )
    assert expired.value.code == "token_expired"

    with pytest.raises(TokenError) as cross_user:
        codec.verify(token, "assistant_session", "user-2", AssistantSnapshot, now=now)
    assert cross_user.value.code == "token_owner_mismatch"


@pytest.mark.asyncio
async def test_missing_model_is_not_silently_replaced_by_rule_dialogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    service = AssistantService(
        _dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )

    with pytest.raises(AssistantModelError, match="拒绝切换到规则问答"):
        await service.turn(AssistantTurnRequest(message="想出去散心"), "user-1")


@pytest.mark.asyncio
async def test_model_reply_is_not_overwritten_by_missing_field_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    service = ScriptedAssistantService(
        _dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )

    events = await service.turn(AssistantTurnRequest(message="想出去散心"), "user-1")
    reply = next(data for event, data in events if event == "message.delta")

    assert reply["content"] == "测试模型回复"


@pytest.mark.asyncio
async def test_agent_uses_model_compatible_json_instead_of_forced_tool_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = Settings.from_env()
    class FakeExtractor:
        async def ainvoke(self, _prompt: str) -> AIMessage:
            return AIMessage(content='{"reply":"待生成","patch":{}}')

    class FakeModel:
        def bind(self, **_kwargs: object) -> FakeExtractor:
            return FakeExtractor()

    service = AssistantService(
        _dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )
    service.model = FakeModel()  # type: ignore[assignment]

    decision = await service._decide(AssistantSnapshot(), "心情不好")

    assert decision.reply == "待生成"


@pytest.mark.asyncio
async def test_agent_uses_model_compatible_json_for_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("PROVIDER_MODE", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings = Settings.from_env()

    class FakeAgent:
        async def ainvoke(self, _payload: object) -> dict[str, object]:
            return {
                "messages": [
                    AIMessage(
                        content='```json\n{"reply":"先聊聊你想怎么放松。"}\n```'
                    )
                ]
            }

    def fake_create_agent(*_args: object, **_kwargs: object) -> FakeAgent:
        return FakeAgent()

    monkeypatch.setattr(assistant_service_module, "create_agent", fake_create_agent)
    service = AssistantService(
        _dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )
    service.model = object()  # type: ignore[assignment]
    reply = await service._respond(
        AssistantSnapshot(),
        AssistantToolbox(_dependencies(), AssistantSnapshot()),
    )

    assert reply == '```json\n{"reply":"先聊聊你想怎么放松。"}\n```'

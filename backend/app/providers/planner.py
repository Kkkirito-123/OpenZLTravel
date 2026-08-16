"""受控模型规划器与确定性分天规划器。

旧快速入口继续使用结构化 LLM 规划；V0.3 工作台使用确定性规划器，模型只可选地润色
摘要和提示，不能改动地点、车票、酒店、天气、路线或价格事实。
"""

import json
import math
from collections.abc import Sequence
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from app.config import Settings
from app.errors import DraftError, ProviderError
from app.models import (
    CandidateCatalog,
    CopyEnhancement,
    DraftActivity,
    DraftDay,
    ItineraryDraft,
    PlanningRequest,
    Poi,
    RailOption,
    TravelRequest,
)


class Planner(Protocol):
    """旧快速规划入口依赖的结构化规划器接口。"""

    def plan(
        self,
        request: TravelRequest,
        candidates: CandidateCatalog,
        feedback: str | None = None,
    ) -> ItineraryDraft:
        """从候选池生成结构化草稿。"""

        ...


class LlmPlanner:
    """让模型做有限的结构化选择，不允许模型编造地图事实。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.llm_api_key or "missing-key",
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
        )

    def plan(
        self,
        request: TravelRequest,
        candidates: CandidateCatalog,
        feedback: str | None = None,
    ) -> ItineraryDraft:
        """请求一次结构化规划，解析失败由旅行服务决定是否修复重试。"""

        if not self.settings.llm_api_key or not self.settings.llm_model:
            raise ProviderError("llm_not_configured", "尚未配置兼容模型的 API Key 或模型名")
        try:
            response = self.client.chat.completions.create(
                model=self.settings.llm_model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": _user_prompt(request, candidates, feedback)},
                ],
            )
            content = response.choices[0].message.content or ""
            return ItineraryDraft.model_validate(json.loads(_clean_json(content)))
        except (ValidationError, json.JSONDecodeError, IndexError, TypeError) as exc:
            raise DraftError("模型返回的行程结构无法解析，请重试") from exc
        except Exception as exc:
            raise ProviderError("llm_unavailable", "规划模型暂时不可用") from exc


class DeterministicPlanner:
    """按日期、节奏和空间距离生成可重复的行程草稿。"""

    PACE_LIMITS = {"轻松": 2, "适中": 3, "紧凑": 4}

    def plan(
        self,
        request: PlanningRequest,
        candidates: CandidateCatalog,
        outbound: RailOption | None,
        return_trip: RailOption | None,
        selected_hotel_id: str | None,
    ) -> ItineraryDraft:
        """确定性安排景点；首末日根据到发时间收缩可用容量。"""

        ordered = _nearest_neighbor(candidates.attractions, selected_hotel_id, candidates)
        capacities = self._capacities(request, outbound, return_trip)
        buckets = _distribute(ordered, capacities)
        days = [
            self._build_day(request, index, pois, candidates, selected_hotel_id)
            for index, pois in enumerate(buckets, start=1)
        ]
        return ItineraryDraft(
            summary=f"已根据交通时刻与地点距离整理 {request.destination} 行程。",
            days=days,
            tips=["车次、房价和天气可能变化，出发前请再次确认。"],
        )

    def _capacities(
        self,
        request: PlanningRequest,
        outbound: RailOption | None,
        return_trip: RailOption | None,
    ) -> list[int]:
        base = self.PACE_LIMITS[request.pace]
        capacities = [base] * request.days_count
        if outbound and _hour(outbound.arrival_time) >= 14:
            capacities[0] = 0 if _hour(outbound.arrival_time) >= 18 else 1
        if return_trip and _hour(return_trip.departure_time) <= 14:
            capacities[-1] = 0 if _hour(return_trip.departure_time) <= 10 else 1
        return capacities

    def _build_day(
        self,
        request: PlanningRequest,
        day_index: int,
        pois: list[Poi],
        candidates: CandidateCatalog,
        selected_hotel_id: str | None,
    ) -> DraftDay:
        start_hour = 9
        activities = [
            DraftActivity(
                poi_id=poi.id,
                start_time=f"{start_hour + offset * 3:02d}:00",
                duration_minutes=120,
                note="按空间顺序游览，减少折返。",
            )
            for offset, poi in enumerate(pois)
        ]
        meals = [item.id for item in candidates.restaurants[:2]]
        hotel_id = selected_hotel_id if day_index < request.days_count else None
        theme = "抵达与安顿" if not pois else " · ".join(item.name for item in pois[:2])
        return DraftDay(
            day_index=day_index,
            theme=theme,
            activities=activities,
            meal_ids=meals,
            hotel_id=hotel_id,
            notes=[] if pois else ["当天受车次时间约束，不强行安排景点。"],
        )


class CopyEnhancer:
    """可选润色确定性草稿，只允许返回摘要、主题和提示。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.llm_api_key or "missing-key",
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_enhancement_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        """只有密钥和模型名都存在时才启用文案增强。"""

        return bool(self.settings.llm_api_key and self.settings.llm_model)

    def enhance(self, request: PlanningRequest, draft: ItineraryDraft) -> ItineraryDraft:
        """润色现有文案，地点、交通、天气和价格完全不进入输出。"""

        if not self.enabled:
            return draft
        payload = {
            "destination": request.destination,
            "pace": request.pace,
            "preferences": request.preferences,
            "dietary_preferences": request.dietary_preferences,
            "summary": draft.summary,
            "themes": [day.theme for day in draft.days],
            "tips": draft.tips,
            "rules": "只润色文案，themes 数量必须与输入一致，不添加地点或事实。",
        }
        try:
            response = self.client.chat.completions.create(
                model=self.settings.llm_model,
                temperature=0.3,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": "只输出摘要、每日主题和旅行提示 JSON，不得新增任何事实。",
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            content = response.choices[0].message.content or ""
            enhanced = CopyEnhancement.model_validate(json.loads(_clean_json(content)))
        except (ValidationError, json.JSONDecodeError, IndexError, TypeError) as exc:
            raise DraftError("文案增强结果无法解析") from exc
        except Exception as exc:
            raise ProviderError("llm_unavailable", "文案增强暂时不可用") from exc
        if len(enhanced.themes) != len(draft.days):
            raise DraftError("文案增强返回的每日主题数量不一致")
        days = [
            day.model_copy(update={"theme": theme})
            for day, theme in zip(draft.days, enhanced.themes, strict=True)
        ]
        return draft.model_copy(
            update={"summary": enhanced.summary, "days": days, "tips": enhanced.tips}
        )


def _nearest_neighbor(
    attractions: Sequence[Poi], hotel_id: str | None, candidates: CandidateCatalog
) -> list[Poi]:
    """从酒店或首个景点开始做轻量最近邻排序。"""

    remaining = list(attractions)
    if not remaining:
        return []
    current = candidates.find(hotel_id) if hotel_id else remaining[0]
    ordered: list[Poi] = []
    while remaining:
        nearest = min(remaining, key=lambda poi: _distance(current or poi, poi))
        ordered.append(nearest)
        remaining.remove(nearest)
        current = nearest
    return ordered


def _distance(left: Poi, right: Poi) -> float:
    latitude = math.radians((left.latitude + right.latitude) / 2)
    x = math.radians(right.longitude - left.longitude) * math.cos(latitude)
    y = math.radians(right.latitude - left.latitude)
    return math.hypot(x, y)


def _distribute(items: list[Poi], capacities: list[int]) -> list[list[Poi]]:
    buckets: list[list[Poi]] = []
    offset = 0
    for capacity in capacities:
        buckets.append(items[offset : offset + capacity])
        offset += capacity
    return buckets


def _hour(value: str) -> int:
    try:
        return int(value.split(":", 1)[0])
    except (ValueError, IndexError):
        return 12


def _system_prompt() -> str:
    return """你是一个严谨的旅行规划器。只输出 JSON，不输出 Markdown。
你只能使用用户提供的候选 POI 的 id；不能创造地点、地址、坐标、天气或路线。
每天安排 1 到 4 个景点，餐厅和酒店只能从对应候选池选择。"""


def _user_prompt(request: TravelRequest, candidates: CandidateCatalog, feedback: str | None) -> str:
    payload: dict[str, Any] = {
        "request": request.model_dump(mode="json"),
        "candidates": candidates.prompt_data(),
        "output_schema": {
            "summary": "string",
            "days": [{"day_index": 1, "theme": "string", "activities": []}],
            "tips": ["string"],
        },
    }
    if feedback:
        payload["previous_error"] = feedback
    return json.dumps(payload, ensure_ascii=False)


def _clean_json(content: str) -> str:
    return content.strip().removeprefix("```json").removesuffix("```").strip()

"""高频旅行表达的轻量规则解析。

这里只提取用户明确写出的事实，不用地理常识补全城市或日期。
无法明确解析的句子交给 RequirementAgent，避免把规则解析器扩展成第二套 NLU。
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import Field

from domain.models import RequirementPatch, StrictModel, TravelRequirements


class FastParseResult(StrictModel):
    """快速解析结果及显式记忆指令。"""

    patch: RequirementPatch = Field(default_factory=RequirementPatch)
    understood: bool = False
    remember_fields: list[str] = Field(default_factory=list)
    forget_fields: list[str] = Field(default_factory=list)


_DATE_RE = re.compile(r"(?P<year>20\d{2})[\-/.年](?P<month>\d{1,2})[\-/.月](?P<day>\d{1,2})日?")
_ROUTE_RE = re.compile(
    r"从(?P<origin>[\u4e00-\u9fffA-Za-z]{2,12}?)(?:出发)?(?:去|到|前往)"
    r"(?P<destination>[\u4e00-\u9fffA-Za-z]{2,12}?)(?=玩|旅游|旅行|出发|，|,|\s|\d|$)"
)
_DESTINATION_RE = re.compile(
    r"(?:想去|去|到|前往)(?P<destination>[\u4e00-\u9fffA-Za-z]{2,12}?)(?=玩|旅游|旅行|，|,|\s|\d|$)"
)
_REGIONS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北", "国内")
_PREFERENCES = ("自然", "历史", "人文", "美食", "博物馆", "亲子", "海边", "登山", "摄影")
_DIETARY = ("素食", "清真", "不辣", "微辣", "川菜", "粤菜")
_MEMORY_NAMES = {
    "出发地": "origin",
    "偏好": "preferences",
    "饮食": "dietary_preferences",
    "节奏": "pace",
    "酒店": "hotel_level",
    "市内交通": "transport_mode",
}


def parse_fast_requirements(text: str) -> FastParseResult:
    """提取高置信槽位，并识别只有显式措辞才生效的记忆指令。

    这是图进入 RequirementAgent 之前的“便宜路径”：正则只接受明确写出的内容，解析
    不出来就返回空补丁，让图决定是否调用模型或追问，而不是在这里猜测用户意图。
    """

    normalized = text.strip()
    values: dict[str, object] = {}
    _parse_route(normalized, values)
    _parse_dates(normalized, values)
    _parse_counts_and_budget(normalized, values)
    _parse_preferences(normalized, values)
    remember, forget = _parse_memory_commands(normalized, values)
    patch = RequirementPatch.model_validate(values)
    understood = bool(patch.model_dump(exclude_none=True)) or bool(forget)
    return FastParseResult(
        patch=patch,
        understood=understood,
        remember_fields=remember,
        forget_fields=forget,
    )


def merge_requirements(
    current: TravelRequirements | None,
    patch: RequirementPatch,
) -> TravelRequirements:
    """以本轮明确值覆盖旧值，未出现的字段保持不变。

    ``RequirementPatch`` 是增量，``TravelRequirements`` 是图中的完整快照；这个函数是
    两者之间唯一的合并规则，避免不同节点各自实现覆盖逻辑。
    """

    base = current.model_dump() if current else TravelRequirements().model_dump()
    base.update(patch.model_dump(exclude_none=True))
    return TravelRequirements.model_validate(base)


def _parse_route(text: str, values: dict[str, object]) -> None:
    match = _ROUTE_RE.search(text)
    if match:
        values.update(match.groupdict())
    else:
        destination = _DESTINATION_RE.search(text)
        if destination:
            values["destination"] = destination.group("destination")
        origin = re.search(
            r"(?:从|出发地(?:是|为)?)([\u4e00-\u9fffA-Za-z]{2,12}?)(?=出发|，|,|\s|$)",
            text,
        )
        if origin:
            values["origin"] = origin.group(1)
    values["region"] = next((region for region in _REGIONS if region in text), None)


def _parse_dates(text: str, values: dict[str, object]) -> None:
    dates: list[date] = []
    for match in _DATE_RE.finditer(text):
        try:
            dates.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            continue
    if dates:
        values["start_date"] = dates[0]
    if len(dates) > 1:
        values["end_date"] = dates[1]
    days = re.search(r"(\d{1,2})\s*天", text)
    if days:
        values["trip_days"] = int(days.group(1))


def _parse_counts_and_budget(text: str, values: dict[str, object]) -> None:
    travelers = re.search(r"(\d{1,2})\s*(?:人|位)", text)
    if travelers:
        values["travelers"] = int(travelers.group(1))
    budget = re.search(r"预算(?:是|为|大约|约)?\s*(\d+(?:\.\d+)?)\s*(万)?", text)
    if budget:
        amount = float(budget.group(1))
        values["budget"] = amount * 10000 if budget.group(2) else amount
    values["pace"] = next((pace for pace in ("轻松", "适中", "紧凑") if pace in text), None)
    values["hotel_level"] = next(
        (level for level in ("经济", "舒适", "品质") if f"{level}酒店" in text),
        None,
    )
    modes = {"步行": "walk", "驾车": "driving", "公交": "transit", "地铁": "transit"}
    values["transport_mode"] = next((mode for name, mode in modes.items() if name in text), None)


def _parse_preferences(text: str, values: dict[str, object]) -> None:
    preferences = [item for item in _PREFERENCES if item in text]
    dietary = [item for item in _DIETARY if item in text]
    if preferences:
        values["preferences"] = preferences
    if dietary:
        values["dietary_preferences"] = dietary


def _parse_memory_commands(
    text: str,
    values: dict[str, object],
) -> tuple[list[str], list[str]]:
    remember: list[str] = []
    forget: list[str] = []
    if "记住" in text:
        remember = [field for field in _MEMORY_NAMES.values() if values.get(field) is not None]
    if "忘记" in text or "删除记忆" in text:
        forget = [field for label, field in _MEMORY_NAMES.items() if label in text]
    return remember, forget

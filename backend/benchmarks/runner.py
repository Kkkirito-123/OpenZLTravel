"""固定 Benchmark 的离线功能运行器与可选 LangSmith 质量入口。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import SecretStr

from benchmarks.cases import AssistantBenchmarkCase, GraphBenchmarkCase, load_cases
from benchmarks.fixtures import assistant_dependencies, planning_dependencies, travel_order
from openzltravel.assistant.models import (
    AssistantAction,
    AssistantDecision,
    AssistantSnapshot,
    AssistantTurnRequest,
)
from openzltravel.assistant.service import AssistantService
from openzltravel.assistant.tools import AssistantToolbox
from openzltravel.domain.errors import TravelGraphError
from openzltravel.runtime.config import Settings
from openzltravel.runtime.tokens import SignedPayloadCodec, TokenError
from openzltravel.travel_graph.workflow import build_travel_graph

TOOL_ALIASES = {
    "search_rail": "search_rail_options",
}


class ReplayAssistantService(AssistantService):
    """用固定决策和回复重放生产 AssistantService 的事实边界。"""

    def __init__(self, case: AssistantBenchmarkCase, settings: Settings, codec: SignedPayloadCodec):
        super().__init__(assistant_dependencies(), settings, codec)
        self.case = case

    async def _decide(self, snapshot: AssistantSnapshot, user_text: str) -> AssistantDecision:
        index = sum(item.role == "user" for item in snapshot.messages) - 1
        turn = self.case.turns[index]
        decision = AssistantDecision.model_validate(
            {"reply": turn.replay_reply, **turn.replay_decision}
        )
        return decision

    async def _respond(  # noqa: C901 - replay dispatch intentionally mirrors tool names
        self, snapshot: AssistantSnapshot, toolbox: AssistantToolbox
    ) -> str:
        index = sum(item.role == "user" for item in snapshot.messages) - 1
        turn = self.case.turns[index]
        for tool_name in turn.replay_tools:
            if tool_name == "resolve_place":
                await toolbox.resolve_place(snapshot.requirements.destination or "杭州")
            elif tool_name == "recommend_destinations":
                await toolbox.recommend_destinations(
                    snapshot.requirements.origin or "上海",
                    snapshot.requirements.region or "江南",
                )
            elif tool_name == "search_pois":
                await toolbox.search_pois(snapshot.requirements.destination or "杭州")
            elif tool_name in {"search_rail", "search_rail_options"}:
                if snapshot.requirements.origin and snapshot.requirements.destination:
                    if snapshot.requirements.start_date and snapshot.requirements.end_date:
                        await toolbox.search_rail(
                            snapshot.requirements.origin,
                            snapshot.requirements.destination,
                            snapshot.requirements.start_date,
                            snapshot.requirements.end_date,
                        )
            elif tool_name == "search_hotels":
                await toolbox.search_hotels()
            elif tool_name == "get_weather":
                await toolbox.get_weather()
            else:
                raise ValueError(f"未知 Replay 工具: {tool_name}")
        return turn.replay_reply


async def run_functional_suite() -> list[dict[str, Any]]:
    assistant_cases, graph_cases = load_cases()
    results: list[dict[str, Any]] = []
    for assistant_case in assistant_cases:
        results.append(await _run_assistant_case(assistant_case))
    for graph_case in graph_cases:
        results.append(await _run_graph_case(graph_case))
    return results


async def _run_assistant_case(case: AssistantBenchmarkCase) -> dict[str, Any]:
    started = time.perf_counter()
    settings = _benchmark_settings()
    codec = SignedPayloadCodec(settings.signing_secret)
    service = ReplayAssistantService(case, settings, codec)
    token: str | None = None
    all_events: list[tuple[str, dict[str, Any]]] = []
    last_snapshot = AssistantSnapshot()
    error: str | None = None
    try:
        for turn in case.turns:
            payload: dict[str, Any] = {}
            if turn.input.message is not None:
                payload["message"] = turn.input.message
            else:
                payload["action"] = AssistantAction.model_validate(turn.input.action)
            events = await service.turn(
                AssistantTurnRequest(session_token=token, **payload),
                "benchmark-user",
            )
            all_events.extend(events)
            session = next(data for event, data in events if event == "session.updated")
            token = str(session["session_token"])
            last_snapshot = AssistantSnapshot.model_validate(session["snapshot"])
            failed = _assert_assistant_turn(turn, events, last_snapshot)
            if failed:
                error = failed
                break
    except Exception as exc:  # benchmark must report a case, not abort the suite
        error = f"{type(exc).__name__}: {exc}"
    handoff = any(event == "handoff.ready" for event, _data in all_events)
    tools = [
        TOOL_ALIASES.get(str(data.get("name")), str(data.get("name")))
        for event, data in all_events
        if event == "tool.started"
    ]
    return {
        "case_id": case.case_id,
        "domain": "assistant",
        "status": "pass" if error is None else "fail",
        "functional": {
            "required_tools_seen": tools,
            "handoff": handoff,
            "snapshot_status": last_snapshot.status,
            "requirements": last_snapshot.requirements.model_dump(mode="json"),
        },
        "response_score": None,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "input_tokens": None,
        "output_tokens": None,
        "error": error,
    }


def _assert_assistant_turn(
    turn: Any,
    events: list[tuple[str, dict[str, Any]]],
    snapshot: AssistantSnapshot,
) -> str | None:
    expected = turn.expected
    actual_tools = {
        TOOL_ALIASES.get(str(data.get("name")), str(data.get("name")))
        for event, data in events
        if event == "tool.started"
    }
    expected_tools = {TOOL_ALIASES.get(item, item) for item in expected.required_tools}
    forbidden_tools = {TOOL_ALIASES.get(item, item) for item in expected.forbidden_tools}
    missing_tools = expected_tools - actual_tools
    forbidden = forbidden_tools & actual_tools
    if missing_tools:
        return f"缺少工具调用: {sorted(missing_tools)}"
    if forbidden:
        return f"出现禁止工具调用: {sorted(forbidden)}"
    for field, value in expected.requirements.items():
        actual = getattr(snapshot.requirements, field)
        actual = actual.isoformat() if hasattr(actual, "isoformat") else actual
        if actual != value:
            return f"字段 {field} 期望 {value!r}，实际 {actual!r}"
    if expected.status is not None and snapshot.status != expected.status:
        return f"状态期望 {expected.status}，实际 {snapshot.status}"
    if expected.handoff is not None:
        actual_handoff = any(event == "handoff.ready" for event, _data in events)
        if actual_handoff != expected.handoff:
            return f"handoff 期望 {expected.handoff}，实际 {actual_handoff}"
    return None


async def _run_graph_case(case: GraphBenchmarkCase) -> dict[str, Any]:  # noqa: C901
    started = time.perf_counter()
    codec = SignedPayloadCodec("b" * 32)
    store = InMemoryStore()
    order = travel_order(case.fixture)
    result: dict[str, Any] = {"case_id": case.case_id, "domain": "graph"}
    try:
        if case.operation == "legacy_input":
            graph = _graph(codec, store)
            await graph.ainvoke(
                {"messages": [{"role": "user", "content": "旧输入"}]}, _config("legacy")
            )
        elif case.operation in {"cross_user", "tampered_token", "expired_token"}:
            now = datetime.now(timezone.utc) - timedelta(minutes=20)
            ttl = 1 if case.operation == "expired_token" else 600
            token = codec.issue(
                "travel_order", "benchmark-user", order, ttl, now=now if ttl == 1 else None
            )
            if case.operation == "tampered_token":
                token += "x"
            owner = "another-user" if case.operation == "cross_user" else "benchmark-user"
            await _graph(codec, store).ainvoke({"order_token": token}, _config(case.case_id, owner))
        else:
            token = codec.issue("travel_order", "benchmark-user", order, 600)
            graph = _graph(codec, store)
            config = _config(case.case_id)
            initial = await graph.ainvoke({"order_token": token}, config)
            if case.operation == "revise_move":
                revised = await graph.ainvoke(
                    Command(
                        resume={
                            "kind": "route_preview",
                            "action": "message",
                            "text": "把西湖放到第二天",
                        }
                    ),
                    config,
                )
                completed = await graph.ainvoke(
                    Command(resume={"kind": "route_preview", "action": "confirm"}), config
                )
                result["trip_id"] = str(completed.get("trip_id"))
                result["phase"] = completed.get("phase")
                result["revision_applied"] = any(
                    activity.poi_id == "poi-west-lake"
                    for activity in revised["draft"].days[1].activities
                )
            elif case.operation == "revise_reduce":
                revised = await graph.ainvoke(
                    Command(
                        resume={
                            "kind": "route_preview",
                            "action": "message",
                            "text": "第一天少安排一个",
                        }
                    ),
                    config,
                )
                completed = await graph.ainvoke(
                    Command(resume={"kind": "route_preview", "action": "confirm"}), config
                )
                result["trip_id"] = str(completed.get("trip_id"))
                result["phase"] = completed.get("phase")
                result["revision_applied"] = len(revised["draft"].days[0].activities) < len(
                    initial["draft"].days[0].activities
                )
            elif case.operation == "revise_unsupported":
                rejected = await graph.ainvoke(
                    Command(
                        resume={"kind": "route_preview", "action": "message", "text": "整体更浪漫"}
                    ),
                    config,
                )
                interrupt = rejected["__interrupt__"][0].value
                result["error_code"] = interrupt["error"]["code"]
            else:
                completed = await graph.ainvoke(
                    Command(resume={"kind": "route_preview", "action": "confirm"}), config
                )
                result["phase"] = completed.get("phase")
                if case.operation == "idempotent":
                    replay = _graph(codec, store)
                    replay_config = _config(f"{case.case_id}-replay")
                    await replay.ainvoke({"order_token": token}, replay_config)
                    second = await replay.ainvoke(
                        Command(resume={"kind": "route_preview", "action": "confirm"}),
                        replay_config,
                    )
                    result["idempotent"] = second.get("trip_id") == completed.get("trip_id")
        expected_error = case.expected.error_code
        if expected_error and result.get("error_code") != expected_error:
            return _graph_result(
                case,
                "fail",
                result,
                f"期望错误 {expected_error}，实际 {result.get('error_code')}",
                started,
            )
        if case.expected.phase and result.get("phase") != case.expected.phase:
            return _graph_result(
                case,
                "fail",
                result,
                f"期望 phase {case.expected.phase}，实际 {result.get('phase')}",
                started,
            )
        if case.expected.idempotent and not result.get("idempotent"):
            return _graph_result(case, "fail", result, "重复运行未保持幂等", started)
        if case.expected.revision and not result.get("revision_applied"):
            return _graph_result(case, "fail", result, "路线修改未应用到草稿", started)
        return _graph_result(case, "pass", result, None, started)
    except (TravelGraphError, TokenError) as exc:
        code = getattr(exc, "code", None)
        result["error_code"] = code
        status = "pass" if code == case.expected.error_code else "fail"
        error = None if status == "pass" else f"期望错误 {case.expected.error_code}，实际 {code}"
        return _graph_result(case, status, result, error, started)
    except Exception as exc:
        return _graph_result(case, "fail", result, f"{type(exc).__name__}: {exc}", started)


def _graph(codec: SignedPayloadCodec, store: InMemoryStore) -> Any:
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("openzltravel.domain.models", "TravelOrder"),
            ("openzltravel.domain.models", "TravelFacts"),
            ("openzltravel.domain.models", "ItineraryDraft"),
            ("openzltravel.domain.models", "BudgetBreakdown"),
        ]
    )
    return build_travel_graph(
        planning_dependencies(), codec, checkpointer=MemorySaver(serde=serde), store=store
    )


def _config(thread_id: str, user_id: str = "benchmark-user") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def _graph_result(
    case: GraphBenchmarkCase,
    status: str,
    functional: dict[str, Any],
    error: str | None,
    started: float,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "domain": "graph",
        "status": status,
        "functional": functional,
        "response_score": None,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "input_tokens": None,
        "output_tokens": None,
        "error": error,
    }


def _benchmark_settings() -> Settings:
    names = ("APP_ENV", "AUTH_MODE", "PROVIDER_MODE", "LANGGRAPH_STRICT_MSGPACK")
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ["APP_ENV"] = "test"
        os.environ["AUTH_MODE"] = "dev"
        os.environ["PROVIDER_MODE"] = "fake"
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "false"
        return Settings.from_env()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_quality_suite() -> dict[str, Any]:
    """运行 LangSmith 质量层；未配置密钥时返回明确 skipped 报告。"""

    key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    if not key:
        return {
            "mode": "quality",
            "status": "skipped",
            "reason": "未配置 LANGSMITH_API_KEY 或 LANGCHAIN_API_KEY",
            "cases": [],
        }
    settings = _benchmark_settings()
    if not settings.model_api_key:
        return {
            "mode": "quality",
            "status": "skipped",
            "reason": "未配置当前 LLM API Key",
            "cases": [],
        }
    try:
        from langsmith import Client, evaluate
    except ImportError as exc:
        return {"mode": "quality", "status": "skipped", "reason": str(exc), "cases": []}

    assistant_cases, _graph_cases = load_cases()
    judge = ChatOpenAI(
        api_key=SecretStr(settings.model_api_key),
        base_url=settings.model_base_url,
        model=settings.fast_model,
        temperature=0,
        timeout=20,
        max_retries=0,
    )
    examples = [
        {
            "inputs": {"case": case.model_dump(mode="json")},
            "outputs": {"rubric": case.response_rubric},
            "metadata": {"case_id": case.case_id, "suite_version": "v1"},
        }
        for case in assistant_cases
    ]

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case = AssistantBenchmarkCase.model_validate(inputs["case"])
        try:
            results = asyncio.run(_run_real_assistant_case(case))
        except Exception as exc:
            results = {"reply": "", "error": f"{type(exc).__name__}: {exc}"}
        return {"reply": results.get("reply", ""), "functional": results}

    def evaluator(
        *,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        reference_outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reply = str(outputs.get("reply", ""))
        rubric = inputs.get("case", {}).get("response_rubric", [])
        prompt = (
            "你是中文旅行助手回复评审员。只根据给定回复和 rubric 评分，不能补造事实。"
            '请只返回 JSON：{"score": 0 到 10 的整数, "comment": "简短理由"}。'
            f"\nrubric={json.dumps(rubric, ensure_ascii=False)}"
            f"\nreply={reply}"
        )
        try:
            response = judge.invoke(prompt)
            content = getattr(response, "content", "")
            parsed = json.loads(str(content))
            score = max(0, min(10, int(parsed.get("score", 0)))) / 10
            comment = str(parsed.get("comment", ""))
        except Exception as exc:
            score = 0.0
            comment = f"评审失败: {exc}"
        return {"key": "response_score", "score": score, "comment": comment}

    experiment = evaluate(
        target,
        data=cast(Any, examples),
        evaluators=[evaluator],
        client=Client(api_key=key),
        max_concurrency=1,
        upload_results=True,
        experiment_prefix="openzltravel-benchmark-v1",
    )
    return {"mode": "quality", "status": "completed", "experiment": str(experiment), "cases": []}


async def _run_real_assistant_case(case: AssistantBenchmarkCase) -> dict[str, Any]:
    settings = _benchmark_settings()
    service = AssistantService(
        assistant_dependencies(), settings, SignedPayloadCodec(settings.signing_secret)
    )
    token: str | None = None
    reply = ""
    for turn in case.turns:
        payload: dict[str, Any] = {"session_token": token}
        if turn.input.message is not None:
            payload["message"] = turn.input.message
        else:
            payload["action"] = AssistantAction.model_validate(turn.input.action)
        events = await service.turn(AssistantTurnRequest(**payload), "benchmark-user")
        session = next(data for event, data in events if event == "session.updated")
        token = str(session["session_token"])
        reply = str(next(data for event, data in events if event == "message.delta")["content"])
    return {"reply": reply}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="OpenZLTravel 固定 Benchmark v1")
    parser.add_argument("--suite", choices=("functional", "quality", "all"), default="functional")
    parser.add_argument("--report", default="reports/latest.json")
    args = parser.parse_args()
    from benchmarks.report import write_report

    report: dict[str, Any] = {
        "suite_version": "v1",
        "run_id": datetime.now(timezone.utc).isoformat(),
        "mode": args.suite,
    }
    if args.suite in {"functional", "all"}:
        report["cases"] = asyncio.run(run_functional_suite())
    if args.suite in {"quality", "all"}:
        report["quality"] = run_quality_suite()
    report["summary"] = summarize(report.get("cases", []), report.get("quality"))
    write_report(report, Path(args.report))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


def summarize(cases: list[dict[str, Any]], quality: dict[str, Any] | None = None) -> dict[str, Any]:
    passed = sum(item.get("status") == "pass" for item in cases)
    latencies = sorted(
        float(item["latency_ms"])
        for item in cases
        if item.get("latency_ms") is not None
    )
    return {
        "functional_pass_rate": passed / len(cases) if cases else None,
        "response_score": None,
        "critical_violations": sum(item.get("status") == "fail" for item in cases),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "quality_status": quality.get("status") if quality else None,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, round((len(values) - 1) * fraction))
    return values[index]


if __name__ == "__main__":
    main()

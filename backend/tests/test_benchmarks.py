"""固定 Benchmark 数据、离线运行与质量层降级测试。"""

from __future__ import annotations

import pytest

from benchmarks.cases import load_cases
from benchmarks.runner import _run_assistant_case, _run_graph_case, run_quality_suite


def test_fixed_benchmark_contains_thirty_unique_cases() -> None:
    assistant, graph = load_cases()
    assert len(assistant) == 20
    assert len(graph) == 10
    assert len(assistant) + len(graph) == 30
    assert len({item.case_id for item in assistant + graph}) == 30


@pytest.mark.asyncio
async def test_functional_cases_are_replayable_without_live_providers() -> None:
    assistant, graph = load_cases()
    first_assistant = await _run_assistant_case(assistant[0])
    second_assistant = await _run_assistant_case(assistant[0])
    assert first_assistant["status"] == second_assistant["status"] == "pass"
    assert first_assistant["functional"] == second_assistant["functional"]

    graph_result = await _run_graph_case(graph[-1])
    assert graph_result["status"] == "pass"
    assert graph_result["functional"]["idempotent"] is True


def test_quality_suite_is_explicitly_skipped_without_langsmith(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    result = run_quality_suite()
    assert result["status"] == "skipped"
    assert "LANGSMITH_API_KEY" in result["reason"]

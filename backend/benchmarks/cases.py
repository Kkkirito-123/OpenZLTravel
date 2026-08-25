"""固定 Benchmark 数据契约与版本化用例加载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, min_length=1)
    action: dict[str, Any] | None = None


class BenchmarkExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    handoff: bool | None = None
    error_code: str | None = None


class BenchmarkTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: BenchmarkInput
    replay_decision: dict[str, Any] = Field(default_factory=dict)
    replay_reply: str = "测试回复"
    replay_tools: list[str] = Field(default_factory=list)
    expected: BenchmarkExpected = Field(default_factory=BenchmarkExpected)


class AssistantBenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    fixture: str
    turns: list[BenchmarkTurn] = Field(min_length=1)
    response_rubric: list[str] = Field(default_factory=list, max_length=8)


class GraphExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None = None
    error_code: str | None = None
    revision: str | None = None
    idempotent: bool = False


class GraphBenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    fixture: str
    operation: Literal[
        "confirm",
        "revise_move",
        "revise_reduce",
        "revise_unsupported",
        "legacy_input",
        "cross_user",
        "tampered_token",
        "expired_token",
        "idempotent",
        "unknown_fact",
    ]
    expected: GraphExpected


def load_cases(
    base_dir: Path | None = None,
) -> tuple[list[AssistantBenchmarkCase], list[GraphBenchmarkCase]]:
    """加载并校验固定版本的 Assistant 与 Graph 数据集。"""

    root = base_dir or Path(__file__).parent / "data"
    assistant = [
        AssistantBenchmarkCase.model_validate(json.loads(line))
        for line in (root / "assistant_v1.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    graph_payload = json.loads((root / "graph_v1.json").read_text(encoding="utf-8"))
    graph = [GraphBenchmarkCase.model_validate(item) for item in graph_payload]
    ids = [item.case_id for item in assistant + graph]
    if len(ids) != len(set(ids)):
        raise ValueError("Benchmark case_id 必须唯一")
    return assistant, graph

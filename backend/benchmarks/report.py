"""Benchmark JSON/Markdown 报告输出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown = path.with_suffix(".md")
    cases = report.get("cases", [])
    lines = [
        "# OpenZLTravel Benchmark v1",
        "",
        f"- mode: `{report.get('mode')}`",
        f"- run_id: `{report.get('run_id')}`",
        f"- functional_pass_rate: `{report.get('summary', {}).get('functional_pass_rate')}`",
        f"- critical_violations: `{report.get('summary', {}).get('critical_violations')}`",
        f"- latency_p50_ms: `{report.get('summary', {}).get('latency_p50_ms')}`",
        f"- latency_p95_ms: `{report.get('summary', {}).get('latency_p95_ms')}`",
        "",
        "| case_id | domain | status | latency_ms | input_tokens | output_tokens | error |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for case in cases:
        lines.append(
            f"| {case.get('case_id')} | {case.get('domain')} | {case.get('status')} | "
            f"{case.get('latency_ms', '')} | {case.get('input_tokens', '')} | "
            f"{case.get('output_tokens', '')} | {case.get('error') or ''} |"
        )
    if report.get("quality"):
        quality_json = json.dumps(report["quality"], ensure_ascii=False, indent=2, default=str)
        lines.extend(
            [
                "",
                "## LangSmith quality",
                "",
                f"```json\n{quality_json}\n```",
            ]
        )
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")

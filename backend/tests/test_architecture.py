"""新平台的结构约束测试。

这些断言不是检查实现细节，而是防止后续修改重新引入双状态、旧运行时或隐式依赖。
架构规则一旦变化，应先更新 ``ARCHITECTURE.md`` 并明确评审，再同步调整本文件。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
CORE_ROOT = BACKEND_ROOT / "src"
EXPECTED_SOURCE_PACKAGES = {
    "api",
    "catalog",
    "domain",
    "providers",
    "runtime",
    "travel_graph",
}

ALLOWED_INTERNAL_IMPORTS = {
    # 领域层只依赖自身；第三方 Pydantic 是数据建模工具，不改变这条方向。
    "domain": {"domain"},
    # Provider 和 Catalog 是事实适配层，可以共享领域模型与 Provider 基础设施。
    "providers": {"domain", "providers"},
    "catalog": {"catalog", "domain", "providers"},
    # runtime 是组合层，负责把实现装配成 Graph 所需的 Protocol。
    "runtime": {"catalog", "domain", "providers", "runtime"},
    # Graph 只能依赖领域模型、runtime 契约和自身的节点/状态；不能绕过容器接 Provider。
    "travel_graph": {"domain", "runtime", "travel_graph"},
    # API 只负责平台边界和历史读取，不参与 Graph 编排。
    "api": {"api", "domain", "runtime"},
}


def test_source_root_is_grouped_by_responsibility() -> None:
    """源码根目录只保留明确职责包，不再嵌套项目同名包。"""

    packages = {
        path.name
        for path in CORE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert packages == EXPECTED_SOURCE_PACKAGES
    assert not (CORE_ROOT / "openzltravel").exists()


def test_source_imports_use_responsibility_packages() -> None:
    """源码导入不得重新引入项目名或 src 前缀。"""

    violations: list[str] = []
    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module == "src" or module.startswith(("src.", "openzltravel.")):
                    violations.append(f"{path}: import {module}")
    assert violations == []


def test_internal_dependency_direction_matches_architecture() -> None:
    """用 AST 守护 ARCHITECTURE.md 中的职责依赖方向。

    这条测试只检查项目内部包，第三方库不在这里做白名单；这样新增 Provider 实现时，
    不会因为换 HTTP 客户端而修改架构测试，同时能阻止领域层或 Graph 重新反向依赖 API。
    """

    violations: list[str] = []
    for package, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        package_root = CORE_ROOT / package
        for path in package_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    root = module.split(".", 1)[0]
                    if root in ALLOWED_INTERNAL_IMPORTS and root not in allowed:
                        violations.append(f"{path}: {module}")
    assert violations == []


def test_graph_nodes_use_contracts_instead_of_runtime_implementations() -> None:
    """除组合根外，Graph 只能读取 runtime.contracts，不能直接构造配置或 Provider。"""

    violations: list[str] = []
    graph_root = CORE_ROOT / "travel_graph"
    for path in graph_root.rglob("*.py"):
        if path.name == "application.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.startswith("runtime.") and module != "runtime.contracts":
                    violations.append(f"{path}: {module}")
    assert violations == []


def test_graph_package_initializers_do_not_eagerly_load_workflow() -> None:
    """导入一个节点或 State 时，不应通过包入口隐式装载整张图。

    工作流的唯一组合根是 ``travel_graph.application``；包入口保持轻量，能让学习者从
    ``state``、``nodes`` 和 ``workflow`` 的显式导入看清依赖边界，也避免导入副作用。
    """

    package_files = [
        CORE_ROOT / "travel_graph" / "__init__.py",
        CORE_ROOT / "travel_graph" / "nodes" / "__init__.py",
    ]
    for path in package_files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "travel_graph.workflow" not in imported_modules
        assert not any(module.startswith("travel_graph.nodes.") for module in imported_modules)


def test_langgraph_exports_exactly_one_travel_graph() -> None:
    """Agent Server 只能暴露一个名为 travel 的业务根图。"""

    config = json.loads((REPOSITORY_ROOT / "langgraph.json").read_text(encoding="utf-8"))
    assert config["graphs"] == {
        "travel": "./backend/src/travel_graph/application.py:travel"
    }


def test_core_contains_exactly_three_agent_classes() -> None:
    """LLM 职责固定为需求、规划和审查，确定性节点不得伪装成 Agent。"""

    classes: list[str] = []
    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes.extend(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.endswith("Agent")
        )
    assert sorted(classes) == ["PlannerAgent", "RequirementAgent", "ReviewAgent"]


def test_core_does_not_import_removed_runtime_dependencies() -> None:
    """新核心不得重新依赖旧 app、re_zlagent、Redis 或 SQLite 业务运行时。"""

    forbidden_roots = {"app", "re_zlagent", "redis", "sqlite3"}
    violations: list[str] = []
    for path in CORE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if "dida_" in source.lower():
            violations.append(f"{path}: DIDA")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            for root in sorted(roots & forbidden_roots):
                violations.append(f"{path}: import {root}")
    assert violations == []


def test_removed_architecture_paths_do_not_return() -> None:
    """旧业务状态、迁移和负载测试目录删除后不得被新代码继续引用。"""

    removed = [
        BACKEND_ROOT / "app",
        BACKEND_ROOT / "database" / "app.sql",
        BACKEND_ROOT / "loadtests",
        BACKEND_ROOT / "scripts",
    ]
    remaining_files = [
        str(path)
        for path in removed
        if path.is_file() or (path.is_dir() and any(item.is_file() for item in path.rglob("*")))
    ]
    # Git 不记录空目录；本地曾运行旧版本时可能暂留空壳，但交付物中不能再有旧文件。
    assert remaining_files == []


def test_core_size_and_file_boundaries_remain_readable() -> None:
    """核心代码总量不超过 7000 行，单个普通业务文件不超过 400 行。"""

    line_counts = {
        path: len(path.read_text(encoding="utf-8").splitlines())
        for path in CORE_ROOT.rglob("*.py")
    }
    oversized = {str(path): count for path, count in line_counts.items() if count > 400}
    assert oversized == {}
    assert sum(line_counts.values()) <= 7000

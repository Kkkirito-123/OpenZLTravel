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
PACKAGE_ROOT = CORE_ROOT / "openzltravel"
EXPECTED_APPLICATION_PACKAGES = {
    "assistant",
    "domain",
    "infrastructure",
    "runtime",
    "travel_graph",
}

ALLOWED_INTERNAL_IMPORTS: dict[str, set[str]] = {
    # 领域层只依赖自身；第三方 Pydantic 是数据建模工具，不改变这条方向。
    "openzltravel.domain": {"openzltravel.domain"},
    # Provider 和 Catalog 是事实适配层，可以共享领域模型与 Provider 基础设施。
    "openzltravel.infrastructure.providers": {
        "openzltravel.domain",
        "openzltravel.infrastructure.providers",
    },
    "openzltravel.infrastructure.catalog": {
        "openzltravel.domain",
        "openzltravel.infrastructure.catalog",
        "openzltravel.infrastructure.providers",
    },
    # Assistant 是独立业务服务，只通过 runtime 契约访问事实 Provider。
    "openzltravel.assistant": {
        "openzltravel.assistant",
        "openzltravel.domain",
        "openzltravel.runtime",
    },
    # runtime 是组合层，负责把实现装配成 Graph 所需的 Protocol。
    "openzltravel.runtime": {
        "openzltravel.domain",
        "openzltravel.infrastructure.catalog",
        "openzltravel.infrastructure.providers",
        "openzltravel.runtime",
    },
    # Graph 只能依赖领域模型、runtime 契约/令牌和自身节点；不能绕过容器接 Provider。
    "openzltravel.travel_graph": {
        "openzltravel.domain",
        "openzltravel.runtime",
        "openzltravel.travel_graph",
    },
}


def _imported_modules(path: Path) -> list[str]:
    """提取一个 Python 文件的绝对导入模块，供目录依赖规则复用。"""

    modules: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_source_root_is_grouped_by_responsibility() -> None:
    """源码只有一个项目根包，根包内再按两个服务和共享职责分组。"""

    source_packages = {
        path.name
        for path in CORE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    application_packages = {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    infrastructure_packages = {
        path.name
        for path in (PACKAGE_ROOT / "infrastructure").iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert source_packages == {"openzltravel"}
    assert application_packages == EXPECTED_APPLICATION_PACKAGES
    assert infrastructure_packages == {"catalog", "providers"}
    assert (PACKAGE_ROOT / "travel_graph" / "api").is_dir()


def test_source_imports_use_responsibility_packages() -> None:
    """内部绝对导入必须从唯一的 ``openzltravel`` 根包开始。"""

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
                internal_names = {
                    "assistant",
                    "api",
                    "catalog",
                    "domain",
                    "providers",
                    "runtime",
                    "travel_graph",
                }
                root = module.split(".", 1)[0]
                if module == "src" or module.startswith("src.") or root in internal_names:
                    violations.append(f"{path}: import {module}")
    assert violations == []


def test_internal_dependency_direction_matches_architecture() -> None:
    """用 AST 守护 ARCHITECTURE.md 中的职责依赖方向。

    这条测试只检查项目内部包，第三方库不在这里做白名单；这样新增 Provider 实现时，
    不会因为换 HTTP 客户端而修改架构测试，同时能阻止领域层或 Graph 重新反向依赖 API。
    """

    violations: list[str] = []
    for package, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        package_root = CORE_ROOT.joinpath(*package.split("."))
        for path in package_root.rglob("*.py"):
            for module in _imported_modules(path):
                if not module.startswith("openzltravel."):
                    continue
                if not any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in allowed
                ):
                    violations.append(f"{path}: {module}")
    assert violations == []


def test_graph_nodes_use_only_runtime_contracts_and_tokens() -> None:
    """除组合根外，Graph 只能读取 runtime 契约、令牌和轻量配置上下文。"""

    violations: list[str] = []
    graph_root = PACKAGE_ROOT / "travel_graph"
    for path in graph_root.rglob("*.py"):
        if path.name == "application.py" or "api" in path.relative_to(graph_root).parts:
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
                if module.startswith("openzltravel.runtime.") and module not in {
                    "openzltravel.runtime.contracts",
                    "openzltravel.runtime.tokens",
                }:
                    violations.append(f"{path}: {module}")
    assert violations == []


def test_graph_package_initializers_do_not_eagerly_load_workflow() -> None:
    """导入一个节点或 State 时，不应通过包入口隐式装载整张图。

    工作流的唯一组合根是 ``openzltravel.travel_graph.application``；包入口保持轻量，能让学习者从
    ``state``、``nodes`` 和 ``workflow`` 的显式导入看清依赖边界，也避免导入副作用。
    """

    package_files = [
        PACKAGE_ROOT / "travel_graph" / "__init__.py",
        PACKAGE_ROOT / "travel_graph" / "nodes" / "__init__.py",
    ]
    for path in package_files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "openzltravel.travel_graph.workflow" not in imported_modules
        assert not any(
            module.startswith("openzltravel.travel_graph.nodes.")
            for module in imported_modules
        )


def test_langgraph_exports_exactly_one_travel_graph() -> None:
    """Agent Server 只能暴露一个名为 travel 的业务根图。"""

    config = json.loads((REPOSITORY_ROOT / "langgraph.json").read_text(encoding="utf-8"))
    assert config["graphs"] == {
        "travel": "./backend/src/openzltravel/travel_graph/application.py:travel"
    }


def test_core_contains_no_custom_agent_classes() -> None:
    """Agent 运行时由 LangChain create_agent 提供，不保留三 Agent 定制层。"""

    classes: list[str] = []
    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes.extend(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.endswith("Agent")
        )
    assert classes == []


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
        PACKAGE_ROOT / "runtime" / "model_gateway.py",
        PACKAGE_ROOT / "travel_graph" / "agents.py",
        PACKAGE_ROOT / "travel_graph" / "prompts.py",
    ]
    remaining_files = [
        str(path)
        for path in removed
        if path.is_file() or (path.is_dir() and any(item.is_file() for item in path.rglob("*")))
    ]
    # Git 不记录空目录；本地曾运行旧版本时可能暂留空壳，但交付物中不能再有旧文件。
    assert remaining_files == []


def test_core_size_and_file_boundaries_remain_readable() -> None:
    """核心非空源码不超过 7600 行，单个普通业务文件不超过 400 物理行。

    总量不统计空行，避免“增加分段来提高可读性”反而触发代码量失败；中文注释和
    docstring 仍保留在源码中。单文件边界继续按物理行计算，防止把复杂度藏进长文件。
    """

    line_counts = {
        path: len(path.read_text(encoding="utf-8").splitlines())
        for path in CORE_ROOT.rglob("*.py")
    }
    oversized = {str(path): count for path, count in line_counts.items() if count > 400}
    assert oversized == {}
    non_empty_lines = sum(
        1
        for path in line_counts
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert non_empty_lines <= 7600

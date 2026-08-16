"""地点目录构建器的资源释放与安全清理测试。"""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import pytest

from catalog_builder import build as build_module


class CloseableIterator:
    """记录 close 调用的最小迭代器。"""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> CloseableIterator:
        return self

    def __next__(self) -> Any:
        raise StopIteration

    def close(self) -> None:
        """标记底层资源已释放。"""

        self.closed = True


def test_close_iterator_releases_closeable_resource() -> None:
    """OSM 流完成后必须在临时目录清理前显式关闭。"""

    records = CloseableIterator()

    build_module._close_iterator(records)

    assert records.closed


def test_remove_osm_tempdir_retries_transient_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 短暂文件锁不得让完整构建失败。"""

    path = Path(mkdtemp(prefix=build_module.OSM_TEMP_PREFIX))
    (path / "nodes.cache").touch()
    real_rmtree = shutil.rmtree
    attempts = 0

    def flaky_rmtree(target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("模拟 Windows 文件锁")
        real_rmtree(target)

    monkeypatch.setattr(build_module.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(build_module.time, "sleep", lambda _delay: None)

    assert build_module._remove_osm_tempdir(path)
    assert attempts == 3
    assert not path.exists()


def test_remove_osm_tempdir_rejects_unrelated_directory(tmp_path: Path) -> None:
    """递归清理只能作用于系统临时目录中的专用前缀。"""

    unrelated = tmp_path / "other-data"
    unrelated.mkdir()

    with pytest.raises(RuntimeError, match="拒绝清理"):
        build_module._remove_osm_tempdir(unrelated)

    assert unrelated.exists()


def test_schedule_osm_cleanup_uses_a_windows_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程级文件锁应交给退出后清理器，不应阻断构建。"""

    path = Path(mkdtemp(prefix=build_module.OSM_TEMP_PREFIX))
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_popen(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(build_module.os, "name", "nt")
    monkeypatch.setattr(build_module.subprocess, "Popen", fake_popen)

    assert build_module._schedule_osm_tempdir_cleanup(path)
    assert len(calls) == 1
    assert calls[0][0][0] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        calls[0][0][0][-1],
    ]
    script = calls[0][0][0][-1]
    assert str(path) in script
    assert "Get-Process" not in script
    assert calls[0][1]["creationflags"] == build_module.subprocess.CREATE_NO_WINDOW

    shutil.rmtree(path)

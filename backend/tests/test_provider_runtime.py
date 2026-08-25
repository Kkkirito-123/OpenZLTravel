"""新 Provider 共享运行时的离线测试。"""

import asyncio

import httpx
import pytest

from openzltravel.infrastructure.providers.base import ProviderError, ProviderRuntime


@pytest.mark.asyncio
async def test_runtime_retries_network_once_and_then_uses_cache() -> None:
    calls = 0
    runtime = ProviderRuntime("demo", timeout_seconds=1, network_retries=1)

    async def operation() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline")
        return {"value": "ok"}

    first, first_hit = await runtime.run("same", operation, ttl_seconds=60)
    second, second_hit = await runtime.run("same", operation, ttl_seconds=60)

    assert first == second == {"value": "ok"}
    assert first_hit is False
    assert second_hit is True
    assert calls == 2


@pytest.mark.asyncio
async def test_runtime_does_not_retry_business_error() -> None:
    calls = 0
    runtime = ProviderRuntime("demo", timeout_seconds=1, network_retries=1)

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ProviderError("business_error", "参数无效")

    with pytest.raises(ProviderError, match="参数无效") as captured:
        await runtime.run("business", operation, ttl_seconds=60)

    assert captured.value.code == "business_error"
    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_exposes_stable_timeout_after_one_retry() -> None:
    calls = 0
    runtime = ProviderRuntime("slow", timeout_seconds=0.01, network_retries=1)

    async def operation() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)

    with pytest.raises(ProviderError) as captured:
        await runtime.run("slow", operation, ttl_seconds=60)

    assert captured.value.code == "slow_timeout"
    assert calls == 2

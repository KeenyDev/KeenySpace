from __future__ import annotations

import asyncio
import threading
import time

import pytest
from keenyspace_server.ws.thread_slots import LoopLocalSemaphore, run_in_thread_slot


@pytest.mark.asyncio
async def test_run_in_thread_slot_caps_concurrent_threads() -> None:
    slots = LoopLocalSemaphore(2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def _work(i: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return i

    results = await asyncio.gather(*(run_in_thread_slot(slots, _work, i) for i in range(6)))
    assert results == list(range(6))
    assert peak == 2


@pytest.mark.asyncio
async def test_run_in_thread_slot_propagates_exception_and_frees_slot() -> None:
    slots = LoopLocalSemaphore(1)

    def _boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_in_thread_slot(slots, _boom)
    assert not slots.get().locked()


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_slot_until_thread_ends_then_disposes() -> None:
    slots = LoopLocalSemaphore(1)
    release = threading.Event()
    disposed: list[str] = []

    def _work() -> str:
        release.wait(timeout=5)
        return "result"

    task = asyncio.create_task(run_in_thread_slot(slots, _work, on_abandoned=disposed.append))
    while not slots.get().locked():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.get().locked()
    assert disposed == []

    release.set()
    async with asyncio.timeout(5):
        while slots.get().locked() or not disposed:
            await asyncio.sleep(0.01)
    assert disposed == ["result"]


def test_loop_local_semaphore_is_distinct_per_event_loop() -> None:
    slots = LoopLocalSemaphore(1)

    async def _grab() -> asyncio.Semaphore:
        sem = slots.get()
        async with sem:
            return sem

    first = asyncio.run(_grab())
    second = asyncio.run(_grab())
    assert first is not second

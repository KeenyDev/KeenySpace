from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable


class LoopLocalSemaphore:
    """An `asyncio.Semaphore` created lazily per running event loop.

    A module-level `asyncio.Semaphore` binds to the first loop that contends
    on it, which breaks under per-test event loops.
    """

    def __init__(self, value: int) -> None:
        self._value = value
        self._by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

    def get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._by_loop.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self._value)
            self._by_loop[loop] = sem
        return sem


def _dispose_result[T](fut: asyncio.Future[T], on_abandoned: Callable[[T], None]) -> None:
    if not fut.cancelled() and fut.exception() is None:
        on_abandoned(fut.result())


async def run_in_thread_slot[T](
    slots: LoopLocalSemaphore,
    func: Callable[..., T],
    /,
    *args: object,
    on_abandoned: Callable[[T], None] | None = None,
) -> T:
    """Run `func(*args)` in a worker thread while holding one of `slots`.

    The slot is held until the thread finishes, even if the caller is
    cancelled first: a cancelled `to_thread` keeps running, and releasing
    early would let callers exceed the cap. When the caller is cancelled,
    `on_abandoned` receives the eventual result so it can release resources
    nobody else will see.
    """
    sem = slots.get()
    await sem.acquire()
    try:
        fut = asyncio.ensure_future(asyncio.to_thread(func, *args))
    except BaseException:
        sem.release()
        raise

    def _release(done: asyncio.Future[T]) -> None:
        sem.release()
        if not done.cancelled():
            done.exception()

    fut.add_done_callback(_release)
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if on_abandoned is not None:
            fut.add_done_callback(lambda done: _dispose_result(done, on_abandoned))
        raise

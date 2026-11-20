"""Cancellation barriers for owned work that must finish before cleanup."""
import asyncio
from typing import TypeVar

T = TypeVar("T")


async def complete(future: asyncio.Future[T]) -> T:
    """Defer even repeated caller cancellation until the owned work is done.

    Cancelling a to_thread task cannot stop its thread. Keeping that task alive
    also keeps its completion observable before rollback or another write.
    """
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break  # Retrieve the owned exception below.
    if cancelled:
        if not future.cancelled():
            future.exception()
        raise asyncio.CancelledError
    return future.result()


async def stop_task(task: asyncio.Task | None) -> None:
    """Request cancellation once; every stopper joins the same cleanup."""
    if task is None:
        return
    if not task.done() and not task.cancelling():
        task.cancel()
    await complete(asyncio.gather(task, return_exceptions=True))

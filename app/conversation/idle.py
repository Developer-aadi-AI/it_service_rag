"""Background idle monitor: periodically runs the manager's idle checks."""
from __future__ import annotations

import asyncio
import logging

from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


class IdleMonitor:
    def __init__(self, manager, interval_seconds: float) -> None:
        self.manager = manager
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            try:
                created = await run_in_threadpool(self.manager.sweep)
                if created:
                    logger.debug("idle monitor created %d messages", created)
            except Exception:
                logger.exception("idle monitor sweep failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="idle-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

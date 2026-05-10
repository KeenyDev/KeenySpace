from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler


def build_scheduler() -> AsyncIOScheduler:
    """AsyncIOScheduler with conservative defaults for single-worker uvicorn (D-11).

    - misfire_grace_time=30 lets a job that fires while a previous run is still
      executing (rare for our 15-min backstop interval) wait up to 30s before
      being skipped.
    - Memory jobstore is the default; we do NOT need persistence because jobs
      are recreated on every boot from coordinator method references.
    """
    return AsyncIOScheduler(job_defaults={"misfire_grace_time": 30})

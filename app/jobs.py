"""Minimal in-process background jobs: one at a time, polled by the UI.

Pulling a day (~10 min) and checking NSNs (minutes) are too slow for a request, but don't need a
queue/worker stack for a single-user tool.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone


class JobBusy(Exception):
    def __init__(self, job: dict):
        super().__init__(f"Already running: {job['label']}")
        self.job = job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: dict | None = None

    def start(self, kind: str, label: str, fn) -> dict:
        """Run fn(job) in a thread. fn reports progress by setting job['done'/'total'/'message']."""
        with self._lock:
            if self.current and self.current["status"] == "running":
                raise JobBusy(self.current)
            job = {
                "id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "status": "running",
                "done": 0, "total": None, "message": "Starting…", "result": None,
                "started_at": _now(), "finished_at": None,
            }
            self.current = job

        def run() -> None:
            try:
                job["result"] = fn(job)
                job["status"] = "done"
            except Exception as e:  # surface the failure to the UI instead of dying silently
                traceback.print_exc()
                job["status"] = "error"
                job["message"] = str(e) or e.__class__.__name__
            finally:
                job["finished_at"] = _now()

        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return job


runner = JobRunner()

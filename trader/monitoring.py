from __future__ import annotations

"""Bounded background jobs."""

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    progress: float = 0.0
    result: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)


class Jobs:
    def __init__(self, workers: int = 2) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(workers)

    def submit(self, kind: str, fn, *a, **k) -> Job:
        j = Job(uuid.uuid4().hex[:8], kind)
        with self._lock:
            if sum(1 for v in self.jobs.values() if v.status in ("queued", "running")) >= 4:
                j.status, j.error = "failed", "Too many jobs."
                self.jobs[j.id] = j
                return j
            self.jobs[j.id] = j

        def _run():
            if j.status == "canceled" or not self._sem.acquire(blocking=False):
                if j.status == "running":
                    pass
                if j.status != "canceled":
                    j.status, j.error = "failed", "No worker."
                return
            try:
                j.status = "running"
                j.result = fn(*a, **k) or {}
                if j.status == "running":
                    j.status, j.progress = "done", 1.0
            except Exception as exc:  # noqa: BLE001
                j.status, j.error = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                self._sem.release()
                with self._lock:
                    while len(self.jobs) > 50:
                        old = min(self.jobs.values(), key=lambda v: v.created)
                        del self.jobs[old.id]

        threading.Thread(target=_run, daemon=True).start()
        return j

    def cancel(self, jid: str) -> bool:
        with self._lock:
            j = self.jobs.get(jid)
            if j and j.status in ("queued", "running"):
                j.status = "canceled"
                return True
        return False

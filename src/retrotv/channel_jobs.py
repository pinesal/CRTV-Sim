"""Durable deletion requests, independent of the busy application database."""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid

from retrotv.db import utc_now


class ChannelDeletionQueue:
    def __init__(self, config):
        self.path = config.storage_root("state") / "channel-deletions.json"
        self.lock = threading.RLock()
        self.error = None
        self.jobs = []
        try:
            if self.path.exists():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(payload, list) or any(not isinstance(job, dict) for job in payload):
                    raise ValueError("Invalid deletion queue")
                required = {"id", "slug", "display_name", "channel_identity", "confirmation_text",
                            "state", "detail", "error", "requested_at", "completed_at", "attempts", "result"}
                if any(not required.issubset(job) or job["state"] not in {
                        "queued", "waiting", "running", "completed", "failed", "cancelled"}
                        or not isinstance(job["channel_identity"], str)
                        or not isinstance(job["attempts"], int) for job in payload):
                    raise ValueError("Invalid deletion job")
                self.jobs = payload
                for job in self.jobs:
                    if job["state"] in {"running", "waiting"}:
                        job.update(state="queued", detail="Resuming confirmed deletion after restart")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Never silently overwrite unreadable pending deletion instructions.
            self.error = f"Cannot read the saved channel deletion queue: {exc}"

    def _write(self):
        if self.error:
            raise ValueError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self.jobs, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def enqueue(self, preview, *, confirmation_text, acknowledge_rescan):
        if confirmation_text != preview["confirmation_text"]:
            raise ValueError(f"Type {preview['confirmation_text']!r} exactly to delete this channel")
        if acknowledge_rescan is not True:
            raise ValueError("Acknowledge that preserved media can make the scanner recreate the channel")
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            for job in self.jobs:
                if job["channel_identity"] == preview["channel_identity"] and job["state"] in {"queued", "waiting", "running"}:
                    return copy.deepcopy(job)
            job = {"id": uuid.uuid4().hex, "slug": preview["slug"],
                   "display_name": preview["display_name"],
                   "channel_identity": preview["channel_identity"],
                   "confirmation_text": confirmation_text, "state": "queued",
                   "requested_at": utc_now(), "completed_at": None,
                   "detail": "Waiting for a safe opportunity to delete the channel",
                   "error": None, "attempts": 0, "result": None}
            self.jobs.append(job)
            try:
                self._write()
            except Exception:
                self.jobs.remove(job)
                raise
            return copy.deepcopy(job)

    def update(self, job_id, **changes):
        with self.lock:
            job = next(job for job in self.jobs if job["id"] == job_id)
            previous = copy.deepcopy(job)
            job.update(changes)
            try:
                self._write()
            except Exception:
                job.clear()
                job.update(previous)
                raise

    def pending(self):
        return [job for job in self.snapshot()["jobs"] if job["state"] in {"queued", "waiting", "running"}]

    def cancel(self, job_id):
        with self.lock:
            job = next((job for job in self.jobs if job["id"] == job_id), None)
            if job is None or job["state"] not in {"queued", "waiting"}:
                raise ValueError("Only a deletion that has not started can be cancelled")
            self.update(job_id, state="cancelled", detail="Deletion cancelled", completed_at=utc_now())
            return {"cancelled": True, "id": job_id}

    def snapshot(self):
        with self.lock:
            return {"jobs": copy.deepcopy(self.jobs), "error": self.error}

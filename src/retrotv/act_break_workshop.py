"""Read-only, bounded video previews and source-frame selection for act breaks."""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from retrotv.breakpoints import BreakpointAnalyzer, resolve_breakpoint_media
from retrotv.asset_paths import require_writable_path
from retrotv.db import connect_database
from retrotv.process_control import ProcessCancelled, run_cancellable


def _canonical(path):
    # Windows can transiently retain the extended-length prefix while another
    # thread creates a parent. Normalize that spelling after resolving aliases.
    value = str(path.resolve())
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class _Cancellation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events if event is not None)


class ActBreakWorkshop:
    def __init__(self, config, *, stop_event=None):
        self.config = config
        self.stop_event = stop_event
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.sessions = {}
        self.pending = OrderedDict()
        self.threads = []
        self.cache_locks = {}
        self.metadata = OrderedDict()
        self.frame_windows = OrderedDict()
        self.clips = OrderedDict()
        self.active_saves = 0

    @property
    def root(self):
        base = _canonical(self.config.storage_root("previews"))
        root = _canonical(base / "act-breaks")
        if not root.is_relative_to(base):
            raise ValueError("Workshop preview directory escapes the configured previews directory")
        return root

    def _source(self, asset_id):
        with closing(connect_database(self.config.database_path)) as connection:
            require_writable_path(connection, self.root)
            row = connection.execute(
                """SELECT a.media_type,m.duration_seconds FROM assets a
                   JOIN asset_media_info m ON m.asset_id=a.id WHERE a.id=?""", (asset_id,),
            ).fetchone()
            if row is None or row["media_type"] not in {"episode", "movie"} or not row["duration_seconds"]:
                raise ValueError("Choose an episode or movie with usable duration metadata")
            path = resolve_breakpoint_media(connection, self.config, asset_id)
            stat = path.stat()
            identity = hashlib.sha256(json.dumps([
                str(path), stat.st_size, stat.st_mtime_ns,
            ]).encode()).hexdigest()
            return {"path": path, "identity": identity, "duration_ms": round(row["duration_seconds"] * 1000),
                    "layout_identity": BreakpointAnalyzer(connection, self.config).recorded_layout(asset_id)['identity']}

    def _session(self, asset_id, session_id):
        session = self.sessions.get(str(session_id))
        if session is None or session["asset_id"] != asset_id:
            raise ValueError("Workshop session expired; reopen the workshop")
        session["touched"] = time.monotonic()
        return session

    def _snapshot(self, session):
        return {key: session.get(key) for key in (
            "session_id", "generation", "state", "error", "result",
        )}

    def request(self, asset_id, payload):
        if self.closed.is_set() or (self.stop_event is not None and self.stop_event.is_set()):
            raise ValueError("CRTV Sim is shutting down")
        action = payload.get("action", "preview")
        if action == "close":
            self.close_session(asset_id, payload.get("session_id"))
            return {"state": "closed"}
        if action not in {"open", "preview", "step", "playback"}:
            raise ValueError("Unknown workshop action")
        source = self._source(asset_id)
        offset = float(payload.get("offset_ms", 0))
        if not math.isfinite(offset) or offset < 0:
            raise ValueError("Choose a finite, nonnegative timestamp")
        with self.condition:
            for key, old in list(self.sessions.items()):
                if time.monotonic() - old["touched"] > 3600:
                    old["cancel"].set()
                    self.pending.pop(key, None)
                    del self.sessions[key]
            session_id = payload.get("session_id")
            if session_id:
                session = self._session(asset_id, session_id)
                if session["identity"] != source["identity"]:
                    raise ValueError("Working video changed; close and reopen the workshop")
            else:
                if len(self.sessions) >= 32:
                    raise ValueError("Too many open workshops; close an existing workshop and retry")
                session_id = secrets.token_hex(16)
                session = {"asset_id": asset_id, "session_id": session_id,
                           "identity": source["identity"], "layout_identity": source["layout_identity"], "generation": 0,
                           "cancel": threading.Event(), "artifacts": {}, "touched": time.monotonic()}
                self.sessions[session_id] = session
            if action == "open":
                session.setdefault("state", "idle")
                return self._snapshot(session)
            direction = 0
            if action == "step":
                direction = int(payload.get("direction", 0))
                if direction not in {-1, 1} or session.get("state") != "ready":
                    raise ValueError("Wait for the selected frame before stepping")
                offset = session["result"]["source_seconds"] * 1000
            session["cancel"].set()
            session.update(cancel=threading.Event(), generation=session["generation"] + 1,
                           state="queued", error=None)
            job = (session_id, session["generation"], {**source, "playback": action == "playback"},
                   offset, direction, session["cancel"])
            self.pending.pop(session_id, None)
            self.pending[session_id] = job
            if not self.threads:
                for index in range(2):
                    worker = threading.Thread(target=self._worker, name=f"act-break-preview-{index}", daemon=True)
                    self.threads.append(worker)
                    worker.start()
            self.condition.notify_all()
            return self._snapshot(session)

    def status(self, asset_id, session_id):
        source = self._source(asset_id)
        with self.condition:
            session = self._session(asset_id, session_id)
            if source["identity"] != session["identity"]:
                session["cancel"].set()
                session.update(state="error", error="Working video changed; close and reopen the workshop")
            self._touch(session["identity"])
            return self._snapshot(session)

    def _touch(self, identity):
        directory = _canonical(self.root / identity)
        if not directory.is_relative_to(self.root):
            raise ValueError("Workshop cache path escapes its directory")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".lease").touch()
        return directory

    def artifact(self, asset_id, session_id, token):
        with self.condition:
            session = self._session(asset_id, session_id)
            path = session["artifacts"].get(token)
            if path is None or not _canonical(path).is_relative_to(self.root) or not path.is_file():
                raise ValueError("Preview expired; reload the workshop")
            self._touch(session["identity"])
            return path

    def validate_selection(self, asset_id, session_id, frame_token, offset_ms, working_identity=None):
        source = self._source(asset_id)
        with self.condition:
            session = self._session(asset_id, session_id)
            result = session.get("result") or {}
            if (source["identity"] != session["identity"]
                    or working_identity is not None and working_identity != session["identity"]):
                raise ValueError("Working video changed; reopen the preview before saving")
            if (session.get("state") != "ready" or result.get("frame_token") != frame_token
                    or result.get("offset_ms") != offset_ms):
                raise ValueError("Selected frame changed; wait for the current preview before saving")

    def close_session(self, asset_id, session_id):
        with self.condition:
            session = self._session(asset_id, session_id)
            session["cancel"].set()
            self.pending.pop(session_id, None)
            del self.sessions[session_id]

    def save(self, connection, asset_id, payload):
        with self.condition:
            if self.closed.is_set() or (self.stop_event is not None and self.stop_event.is_set()):
                raise ValueError("CRTV Sim is shutting down")
            self.active_saves += 1
        try:
            session_id = payload["workshop_session"]
            if payload["action"] == "manual":
                self.validate_selection(asset_id, session_id, payload.get("frame_token"),
                                        int(payload["offset_ms"]), payload.get("working_identity"))
            else:
                source = self._source(asset_id)
                with self.condition:
                    if source["identity"] != self._session(asset_id, session_id)["identity"]:
                        raise ValueError("Working video changed; reopen the workshop")
            current_layout = BreakpointAnalyzer(connection, self.config).recorded_layout(asset_id)['identity']
            expected_layout = payload.get('target_layout_identity')
            with self.condition:
                opened_layout = self._session(asset_id, session_id).get('layout_identity')
            if (expected_layout is not None and expected_layout != current_layout
                    or opened_layout is not None and opened_layout != current_layout):
                raise ValueError('Act-break targets changed; reopen the workshop before saving')
            analyzer = BreakpointAnalyzer(connection, self.config,
                                           cancel_event=_Cancellation(self.closed, self.stop_event))
            if payload['action'] == 'manual':
                result = analyzer.set_manual(asset_id, int(payload['offset_ms']), int(payload.get('target_index', 1)))
            else:
                result = analyzer.reset_manual(asset_id, int(payload['target_index']))
            with self.condition:
                self._session(asset_id, session_id)['layout_identity'] = analyzer.recorded_layout(asset_id)['identity']
            return result
        finally:
            with self.condition:
                self.active_saves -= 1
                self.condition.notify_all()

    def close(self):
        self.closed.set()
        with self.condition:
            for session in self.sessions.values():
                session["cancel"].set()
            self.pending.clear()
            self.condition.notify_all()
        for worker in self.threads:
            worker.join(timeout=10)
            if worker.is_alive():
                raise RuntimeError("Act-break preview worker did not stop")
        with self.condition:
            if not self.condition.wait_for(lambda: self.active_saves == 0, timeout=90):
                raise RuntimeError("Act-break save did not finish safely")

    def _worker(self):
        while not self.closed.is_set():
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed.is_set())
                if self.closed.is_set():
                    return
                session_id, job = self.pending.popitem(last=False)
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                session["state"] = "running"
            _, generation, source, offset, direction, event = job
            cancel = _Cancellation(event, self.closed, self.stop_event)
            try:
                result, artifacts = self._build(source, offset, direction, cancel)
                if self._source(session["asset_id"])["identity"] != source["identity"]:
                    raise ValueError("Working video changed; close and reopen the workshop")
                error = None
            except Exception as exc:
                result, artifacts = None, {}
                error = "Preview cancelled" if isinstance(exc, ProcessCancelled) else str(exc)
            with self.condition:
                current = self.sessions.get(session_id)
                if current is None or current["generation"] != generation:
                    continue
                if error:
                    current.update(state="error", error=error)
                else:
                    prefix = f"/api/assets/{session['asset_id']}/act-break-workshop/{session_id}/"
                    result["clip_url"] = prefix + result["clip_token"] if result.get("clip_token") else None
                    result["frame_url"] = prefix + result["frame_token"]
                    current["artifacts"].update(artifacts)
                    current.update(state="ready", result=result, error=None)

    def _run(self, args, cancel, timeout=120):
        result = run_cancellable([str(arg) for arg in args], timeout, cancel)
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:] or "Preview decoding failed")
        return result

    def _probe(self, path, cancel, frames=False):
        args = [self.config.ffprobe, "-v", "error", "-of", "json"]
        args += (["-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time"]
                 if frames else ["-show_format", "-show_streams"])
        return json.loads(self._run([*args, path], cancel, 60).stdout)

    @staticmethod
    def _times(stderr):
        # Read integer presentation timestamps and their time base. The human
        # readable pts_time field loses precision at large absolute timestamps.
        base = re.search(r"config in time_base:\s*(\d+)/(\d+)", stderr)
        ticks = re.findall(r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:", stderr)
        if base and ticks:
            return [int(value) * int(base[1]) / int(base[2]) for value in ticks]
        return [float(value) for value in re.findall(r"\bpts_time:([-+\d.eE]+)", stderr)]

    def _build(self, source, offset_ms, direction, cancel):
        with self.condition:
            lock = self.cache_locks.setdefault(source["identity"], threading.Lock())
        while not lock.acquire(timeout=.25):
            if cancel.is_set():
                raise ProcessCancelled("Preview superseded")
        try:
            if cancel.is_set():
                raise ProcessCancelled("Preview superseded")
            return self._build_locked(source, offset_ms, direction, cancel)
        finally:
            lock.release()

    def _remember(self, cache, key, value, limit=64):
        # Accessed by workers for different assets, not just a single file lock.
        with self.condition:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > limit:
                cache.popitem(last=False)
        return value

    def _metadata(self, source, cancel):
        with self.condition:
            known = self.metadata.get(source["identity"])
        if known is not None:
            return known
        info = self._probe(source["path"], cancel)
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if stream is None:
            raise ValueError("No video stream is available")
        origin = float(info.get("format", {}).get("start_time") or stream.get("start_time") or 0)
        duration = float(stream.get("duration") or max(0, float(info.get("format", {}).get("duration") or 0) - max(0, origin)))
        if duration <= 0:
            duration = source["duration_ms"] / 1000
        return self._remember(self.metadata, source["identity"], (origin, duration))

    def _cached_clip(self, source, cursor, direction, duration):
        with self.condition:
            candidates = list(self.clips.items())
        for (identity, _), item in reversed(candidates):
            clip, mapping, start, end = item
            times = mapping["source"]
            if (identity == source["identity"] and clip.is_file()
                    and times[0] <= cursor <= times[-1]
                    and (not source.get("playback")
                         or start <= max(0, cursor - 5) and end >= min(duration, cursor + 5))
                    and (direction >= 0 or cursor > times[0])
                    and (direction <= 0 or cursor < times[-1])):
                return item
        return None

    def _build_locked(self, source, offset_ms, direction, cancel):
        directory = self._touch(source["identity"])
        origin, duration = self._metadata(source, cancel)
        cursor = min(offset_ms / 1000, max(0, duration - .001))
        cached = self._cached_clip(source, cursor, direction, duration)
        if cached is not None:
            clip, mapping, start, end = cached
            return self._frame_result(source, cursor, direction, cancel, directory, origin,
                                      duration, mapping["source"], clip, mapping, start, end)
        if source.get("playback"):
            return self._build_clip_locked(source, offset_ms, direction, cancel)
        # Scrubbing must never wait for an encoded minute of video. Decode only
        # a small source window to obtain actual PTS (including VFR), then one JPEG.
        with self.condition:
            windows = list(self.frame_windows.items())
        times = None
        for (identity, _), values in reversed(windows):
            if (identity == source["identity"] and values[0] <= cursor <= values[-1]
                    and (direction >= 0 or cursor > values[0])
                    and (direction <= 0 or cursor < values[-1])):
                times = values
                break
        if times is None:
            start = max(0, cursor - 1)
            # A wider retry handles sparse/low-frame-rate sources without ever
            # estimating frame boundaries from average FPS.
            for span in (2, 10, 60):
                start = max(0, cursor - span / 2)
                decoded = self._run([
                    self.config.ffmpeg, "-hide_banner", "-nostdin", "-threads", "2", "-copyts",
                    "-ss", f"{start:.6f}", "-i", source["path"], "-t", str(span),
                    "-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
                    f"showinfo,setpts=PTS-({origin + start:.9f})/TB",
                    "-fps_mode", "passthrough", "-f", "null", "-",
                ], cancel, 60)
                times = sorted(set(value - origin for value in self._times(decoded.stderr)
                                   if value - origin >= start - .000001
                                   and value - origin < min(duration, start + span) - .000001))
                before = any(value < cursor - .000002 for value in times)
                after = any(value > cursor + .000002 for value in times)
                if times and (before or start == 0) and (after or start + span >= duration):
                    break
            if not times:
                raise ValueError("No frames found around this timestamp")
            self._remember(self.frame_windows, (source["identity"], start), times)
        return self._frame_result(source, cursor, direction, cancel, directory, origin, duration, times)

    def _build_clip_locked(self, source, offset_ms, direction, cancel):
        directory = self._touch(source["identity"])
        origin, duration = self._metadata(source, cancel)
        cursor = min(offset_ms / 1000, max(0, duration - .001))
        start = max(0, min(math.floor((cursor - 8) / 4) * 4, max(0, duration - 20)))
        end = min(duration, start + 20)
        clip = directory / f"play-20-{round(start * 1000000)}.mp4"
        manifest = clip.with_suffix(".json")
        scale = "scale=w='min(640,iw)':h=-2"
        if clip.is_file() and manifest.is_file():
            mapping = json.loads(manifest.read_text(encoding="utf-8"))
        else:
            partial = directory / f"{secrets.token_hex(12)}.partial.mp4"
            try:
                encoded = self._run([
                    self.config.ffmpeg, "-hide_banner", "-nostdin", "-y", "-threads", "2", "-copyts",
                    "-ss", f"{start:.6f}", "-i", source["path"], "-t", f"{end - start:.6f}",
                    "-map", "0:v:0", "-map", "0:a:0?", "-vf",
                    scale + f",showinfo,setpts=PTS-({origin + start:.9f})/TB",
                    "-af", f"asetpts=PTS-({origin + start:.9f})/TB",
                    "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "25",
                    "-pix_fmt", "yuv420p", "-fps_mode", "vfr", "-enc_time_base", "1:1000000",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", partial,
                ], cancel)
                source_times = [value - origin for value in self._times(encoded.stderr)
                                if value - origin >= start - .000001 and value - origin < end - .000001]
                preview_frames = self._probe(partial, cancel, frames=True).get("frames", [])
                preview_times = [float(f["best_effort_timestamp_time"]) for f in preview_frames]
                # showinfo may see lookahead frames beyond the requested output duration.
                if len(source_times) < len(preview_times) or not preview_times:
                    raise ValueError("Could not map preview frames to source timestamps")
                source_times = source_times[:len(preview_times)]
                shift = preview_times[0] - (source_times[0] - start)
                if any(abs(preview - (value - start) - shift) > .002
                       for preview, value in zip(preview_times, source_times)):
                    raise ValueError("Preview encoding changed the frame sequence; no breakpoint was changed")
                mapping = {"source": source_times, "preview": preview_times}
                partial.replace(clip)
                temporary_manifest = directory / f".{secrets.token_hex(12)}.tmp"
                try:
                    temporary_manifest.write_text(json.dumps(mapping), encoding="utf-8")
                    temporary_manifest.replace(manifest)
                finally:
                    temporary_manifest.unlink(missing_ok=True)
            finally:
                partial.unlink(missing_ok=True)
        self._remember(self.clips, (source["identity"], start), (clip, mapping, start, end), limit=32)
        return self._frame_result(source, cursor, direction, cancel, directory, origin,
                                  duration, mapping["source"], clip, mapping, start, end)

    def _frame_result(self, source, cursor, direction, cancel, directory, origin, duration,
                      times, clip=None, mapping=None, start=0, end=0):
        if not times:
            raise ValueError("No frames found around this timestamp")
        if direction > 0:
            index = min(len(times) - 1, bisect.bisect_right(times, cursor + .000002))
        elif direction < 0:
            index = max(0, bisect.bisect_left(times, cursor - .000002) - 1)
        else:
            index = min(range(len(times)), key=lambda i: abs(times[i] - cursor))
        selected = times[index]
        frame = directory / f"frame-{round(selected * 1000000)}.jpg"
        if not frame.is_file():
            temporary = directory / f"{secrets.token_hex(12)}.partial.jpg"
            seek = max(0, selected - .00001)
            try:
                decoded = self._run([
                    self.config.ffmpeg, "-hide_banner", "-nostdin", "-y", "-threads", "2", "-copyts",
                    "-ss", f"{seek:.6f}", "-i", source["path"], "-map", "0:v:0",
                    "-frames:v", "1", "-vf", "scale=w='min(640,iw)':h=-2,showinfo", "-c:v", "mjpeg",
                    "-threads", "1", "-q:v", "2", "-update", "1", temporary,
                ], cancel, 60)
                actual = self._times(decoded.stderr)
                if not actual or abs(actual[0] - origin - selected) > .002:
                    raise ValueError("Could not verify the selected source frame")
                temporary.replace(frame)
            finally:
                temporary.unlink(missing_ok=True)
        clip_token = hashlib.sha256(str(clip).encode()).hexdigest() if clip else None
        frame_token = hashlib.sha256(str(frame).encode()).hexdigest()
        artifacts = {frame_token: frame}
        if clip:
            artifacts[clip_token] = clip
        return {
            "working_identity": source["identity"], "duration_ms": round(duration * 1000),
            "start_ms": round(start * 1000), "end_ms": round(end * 1000),
            "source_seconds": selected, "offset_ms": round(selected * 1000),
            "clip_token": clip_token, "frame_token": frame_token,
            "mapping": [[preview, value] for preview, value in zip(mapping["preview"], times)] if mapping else [],
        }, artifacts

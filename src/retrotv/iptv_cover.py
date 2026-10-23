"""Prepare one opaque interstitial frame without delaying live tuning."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
from contextlib import closing
from uuid import uuid4

from retrotv.db import connect_database


class LiveLoadingCover:
    def __init__(self, config):
        self.config = config
        self.condition = threading.Condition()
        self.pending = None
        self.cancel_job = threading.Event()
        self.closed = False
        self.revision = 0
        self.channel = None
        self.frame = None
        self.thread = threading.Thread(target=self._run, name='iptv-loading-cover', daemon=True)
        self.thread.start()

    def request(self, channel, when):
        with self.condition:
            if self.closed:
                return
            self.revision += 1
            self.cancel_job.set()
            if self.channel != channel:
                self.frame = None
            self.channel = channel
            self.pending = (self.revision, channel, when)
            self.condition.notify()

    def snapshot(self):
        with self.condition:
            return self.frame

    def cancel(self):
        with self.condition:
            self.revision += 1
            self.pending = None
            self.frame = None
            self.channel = None
            self.cancel_job.set()

    def close(self):
        with self.condition:
            self.closed = True
            self.cancel_job.set()
            self.condition.notify()
        self.thread.join(timeout=2)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                revision, channel, when = self.pending
                self.pending = None
                cancel = self.cancel_job = threading.Event()
            try:
                frame = self._prepare(channel, when, cancel)
            except Exception:
                # An absent/unreadable graphic must never prevent live TV.
                frame = None
            with self.condition:
                if revision == self.revision and not cancel.is_set():
                    self.frame = frame

    def _prepare(self, channel, when, cancel):
        from retrotv.graphics import GraphicsService
        with closing(connect_database(self.config.database_path)) as connection:
            graphic = GraphicsService(connection, self.config).select_asset(channel, 'interstitials', when)
            if graphic is None or cancel.is_set():
                return None
            source = self.config.storage_root('media') / Path(graphic['relative_path'])
            stat = source.stat()
            identity = hashlib.sha256(f'{source}|{stat.st_size}|{stat.st_mtime_ns}|cover-v1'.encode()).hexdigest()
        root = self.config.storage_root('state') / 'generated' / 'iptv-covers'
        root.mkdir(parents=True, exist_ok=True)
        target = root / f'{identity}.bgra'
        if target.is_file() and target.stat().st_size == 640 * 480 * 4:
            target.touch()
            return target
        partial = root / f'{identity}-{uuid4().hex}.partial.bgra'
        process = None
        try:
            if cancel.is_set():
                return None
            process = subprocess.Popen([
                self.config.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                '-i', os.fspath(source), '-frames:v', '1', '-an',
                '-vf', 'scale=640:480:force_original_aspect_ratio=increase,crop=640:480,format=rgb24,format=bgra',
                '-pix_fmt', 'bgra', '-f', 'rawvideo', os.fspath(partial)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 5
            while process.poll() is None:
                if cancel.wait(.05) or time.monotonic() >= deadline:
                    return None
            if cancel.is_set() or process.returncode or partial.stat().st_size != 640 * 480 * 4:
                return None
            os.replace(partial, target)
            return target
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            partial.unlink(missing_ok=True)

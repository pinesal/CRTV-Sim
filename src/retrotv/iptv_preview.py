"""One explicit, independently cancellable live preview per application."""
from __future__ import annotations

import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

from retrotv.db import connect_database
from retrotv.iptv import IPTVService


class IPTVPreview:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.player = None
        self.state = {'state':'idle', 'detail':None}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, feed_id):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Close the existing IPTV test before opening another')
            self.cancel.clear()
            self.state = {'state':'connecting','feed_id':feed_id,'detail':'Opening a separate player on this PC'}
            self.thread = threading.Thread(target=self._run,args=(feed_id,),name='iptv-preview',daemon=True)
            self.thread.start()
        return self.snapshot()

    def _run(self, feed_id):
        from retrotv.playback import PlaybackTarget, MpvIpcPlayer, PlaybackError, create_player
        try:
            with closing(connect_database(self.config.database_path)) as connection:
                service = IPTVService(connection,self.config)
                url = service.playable_url(feed_id)
                player = create_player(self.config,'mpv')
                if not isinstance(player,MpvIpcPlayer):
                    raise ValueError('IPTV preview requires mpv')
                self.player = player
                player.fullscreen = False
                now = datetime.now().astimezone()
                target = PlaybackTarget(-4,None,'IPTV test','iptv_live',now,now+timedelta(seconds=30),None,
                    'iptv_live',0,30,stream_url=url,feed_id=feed_id)
                if self.cancel.is_set():
                    return
                player.load(target)
                started = time.monotonic()
                confirmed = False
                first_position = None
                while not self.cancel.wait(.2) and time.monotonic()-started < 30:
                    if player.live_broken():
                        raise ValueError('Feed ended or could not be decoded')
                    if player.live_ready():
                        try:
                            tracks = player._command(['get_property','track-list']) or []
                            position = player.playback_position()
                        except PlaybackError:
                            tracks,position = [],None
                        if first_position is None:
                            first_position = position
                        if any(t.get('type')=='video' for t in tracks) and position is not None and position != first_position:
                            audio = any(t.get('type')=='audio' for t in tracks)
                            confirmed = True
                            with self.lock:
                                self.state.update(state='playing',detail='Video decoded; '+('audio present' if audio else 'no audio track'))
                    if not confirmed and time.monotonic()-started >= 15:
                        raise ValueError('No decoded video within 15 seconds; check URL and network access')
                with self.lock:
                    self.state.update(state='passed' if confirmed else 'cancelled')
        except Exception:
            # Never expose stream tokens or player command arguments in errors.
            with self.lock:
                self.state.update(state='failed',detail='IPTV test failed or timed out. Check the feed URL, playlist selection, network access and supported format.')
        finally:
            if self.player:
                self.player.stop()
                self.player = None

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=17)
        return self.snapshot()

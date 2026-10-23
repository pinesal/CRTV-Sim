"""Live inputs, intentionally separate from local assets and Internet Media."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, UTC
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from retrotv.classification import slugify
from retrotv.db import utc_now


def stream_url(value: str) -> str:
    value = str(value).strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(c) < 32 for c in value) or len(value) > 8192):
        raise ValueError('Enter a direct HTTP/HTTPS URL without embedded login credentials')
    return value


class SafeRedirect(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        stream_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def inspect_url(url: str, *, stop_event=None) -> dict:
    """Read a bounded catalogue, not an unbounded live response body."""
    url = stream_url(url)
    opener = build_opener(SafeRedirect())
    if stop_event is not None and stop_event.is_set():
        raise ValueError('IPTV request cancelled')
    with opener.open(Request(url, headers={'User-Agent': 'CRTV-Sim IPTV'}), timeout=5) as response:
        final = stream_url(response.url)
        content_type = response.headers.get('Content-Type', '').lower()
        # MPEG-TS and other binary responses need only a small signature read.
        read = getattr(response,'read1',response.read)
        deadline = time.monotonic()+10
        first = read(1024)
        while len(first)<8 and first and time.monotonic()<deadline:
            chunk = read(8-len(first))
            if not chunk:
                break
            first += chunk
        text_like = first.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U')
        if not text_like and ('mpegurl' not in content_type and not final.split('?')[0].lower().endswith(('.m3u', '.m3u8'))):
            return {'url': url, 'kind': 'direct', 'feeds': [
                {'key': 'direct', 'name': urlsplit(final).hostname, 'url': final}]}
        data = bytearray(first)
        while True:
            if time.monotonic() >= deadline or (stop_event is not None and stop_event.is_set()):
                raise ValueError('Playlist download timed out')
            chunk = read(min(65536,2_000_001-len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise ValueError('Playlist exceeds the 2 MB limit')
    text = data.decode('utf-8-sig')
    if not text.lstrip().startswith('#EXTM3U'):
        raise ValueError('Expected an M3U playlist or direct stream')
    if re.search(r'^#EXT-X-', text, re.M):
        # Includes both master manifests and media/segment manifests.
        return {'url': url, 'kind': 'direct', 'feeds': [
            {'key': 'direct', 'name': urlsplit(final).hostname, 'url': final}]}
    feeds = []
    skipped = []
    entry_count = 0
    name, channel_id = '', ''
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            name = line.partition(',')[2].strip()[:250]
            match = re.search(r'\btvg-id="([^"]*)"', line)
            channel_id = match[1] if match else ''
        elif line and not line.startswith('#'):
            entry_count += 1
            if entry_count > 10000:
                raise ValueError('Playlist exceeds 10,000 feeds')
            try:
                target = stream_url(urljoin(final, line))
            except ValueError:
                # A mixed channel list may contain unsupported protocols or
                # login-bearing entries. They do not invalidate other feeds.
                # Never return rejected URLs, which may contain credentials.
                skipped.append({'line':line_number,'name':name,
                                'reason':'Unsupported or invalid URL; requires HTTP/HTTPS without embedded login credentials'})
                name, channel_id = '', ''
                continue
            feeds.append({'key': 'id:' + channel_id if channel_id else 'url:' + target,
                          'name': name or target, 'url': target})
            name, channel_id = '', ''
    if not feeds:
        raise ValueError(f'Playlist contains no supported HTTP/HTTPS feeds ({len(skipped)} unsupported or invalid entries)')
    keys = Counter(f['key'] for f in feeds)
    for feed in feeds:
        feed['ambiguous'] = keys[feed['key']] != 1
    return {'url': url, 'kind': 'playlist', 'feeds': feeds, 'skipped_entries':skipped}


def presentation_mode(value='preserve'):
    if value not in {'preserve', 'fill_4_3', 'stretch_4_3'}:
        raise ValueError('Choose Preserve original, Fill 4:3 or Stretch to 4:3')
    return value


def presentation_filter(mode):
    mode = presentation_mode(mode)
    if mode == 'fill_4_3':
        # Work in display dimensions, including anamorphic pixel aspect ratios.
        # libavfilter reconfigures this graph when the input format changes.
        return "lavfi=[crop=w='min(iw,ih*4/3/sar)':h='min(ih,iw*sar*3/4)',setsar=1,setdar=4/3]"
    if mode == 'stretch_4_3':
        return 'lavfi=[setsar=1,setdar=4/3]'
    return None


def select_feed(payload):
    """Resolve an unsaved selection without creating sources, channels or folders."""
    mode = presentation_mode(payload.get('presentation','preserve'))
    url = stream_url(payload.get('url', ''))
    direct = bool(payload.get('direct'))
    catalog = ({'kind':'direct', 'feeds':[{'key':'direct',
                'name':urlsplit(url).hostname, 'url':url}]} if direct else inspect_url(url))
    key = payload.get('selection_key') or 'direct'
    matches = [f for f in catalog['feeds'] if f['key'] == key]
    if len(matches) != 1:
        raise ValueError('Select one unambiguous feed; refresh the playlist if it changed')
    return {'url':url, 'direct':direct, 'selection_key':key, 'kind':catalog['kind'],
            'feed':dict(matches[0]), 'presentation':mode}


def selection_matches(selection, payload):
    return (selection['url'] == stream_url(payload.get('url', ''))
            and selection['direct'] == bool(payload.get('direct'))
            and selection.get('presentation','preserve') == presentation_mode(payload.get('presentation','preserve'))
            and selection['selection_key'] == (payload.get('selection_key') or 'direct'))


class IPTVService:
    def __init__(self, connection, config):
        self.connection, self.config = connection, config

    def _feed(self, feed_id):
        row = self.connection.execute('SELECT * FROM iptv_feeds WHERE id=?', (feed_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown IPTV feed')
        return row

    def register(self, payload, *, tested_selection=None):
        # This proof comes from the application's preview, never from request JSON.
        if payload.get('new_channel') and tested_selection is None:
            raise ValueError('Test this feed successfully before creating its channel')
        if tested_selection is not None and not selection_matches(tested_selection, payload):
            raise ValueError('The selected feed changed; test it again before saving')
        selection = tested_selection or select_feed(payload)
        url, key, item = selection['url'], selection['selection_key'], selection['feed']
        name = str(payload.get('name') or item['name']).strip()[:250]
        mode = presentation_mode(payload.get('presentation','preserve'))
        try:
            self.connection.execute('''INSERT INTO iptv_sources(url,kind,last_refresh,catalog_json)
                VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET kind=excluded.kind,
                last_refresh=excluded.last_refresh,catalog_json=excluded.catalog_json,refresh_error=NULL''',
                (url, selection['kind'], utc_now(), json.dumps([item])))
            source = self.connection.execute('SELECT id FROM iptv_sources WHERE url=?', (url,)).fetchone()[0]
            self.connection.execute('''INSERT INTO iptv_feeds(source_id,selection_key,name,stream_url,presentation)
                VALUES (?,?,?,?,?) ON CONFLICT(source_id,selection_key) DO UPDATE SET enabled=1,
                name=excluded.name,stream_url=excluded.stream_url,availability='unknown',detail=NULL,
                presentation=COALESCE(?,iptv_feeds.presentation)''',
                (source, key, name, item['url'],mode,mode if 'presentation' in payload else None))
            feed_id = self.connection.execute('SELECT id FROM iptv_feeds WHERE source_id=? AND selection_key=?', (source,key)).fetchone()[0]
            if payload.get('new_channel'):
                payload = dict(payload)
                display = str(payload['new_channel']).strip()
                slug = slugify(display)
                from retrotv.synthetic import weather_channel_definition
                if slug in {'guide', weather_channel_definition(self.config).slug}:
                    raise ValueError('Guide and Weather cannot receive IPTV')
                if self.connection.execute('SELECT 1 FROM channels WHERE slug=?', (slug,)).fetchone():
                    raise ValueError('That channel already exists; select it instead')
                used = {r[0] for r in self.connection.execute('SELECT channel_number FROM channels')}
                used.add(int(self.config.data.get('guide', {}).get('channel_number', 2)))
                used.update(int(n) for n in self.config.data.get('guide', {}).get('channel_numbers', {}).values())
                number = int(payload.get('channel_number') or self.config.data.get('library', {}).get('auto_channel_number_start', 10))
                while number in used and not payload.get('channel_number'):
                    number += 1
                if not 1 <= number <= 999 or number in used or not slug:
                    raise ValueError('Choose an unused channel number between 1 and 999')
                self.connection.execute('INSERT INTO channels(slug,display_name,channel_number) VALUES (?,?,?)', (slug,display,number))
                payload['assignments'] = [{'channel':slug, 'scope':payload.get('scope','all-day')}]
            self._assign(feed_id, payload.get('assignments', []))
            if payload.get('new_channel'):
                from retrotv.classification import resolve_channel_root
                from retrotv.media_folders import ensure_library_scaffold
                from retrotv.scheduling import ScheduleBuilder
                media_root = self.config.storage_root('media')
                if media_root.is_dir():
                    ensure_library_scaffold(media_root,
                        channels=[resolve_channel_root(media_root,slugify(str(payload['new_channel'])))],
                        dayparts={p.slug for p in ScheduleBuilder(self.connection,self.config).dayparts},
                        holidays=set(self.config.data.get('seasonal',{}).get('holidays',{})))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {'id': feed_id}

    def _assign(self, feed_id, assignments):
        from retrotv.synthetic import weather_channel_definition
        from retrotv.scheduling import ScheduleBuilder
        scheduler = ScheduleBuilder(self.connection, self.config)
        scopes = {'daytime', 'all-day', *(p.slug for p in scheduler.dayparts)}
        values = set()
        for item in assignments:
            channel = slugify(str(item.get('channel','')))
            scope = str(item.get('scope') or 'all-day')
            weekly = str(item.get('weekly_slug') or '')
            row = self.connection.execute('''SELECT c.id,p.playback_mode FROM channels c
                LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id WHERE c.slug=?''', (channel,)).fetchone()
            if (channel in {'guide', weather_channel_definition(self.config).slug} or row is None
                    or row['playback_mode'] == 'watch_at_your_pace'):
                raise ValueError('IPTV requires an ordinary scheduled channel; Guide, Weather and At Your Pace are excluded')
            if weekly:
                if not any(w.slug == weekly and w.channel_slug == channel for w in scheduler.rules.windows):
                    raise ValueError('Choose an existing weekly special on this channel')
                scope = 'all-day'
            if scope not in scopes:
                raise ValueError('Choose a configured daypart, daytime or all-day')
            values.add((channel,scope,weekly,str(item.get('display_name','')).strip()[:250]))
        existing = {tuple(r) for r in self.connection.execute('''SELECT channel_slug,scope,weekly_slug,display_name
            FROM iptv_assignments WHERE feed_id=?''', (feed_id,))}
        if values != existing:
            self.connection.execute('DELETE FROM iptv_assignments WHERE feed_id=?', (feed_id,))
            self.connection.executemany('''INSERT INTO iptv_assignments
                (feed_id,channel_slug,scope,weekly_slug,display_name) VALUES (?,?,?,?,?)''',
                [(feed_id,*v) for v in sorted(values)])
        for channel, *_ in values:
            self.connection.execute('UPDATE channels SET enabled=1 WHERE slug=?', (channel,))

    def update(self, feed_id, payload):
        self._feed(feed_id)
        try:
            if 'presentation' in payload:
                self.connection.execute('UPDATE iptv_feeds SET presentation=? WHERE id=?',
                    (presentation_mode(payload['presentation']),feed_id))
            if 'assignments' in payload:
                self._assign(feed_id, payload['assignments'])
            if 'enabled' in payload:
                self.connection.execute('UPDATE iptv_feeds SET enabled=? WHERE id=?', (int(bool(payload['enabled'])),feed_id))
            if 'name' in payload:
                name = str(payload['name']).strip()
                if not name or len(name) > 250:
                    raise ValueError('Enter a feed name of at most 250 characters')
                self.connection.execute('UPDATE iptv_feeds SET name=? WHERE id=?', (name,feed_id))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {'id': feed_id}

    def remove(self, feed_id):
        self._feed(feed_id)
        self.connection.execute('DELETE FROM iptv_feeds WHERE id=?', (feed_id,))
        self.connection.commit()
        return {'removed': feed_id}

    def refresh(self, feed_id, *, stop_event=None):
        feed = self._feed(feed_id)
        source = self.connection.execute('SELECT * FROM iptv_sources WHERE id=?', (feed['source_id'],)).fetchone()
        try:
            catalog = inspect_url(source['url'],stop_event=stop_event)
        except Exception:
            if stop_event is not None and stop_event.is_set():
                return {'cancelled':True}
            # Do not persist exception text: urllib errors can contain private query strings.
            self.connection.execute('UPDATE iptv_sources SET last_refresh=?,refresh_error=? WHERE id=?',
                                    (utc_now(),'Playlist connection failed or timed out',source['id']))
            self.connection.commit()
            return {'error': 'Playlist connection failed or timed out'}
        if stop_event is not None and stop_event.is_set():
            return {'cancelled':True}
        for selected in self.connection.execute('SELECT * FROM iptv_feeds WHERE source_id=?', (source['id'],)).fetchall():
            matches = [f for f in catalog['feeds'] if f['key'] == selected['selection_key']]
            detail = None if len(matches) == 1 else ('Stream not found' if not matches else 'Ambiguous playlist selection; reselect feed')
            self.connection.execute('UPDATE iptv_feeds SET stream_url=?,availability=?,detail=?,checked_at=? WHERE id=?',
                (matches[0]['url'] if len(matches)==1 else selected['stream_url'],
                 'available' if detail is None else 'missing', detail,utc_now(),selected['id']))
        self.connection.execute('UPDATE iptv_sources SET catalog_json=?,last_refresh=?,refresh_error=NULL WHERE id=?',
                                (json.dumps(catalog['feeds']),utc_now(),source['id']))
        self.connection.commit()
        return {'refreshed': feed_id}

    def playable_url(self, feed_id):
        feed = self._feed(feed_id)
        if not feed['enabled']:
            raise ValueError('IPTV feed is disabled')
        if feed['availability'] == 'missing':
            raise ValueError(feed['detail'] or 'Stream not found')
        return stream_url(feed['stream_url'])

    def status(self, feed_id, state, detail=None):
        self.connection.execute('UPDATE iptv_feeds SET availability=?,detail=?,checked_at=? WHERE id=?',
                                (state,detail,utc_now(),feed_id))
        self.connection.commit()

    def overview(self, *, include_conflicts=True, cancel_event=None):
        from retrotv.scheduling import ScheduleBuilder
        from retrotv.synthetic import weather_channel_definition
        scheduler = ScheduleBuilder(self.connection,self.config)
        feeds = []
        for row in self.connection.execute('''SELECT f.*,s.url AS source_url,s.kind,s.last_refresh,s.refresh_error
            FROM iptv_feeds f JOIN iptv_sources s ON s.id=f.source_id ORDER BY f.name'''):
            feed = dict(row)
            feed['assignments'] = [dict(a) for a in self.connection.execute('SELECT * FROM iptv_assignments WHERE feed_id=?', (row['id'],))]
            feeds.append(feed)
        channels = [dict(r) for r in self.connection.execute('''SELECT c.slug,c.display_name FROM channels c
            LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id
            WHERE COALESCE(p.playback_mode,'scheduled')='scheduled' AND c.slug NOT IN ('guide',?) ORDER BY c.channel_number''',
            (weather_channel_definition(self.config).slug,))]
        result = {'feeds':feeds,'channels':channels,'scopes':['all-day','daytime',*(p.slug for p in scheduler.dayparts)],
                'weekly':[{'slug':w.slug,'channel':w.channel_slug,'name':w.guide_label} for w in scheduler.rules.windows],
                'conflicts':[]}
        if include_conflicts:
            status = self.conflict_status(cancel_event=cancel_event)
            result.update(conflicts=status['items'], conflicts_complete=status['complete'],
                          conflicts_detail=status['detail'])
        return result

    def conflict_status(self, *, timeout_seconds=2.0, cancel_event=None):
        """Bound diagnostics separately from the fast assignment catalog.

        Begin with current/future generations using the existing indexes. A
        scan of every historical schedule entry can take minutes on a large DB.
        """
        from retrotv.scheduling import resolve_timezone
        settings = self.config.data.get('scheduling', {})
        zone = resolve_timezone(str(settings.get('timezone', self.config.data.get('application', {}).get('timezone', 'UTC'))))
        since = (datetime.now(zone).date() - timedelta(days=1)).isoformat()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        def interrupted():
            return time.monotonic() >= deadline or bool(cancel_event and cancel_event.is_set())
        unavailable = {'items': [], 'complete': False,
            'detail': 'Conflict checking could not finish in time. Channel and daypart options remain available; choose Refresh status to retry.'}
        if interrupted():
            return unavailable
        self.connection.set_progress_handler(interrupted, 1000)
        try:
            rows = self.connection.execute('''
                SELECT c.slug,se.daypart_slug,se.starts_at,se.ends_at,se.explanation_json
                FROM channels c
                CROSS JOIN schedule_generations g INDEXED BY idx_schedule_one_active
                CROSS JOIN schedule_entries se INDEXED BY idx_schedule_entries_generation
                WHERE g.channel_id=c.id AND g.status='active' AND g.schedule_date>=?
                  AND se.generation_id=g.id AND se.entry_type='iptv_conflict'
                  AND julianday(se.ends_at)>julianday(?)
                ORDER BY se.starts_at LIMIT 100
            ''', (since, utc_now())).fetchall()
            return {'items': [dict(row) for row in rows], 'complete': True, 'detail': None}
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', 0) == sqlite3.SQLITE_INTERRUPT:
                return unavailable
            raise
        finally:
            self.connection.set_progress_handler(None, 0)

    def fingerprint(self, channel):
        return [list(r) for r in self.connection.execute('''SELECT a.id,a.feed_id,a.scope,a.weekly_slug,a.display_name,
            f.name,f.enabled FROM iptv_assignments a JOIN iptv_feeds f ON f.id=a.feed_id
            WHERE a.channel_slug=? ORDER BY a.id''', (channel,))]

    def owner(self, channel, daypart, context):
        rows = self.connection.execute('''SELECT a.*,f.name FROM iptv_assignments a
            JOIN iptv_feeds f ON f.id=a.feed_id WHERE a.channel_slug=? AND f.enabled=1''', (channel,)).fetchall()
        matches = []
        for row in rows:
            if context.owner:
                if row['weekly_slug'] == context.owner.slug:
                    matches.append((3,row))
            elif not row['weekly_slug']:
                rank = (3 if row['scope']==daypart else
                        2 if row['scope']=='daytime' and daypart in {'morning','afternoon','evening'} else
                        1 if row['scope']=='all-day' else 0)
                if rank:
                    matches.append((rank,row))
        if not matches:
            return None
        rank = max(v[0] for v in matches)
        selected = [dict(r) for n,r in matches if n==rank]
        return {'assignments':selected,'feed_id':selected[0]['feed_id'],
                'title':selected[0]['display_name'] or selected[0]['name'],
                'conflict':len({r['feed_id'] for r in selected})>1}

    def conflicts(self, channel, daypart, context):
        from retrotv.scheduling import PLACEMENT_CTE
        rows = self.connection.execute(PLACEMENT_CTE + '''SELECT DISTINCT p.asset_id,p.placement_relative_path,p.collection_type,
            p.collection_slug,p.holiday_slug,p.holiday_weekly_slug FROM placements p
            LEFT JOIN block_profiles bp ON bp.asset_id=p.asset_id
            WHERE p.placement_channel=? AND p.eligibility_channel IN (?, '*')
            AND (p.eligibility_daypart=? OR p.eligibility_daypart='all-day' OR p.eligibility_daypart IS NULL)
            AND p.media_type IN ('episode','short','movie','complete_block')
            AND (p.media_type!='complete_block' OR COALESCE(bp.enabled,1)=1)''', (channel,channel,daypart)).fetchall()
        active = {h.slug for h in context.holidays if h.mode in {'boost','replace'}}
        result = []
        for r in rows:
            if context.owner:
                eligible = ((r['collection_type']=='weekly' and r['collection_slug']==context.owner.collection_slug)
                    or (r['holiday_slug'] in active and r['holiday_weekly_slug']==context.owner.collection_slug))
            else:
                eligible = r['collection_type'] is None or (r['holiday_slug'] in active and not r['holiday_weekly_slug'])
            if eligible:
                result.append({'asset_id':r['asset_id'],'placement':r['placement_relative_path']})
        return result[:20]

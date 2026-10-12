"""Read-only series import using the ordinary asset catalog and eligibility."""
from __future__ import annotations

import os
import stat as file_stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from retrotv.asset_paths import contained_path
from retrotv.classification import _expanded_dayparts, slugify
from retrotv.db import utc_now
from retrotv.classification import resolve_channel_root
from retrotv.media_folders import ensure_library_scaffold
from retrotv.probe import fingerprint_file
from retrotv.scanner import LibraryScanner, FFmpegInspector
from retrotv.synthetic import weather_channel_definition


def _directory_alias(path):
    """Detect Windows junctions on Python 3.11 as well as ordinary symlinks."""
    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    return path.is_symlink() or bool(attributes & getattr(file_stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def require_scheduled_channel(connection, channel_slug):
    if connection.execute(
        'SELECT 1 FROM external_assignments e JOIN storage_roots r ON r.id=e.root_id WHERE e.channel_slug=? AND e.enabled=1 AND r.enabled=1 LIMIT 1',
        (channel_slug,),
    ).fetchone():
        raise ValueError('Disconnect external assignments before switching to At Your Pace; external playlists are planned for Phase 1B')


class ExternalMediaService:
    def __init__(self, connection, config, *, inspector=None):
        self.connection = connection
        self.config = config
        self.inspector = inspector or FFmpegInspector(config)
        self.dayparts = LibraryScanner(connection, config).known_dayparts

    def _source(self, root_id):
        row = self.connection.execute(
            'SELECT r.*,s.* FROM storage_roots r JOIN external_sources s ON s.root_id=r.id WHERE r.id=?',
            (root_id,),
        ).fetchone()
        if row is None:
            raise ValueError('Unknown external source')
        return row

    def _validate_root(self, raw):
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise ValueError('Enter an absolute folder path accessible to this computer')
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Source must be a series directory')
        protected = [self.config.storage_root(name).resolve() for name in self.config.data.get('storage_roots', {})]
        protected.append(self.config.database_path.parent.resolve())
        for section, key, default in (
            ('announcements', 'output_dir', './data/generated/announcements'),
            ('graphics', 'output_dir', './data/generated/graphics'),
            ('guide', 'output', './data/generated/guide.mp4'),
        ):
            output = Path(str(self.config.data.get(section, {}).get(key, default)))
            if not output.is_absolute():
                output = self.config.project_root / output
            protected.append((output.parent if key == 'output' else output).resolve())
        for other in protected:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError('External sources must not overlap managed media or application data')
        for row in self.connection.execute("SELECT id,absolute_path FROM storage_roots WHERE source_kind='external'"):
            other = Path(row['absolute_path'])
            if path == other:
                return path, int(row['id'])
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError('External source folders must not overlap')
        return path, None

    def _assignments(self, root_id, assignments):
        if not isinstance(assignments, list) or not assignments:
            raise ValueError('Choose at least one scheduled channel and daypart')
        validated = set()
        weather_slug = weather_channel_definition(self.config).slug
        for item in assignments:
            channel = slugify(str(item.get('channel') or ''))
            scope = str(item.get('scope') or '')
            if channel == weather_slug:
                raise ValueError('Weather is a synthetic channel and cannot receive scheduled programming')
            row = self.connection.execute(
                "SELECT c.id,p.playback_mode FROM channels c LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id WHERE c.slug=?",
                (channel,),
            ).fetchone()
            if row is None or row['playback_mode'] == 'watch_at_your_pace':
                raise ValueError('External sources require a scheduled channel; At Your Pace is planned for Phase 1B')
            if scope not in self.dayparts:
                raise ValueError('Choose a configured daypart, daytime, or all-day')
            validated.add((channel, scope))
        self.connection.execute('UPDATE external_assignments SET enabled=0 WHERE root_id=?', (root_id,))
        for channel, scope in sorted(validated):
            self.connection.execute(
                "INSERT INTO external_assignments(root_id,channel_slug,scope) VALUES (?,?,?) ON CONFLICT(root_id,channel_slug,scope) DO UPDATE SET enabled=1",
                (root_id, channel, scope),
            )
            self.connection.execute('UPDATE channels SET enabled=1 WHERE slug=?', (channel,))

    def register(self, payload):
        title = str(payload.get('series_title') or '').strip()
        if not title or len(title) > 250:
            raise ValueError('Enter a series title of at most 250 characters')
        path, existing = self._validate_root(payload.get('path') or '')
        if existing is not None and self._source(existing)['enabled']:
            raise ValueError('This folder is already registered; edit its assignments or rescan it')
        assignments = payload.get('assignments')
        try:
            new_channel = str(payload.get('new_channel') or '').strip()
            new_channel_slug = None
            if new_channel:
                channel = slugify(new_channel)
                if channel == weather_channel_definition(self.config).slug:
                    raise ValueError('Weather is a synthetic channel and cannot be created as a programming channel')
                if self.connection.execute('SELECT 1 FROM channels WHERE slug=?', (channel,)).fetchone():
                    raise ValueError('That channel already exists; select it instead')
                number = payload.get('channel_number')
                used = {r[0] for r in self.connection.execute('SELECT channel_number FROM channels')}
                guide = self.config.data.get('guide', {})
                used.add(int(guide.get('channel_number', 2)))
                used.update(int(value) for value in guide.get('channel_numbers', {}).values())
                if number in (None, ''):
                    number = int(self.config.data.get('library', {}).get('auto_channel_number_start', 10))
                    while number in used:
                        number += 1
                else:
                    number = int(number)
                if number <= 0 or number in used:
                    raise ValueError('Choose an unused positive channel number')
                # Use the existing channel representation, without its Internet Media workflow.
                self.connection.execute('INSERT INTO channels(slug,display_name,channel_number,enabled) VALUES (?,?,?,1)', (channel,new_channel,number))
                assignments = [{'channel': channel, 'scope': str(payload.get('scope') or 'all-day')}]
                new_channel_slug = channel
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO storage_roots(logical_name,source_kind,absolute_path) VALUES (?,'external',?)",
                    ('external-' + uuid4().hex, str(path)),
                )
                existing = int(cursor.lastrowid)
                self.connection.execute('INSERT INTO external_sources(root_id,series_title) VALUES (?,?)', (existing,title))
            self._assignments(existing, assignments)
            self.connection.execute('UPDATE storage_roots SET enabled=1 WHERE id=?', (existing,))
            self.connection.execute('UPDATE external_sources SET series_title=?,scan_status=\'pending\' WHERE root_id=?', (title,existing))
            self.connection.execute('UPDATE assets SET series_title=? WHERE source_root_id=?', (title,existing))
            self._eligibility(existing)
            if new_channel_slug:
                media_root = self.config.storage_root('media').resolve()
                channel_root = resolve_channel_root(media_root, new_channel_slug)
                dayparts = self.dayparts
                holidays = set(self.config.data.get('seasonal', {}).get('holidays', {}).keys())
                holidays.update(self.config.data.get('graphics', {}).get('holidays', []))
                ensure_library_scaffold(
                    media_root,
                    channels=[channel_root],
                    dayparts=dayparts,
                    holidays=holidays,
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {'root_id': existing}

    def update(self, root_id, payload):
        self._source(root_id)
        try:
            if 'assignments' in payload:
                self._assignments(root_id, payload['assignments'])
            if 'enabled' in payload:
                if payload['enabled']:
                    assignments = [{'channel': r['channel_slug'], 'scope': r['scope']} for r in self.connection.execute(
                        'SELECT * FROM external_assignments WHERE root_id=? AND enabled=1', (root_id,)
                    )]
                    self._assignments(root_id, assignments)
                self.connection.execute('UPDATE storage_roots SET enabled=? WHERE id=?', (int(bool(payload['enabled'])),root_id))
            if 'series_title' in payload:
                title = str(payload['series_title']).strip()
                if not title or len(title) > 250:
                    raise ValueError('Enter a series title of at most 250 characters')
                self.connection.execute('UPDATE external_sources SET series_title=? WHERE root_id=?', (title,root_id))
                self.connection.execute('UPDATE assets SET series_title=? WHERE source_root_id=?', (title,root_id))
            self._eligibility(root_id)
            self.connection.execute("UPDATE external_sources SET scan_status='pending' WHERE root_id=? AND root_id IN (SELECT id FROM storage_roots WHERE enabled=1)", (root_id,))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {'root_id': root_id}

    def _eligibility(self, root_id):
        self.connection.execute('DELETE FROM asset_eligibility WHERE asset_id IN (SELECT id FROM assets WHERE source_root_id=?)', (root_id,))
        for assignment in self.connection.execute('SELECT * FROM external_assignments WHERE root_id=? AND enabled=1', (root_id,)).fetchall():
            for daypart in _expanded_dayparts(assignment['scope'], self.dayparts):
                self.connection.execute(
                    "INSERT INTO asset_eligibility(asset_id,channel_slug,daypart_slug,eligibility_source) SELECT id,?,?,? FROM assets WHERE source_root_id=?",
                    (assignment['channel_slug'],daypart,f"external:{assignment['id']}",root_id),
                )

    def overview(self):
        result = []
        weather_slug = weather_channel_definition(self.config).slug
        for row in self.connection.execute('SELECT r.*,s.* FROM storage_roots r JOIN external_sources s ON s.root_id=r.id ORDER BY s.series_title').fetchall():
            item = dict(row)
            assignments = [dict(a) for a in self.connection.execute('SELECT * FROM external_assignments WHERE root_id=? AND enabled=1', (row['id'],))]
            item['unsupported_assignments'] = [a for a in assignments if a['channel_slug'] == weather_slug]
            item['assignments'] = [a for a in assignments if a['channel_slug'] != weather_slug]
            item['episode_count'] = self.connection.execute("SELECT COUNT(*) FROM assets WHERE source_root_id=? AND integrity_status!='missing'", (row['id'],)).fetchone()[0]
            result.append(item)
        channels = [dict(r) for r in self.connection.execute("SELECT c.slug,c.display_name,c.channel_number FROM channels c LEFT JOIN channel_playback_preferences p ON p.channel_id=c.id WHERE COALESCE(p.playback_mode,'scheduled')='scheduled' AND c.slug<>? ORDER BY c.channel_number", (weather_slug,))]
        interval = self.config.data.get('external_media', {}).get(
            'rescan_interval_hours', 168
        )
        try:
            interval = max(1, int(interval))
        except (TypeError, ValueError):
            interval = 168
        return {
            'sources':result,
            'channels':channels,
            'dayparts':sorted(self.dayparts),
            'rescan_interval_hours':interval,
        }

    def scan_due(self, *, cancel_event=None, interval_hours=None):
        """Scan explicitly pending sources and sources past the periodic interval."""
        if interval_hours is None:
            interval_hours = self.config.data.get('external_media', {}).get(
                'rescan_interval_hours', 168
            )
        try:
            interval = timedelta(hours=max(1, int(interval_hours)))
        except (TypeError, ValueError):
            interval = timedelta(hours=168)
        now = datetime.now(timezone.utc)
        due = []
        for row in self.connection.execute(
            "SELECT r.id,s.scan_status,s.last_scan_at FROM storage_roots r "
            "JOIN external_sources s ON s.root_id=r.id "
            "WHERE r.source_kind='external' AND r.enabled=1 ORDER BY r.id"
        ).fetchall():
            if row['scan_status'] in {'pending', 'running'} or not row['last_scan_at']:
                due.append(int(row['id']))
                continue
            try:
                last_scan = datetime.fromisoformat(row['last_scan_at'])
                if last_scan.tzinfo is None:
                    last_scan = last_scan.replace(tzinfo=timezone.utc)
                if now - last_scan.astimezone(timezone.utc) >= interval:
                    due.append(int(row['id']))
            except (TypeError, ValueError):
                # An unusable timestamp should recover through a fresh scan.
                due.append(int(row['id']))
        return [
            self.scan(root_id, cancel_event=cancel_event)
            for root_id in due
            if not (cancel_event and cancel_event.is_set())
        ]

    def scan(self, root_id, *, cancel_event=None):
        source = self._source(root_id)
        if not source['enabled']:
            raise ValueError('Reconnect this source before scanning')
        root = Path(source['absolute_path'])
        errors, seen, processed = [], set(), 0
        status = 'completed'
        try:
            # Test access before claiming availability. os.walk reports subtree failures.
            with os.scandir(root):
                pass
        except OSError as exc:
            self.connection.execute("UPDATE external_sources SET availability='unavailable',scan_status='error',scan_error=?,last_scan_at=? WHERE root_id=?", (str(exc),utc_now(),root_id))
            self.connection.commit()
            return {'root_id':root_id,'status':'unavailable','error':str(exc)}
        self.connection.execute("UPDATE external_sources SET availability='available',scan_status='running',processed_count=0,scan_error=NULL WHERE root_id=?", (root_id,))
        cursor = self.connection.execute("INSERT INTO scan_runs(root_id,started_at,status) VALUES (?,?,'running')", (root_id,utc_now()))
        run_id = int(cursor.lastrowid)
        self.connection.commit()
        scanner = LibraryScanner(self.connection,self.config,inspector=self.inspector)
        try:
            for directory, directories, names in os.walk(root, followlinks=False, onerror=lambda exc: errors.append(str(exc))):
                # Never traverse aliases, cycles, or resolved directories outside the registered root.
                safe_directories = []
                for directory_name in sorted(directories):
                    if _directory_alias(Path(directory)/directory_name):
                        errors.append(f'{directory_name}: nested directory alias skipped; register its actual series root')
                    else:
                        safe_directories.append(directory_name)
                directories[:] = safe_directories
                for name in sorted(names):
                    if cancel_event and cancel_event.is_set():
                        status = 'cancelled'
                        break
                    path = Path(directory)/name
                    if path.suffix.lower() not in scanner.extensions:
                        continue
                    relative = path.relative_to(root).as_posix()
                    seen.add(relative)
                    try:
                        path = contained_path(root,relative)
                        stat = path.stat()
                        row = self.connection.execute('SELECT * FROM assets WHERE source_root_id=? AND source_relative_path=?', (root_id,relative)).fetchone()
                        unchanged = row and row['file_size']==stat.st_size and row['modified_ns']==stat.st_mtime_ns and row['metadata_status']=='ready'
                        if unchanged:
                            self.connection.execute("UPDATE assets SET last_seen_at=?,last_scan_run_id=?,integrity_status='ready',review_status='ready',enabled_for_scheduling=1 WHERE id=?", (utc_now(),run_id,row['id']))
                        else:
                            fingerprint, algorithm = fingerprint_file(path)
                            if row is None:
                                matches = self.connection.execute('SELECT * FROM assets WHERE source_root_id=? AND fingerprint=? AND fingerprint_algorithm=? AND file_size=?', (root_id,fingerprint,algorithm,stat.st_size)).fetchall()
                                matches = [m for m in matches if not contained_path(root,m['source_relative_path']).exists()]
                                if len(matches)==1:
                                    row = matches[0]
                            probe = self.inspector.probe(path)
                            ok = probe.ok and float(probe.metadata.get('duration_seconds') or 0)>0
                            now = utc_now()
                            if row is None:
                                cursor = self.connection.execute("INSERT INTO assets(source_root_id,source_relative_path,original_filename,file_size,modified_ns,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?)", (root_id,relative,name,stat.st_size,stat.st_mtime_ns,now,now))
                                asset_id = int(cursor.lastrowid)
                            else:
                                asset_id = row['id']
                            self.connection.execute("UPDATE assets SET source_relative_path=?,original_filename=?,file_size=?,modified_ns=?,fingerprint=?,fingerprint_algorithm=?,media_type='episode',display_title=?,series_title=?,metadata_status=?,integrity_status=?,review_status=?,enabled_for_scheduling=?,last_seen_at=?,last_scan_run_id=? WHERE id=?", (relative,name,stat.st_size,stat.st_mtime_ns,fingerprint,algorithm,path.stem,source['series_title'],'ready' if ok else 'error','ready' if ok else 'error','ready' if ok else 'open',int(ok),now,run_id,asset_id))
                            scanner._replace_media_info(asset_id,probe)
                            self.connection.execute("DELETE FROM review_items WHERE asset_id=? AND reason_code='external_probe'", (asset_id,))
                            if not ok:
                                self.connection.execute("INSERT INTO review_items(asset_id,reason_code,detail,status,created_at) VALUES (?,'external_probe',?,'open',?)", (asset_id,probe.error_detail or 'No playable duration',now))
                                errors.append(f'{relative}: {probe.error_detail or "No playable duration"}')
                        processed += 1
                        self.connection.execute('UPDATE external_sources SET processed_count=? WHERE root_id=?', (processed,root_id))
                        self.connection.commit()
                    except (OSError,ValueError,RuntimeError) as exc:
                        self.connection.rollback()
                        errors.append(f'{relative}: {exc}')
                if status=='cancelled':
                    break
            # Conservative: any enumeration/file failure disables missing reconciliation.
            if status=='completed' and not errors:
                self.connection.execute("UPDATE assets SET integrity_status='missing',review_status='ignored',enabled_for_scheduling=0 WHERE source_root_id=? AND (last_scan_run_id IS NULL OR last_scan_run_id!=?)", (root_id,run_id))
                self.connection.execute("UPDATE review_items SET status='resolved',resolved_at=? WHERE reason_code='external_probe' AND asset_id IN (SELECT id FROM assets WHERE source_root_id=? AND integrity_status='missing')", (utc_now(),root_id))
            if errors and status=='completed':
                status='partial'
        except Exception as exc:
            errors.append(str(exc))
            status='error'
        self._eligibility(root_id)
        self.connection.execute('UPDATE external_sources SET scan_status=?,scan_error=?,last_scan_at=?,discovered_count=? WHERE root_id=?', (status,'; '.join(errors)[:4000] or None,utc_now(),len(seen),root_id))
        ready = self.connection.execute("SELECT COUNT(*) FROM assets WHERE source_root_id=? AND last_scan_run_id=? AND metadata_status='ready'", (root_id,run_id)).fetchone()[0]
        self.connection.execute('UPDATE scan_runs SET completed_at=?,status=?,discovered_count=?,ready_count=?,error_count=? WHERE id=?', (utc_now(),status,len(seen),ready,len(errors),run_id))
        self.connection.commit()
        return {'root_id':root_id,'status':status,'processed':processed,'errors':errors}

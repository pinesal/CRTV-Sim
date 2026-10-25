"""Weekly-special projections, kept alongside (and separate from) regular progress."""
import json
from datetime import datetime, time


class WeeklyLineups:
    def __init__(self, builder, state):
        self.builder = builder
        self.state = state
        self.used = {}

    def used_for(self, owner):
        return self.used.setdefault(self.key(owner), set())

    @staticmethod
    def key(owner):
        return json.dumps([owner.slug, owner.collection_slug], separators=(',', ':'))

    def saved(self, owner):
        return self.state.setdefault(self.key(owner), {'progress': {}, 'slots': {}})

    @staticmethod
    def progress_key(series, collection_type, collection_slug):
        return json.dumps([series, collection_type, collection_slug], separators=(',', ':'))

    def previous(self, owner, candidates):
        progress = self.saved(owner)['progress']
        result = {}
        for candidate in candidates:
            row = progress.get(self.progress_key(candidate.series, candidate.collection_type, candidate.collection_slug))
            if row:
                result[candidate.series] = int(row['last_asset_id'])
        return result

    def slot(self, owner, when):
        interval = owner.active_interval(when)
        if interval is None:
            return None
        minutes = int((when - interval[0]).total_seconds() // 60)
        return str(minutes // self.builder.clock_slot_minutes * self.builder.clock_slot_minutes)

    def preferred(self, owner, when, groups):
        if owner.schedule_mode != 'recurring':
            return None
        series = self.saved(owner)['slots'].get(self.slot(owner, when))
        return series if series in groups else None

    def note(self, owner, when, asset_id, series, collection_type, collection_slug):
        self.used_for(owner).add(asset_id)
        saved = self.saved(owner)
        saved['progress'][self.progress_key(series, collection_type, collection_slug)] = {'last_asset_id': asset_id}
        # Seasonal replacements and complete blocks must not become the normal grid.
        slot = self.slot(owner, when)
        if slot is not None and collection_type == 'weekly' and collection_slug == owner.collection_slug:
            saved['slots'][slot] = series

    def record(self, timeline):
        owners = {window.slug: window for window in self.builder.rules.windows}
        seen = set()
        for item in timeline:
            owner = owners.get(item.programming_owner)
            if not owner or not item.is_program or item.asset_id is None:
                continue
            identity = (owner.slug, item.sequence_group, item.asset_id)
            if identity in seen:
                continue
            seen.add(identity)
            row = self.builder.connection.execute(
                "SELECT series_title,display_title,original_filename,collection_type,collection_slug,media_type FROM assets WHERE id=?",
                (item.asset_id,),
            ).fetchone()
            if not row or row['media_type'] == 'complete_block':
                continue
            selection = json.loads(item.explanation_json or '{}').get('selection', {})
            self.note(owner, item.starts_at, item.asset_id,
                      selection.get('series') or row['series_title'] or row['display_title'] or row['original_filename'],
                      selection.get('collection_type', row['collection_type']),
                      selection.get('collection', selection.get('collection_slug', row['collection_slug'])))


def seed_weekly(builder, projection, channel_id, channel_slug, schedule_date):
    state = projection.setdefault('weekly_lineups', {})
    service = WeeklyLineups(builder, state)
    before = datetime.combine(schedule_date, time.min, builder.timezone).isoformat(timespec='milliseconds')
    for owner in builder.rules.windows:
        if owner.channel_slug != channel_slug or service.key(owner) in state:
            continue
        service.saved(owner)
        # Only active, prior schedules count. Superseded rebuilds and future
        # projections must never advance an episode a second time.
        rows = builder.connection.execute(
            """SELECT e.*, a.series_title,a.display_title,a.original_filename,
                      a.collection_type,a.collection_slug,a.media_type
               FROM schedule_entries e JOIN schedule_generations g ON g.id=e.generation_id
               JOIN assets a ON a.id=e.asset_id
               WHERE e.channel_id=? AND e.programming_owner=? AND e.is_program=1
                 AND g.status='active' AND e.ends_at<=?
               ORDER BY e.starts_at DESC LIMIT 2048""", (channel_id, owner.slug, before),
        ).fetchall()
        seen = set()
        for row in reversed(rows):
            if row['media_type'] == 'complete_block':
                continue
            identity = (row['generation_id'], row['sequence_group'], row['asset_id'])
            if identity in seen:
                continue
            seen.add(identity)
            selection = json.loads(row['explanation_json'] or '{}').get('selection', {})
            collection = selection.get('collection', selection.get('collection_slug', row['collection_slug']))
            kind = selection.get('collection_type', row['collection_type'])
            if kind == 'weekly' and collection != owner.collection_slug:
                continue
            service.note(owner, datetime.fromisoformat(row['starts_at']), row['asset_id'],
                         selection.get('series') or row['series_title'] or row['display_title'] or row['original_filename'],
                         kind, collection)
    return projection

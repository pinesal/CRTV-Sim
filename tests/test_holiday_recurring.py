"""Seasonal overlays must fulfil holiday assignments without damaging base state."""
import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import test_scheduling as fixture
from retrotv.scheduling import ScheduleBuilder
from retrotv.recurring_scopes import ScopePlacement, allocate_recurring_scopes
from retrotv.database_maintenance import DatabaseHousekeepingService


class HolidayRecurringTests(unittest.TestCase):
    setUp = fixture.SchedulingTests.setUp
    tearDown = fixture.SchedulingTests.tearDown
    add_asset = fixture.SchedulingTests.add_asset

    def setup_lineup(self, base_count=8, broad=False, unique=False):
        self.config.data['scheduling']['dayparts'] = [
            {'slug': 'morning', 'start': '06:00', 'end': '10:00', 'boundary_policy': 'must_fit'},
            {'slug': 'night', 'start': '10:00', 'end': '06:00', 'boundary_policy': 'must_fit'},
        ]
        self.config.data['seasonal'] = {'holidays': {'halloween': {
            'start': '10-01', 'end': '10-31', 'mode': 'boost', 'priority': 200}}}
        for index in range(base_count):
            for episode in range(5):
                self.add_asset(index * 10 + episode + 1, media_type='episode',
                               daypart='daytime' if broad else 'morning', duration=1380,
                               series=f'Regular {index}')
                if broad:
                    self.connection.execute("UPDATE asset_eligibility SET daypart_slug='morning' WHERE asset_id=?",
                                            (index * 10 + episode + 1,))
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences('fox', recurring_enabled=True, unique_weekends=unique)
        builder.generate_day('fox', date(2026, 9, 30))

    def holiday(self, index=0, episodes=5, scope='morning', holiday='halloween', duration=1380,
                channel='fox', series=None, media_type='episode'):
        ids = []
        for episode in range(episodes):
            asset_id = 1000 + index * 100 + episode
            self.add_asset(asset_id, media_type=media_type, daypart=scope, duration=duration,
                           series=series or f'Holiday {index}', channel=channel,
                           collection_type='holiday', collection_slug=holiday)
            self.connection.execute("UPDATE assets SET daypart_slug=?,source_relative_path=? WHERE id=?",
                                    (scope, f'{channel or "shared"}/specials/holidays/{holiday}/{scope}/{media_type}s/{series or index}/{episode}.mp4', asset_id))
            self.connection.execute("UPDATE asset_eligibility SET daypart_slug=? WHERE asset_id=?",
                                    ('all-day' if scope == 'all-day' else 'morning' if scope == 'daytime' else scope, asset_id))
            ids.append(asset_id)
        self.connection.commit()
        return ids

    def holiday_window(self, start='10-01', end='10-01'):
        rule = self.config.data['seasonal']['holidays']['halloween']
        rule['start'], rule['end'] = start, end

    def folderize_base_series(self, series):
        rows = self.connection.execute(
            "SELECT id,original_filename FROM assets WHERE series_title=? AND collection_type IS NULL",
            (series,),
        ).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE assets SET source_relative_path=? WHERE id=?",
                (f"fox/morning/episodes/{series}/Season 01/{row['original_filename']}", row['id']),
            )
        self.connection.commit()

    def generate(self, target):
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day('fox', target)
        self.assertEqual(builder.validate_generation(result.generation_id)[0], 'valid')
        rows = self.connection.execute('''SELECT se.asset_id,a.series_title,se.starts_at,se.daypart_slug AS daypart,se.explanation_json,
                      se.programming_block_id FROM schedule_entries se JOIN assets a ON a.id=se.asset_id
                      WHERE se.generation_id=? AND se.is_program=1 AND se.media_start_ms=0
                      ORDER BY se.starts_at''', (result.generation_id,)).fetchall()
        state = json.loads(self.connection.execute('SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?',
                                                   (result.generation_id,)).fetchone()[0])
        return rows, state

    def test_one_holiday_series_keeps_seven_regular_slots_and_advances(self):
        self.setup_lineup()
        self.holiday(episodes=30)
        first, state = self.generate(date(2026, 10, 1))
        self.assertEqual(sum(row['asset_id'] >= 1000 for row in first), 1)
        self.assertEqual(sum(row['asset_id'] < 1000 for row in first), 7)
        second, state2 = self.generate(date(2026, 10, 2))
        a = next(row for row in first if row['asset_id'] >= 1000)
        b = next(row for row in second if row['asset_id'] >= 1000)
        self.assertEqual((a['starts_at'][11:], b['starts_at'][11:]), (a['starts_at'][11:], a['starts_at'][11:]))
        self.assertNotEqual(a['asset_id'], b['asset_id'])
        self.assertTrue(json.loads(a['explanation_json'])['selection']['recurring_holiday_override'])
        self.assertEqual(state['slots'], state2['slots'])

    def test_full_takeover_preserves_broad_base_grid_and_progress(self):
        self.setup_lineup(broad=True)
        self.holiday_window()
        _, before = self.generate(date(2026, 9, 30))
        for index in range(8): self.holiday(index)
        rows, during = self.generate(date(2026, 10, 1))
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row['asset_id'] >= 1000 for row in rows))
        self.assertEqual(before['slots'], during['slots'])
        self.assertEqual(before['progress'], during['progress'])
        after, restored = self.generate(date(2026, 11, 1))
        self.assertTrue(all(row['asset_id'] < 1000 for row in after))
        self.assertEqual(before['slots'], restored['slots'])
        self.assertEqual(during['holiday_lineups'], restored['holiday_lineups'])

    def test_partial_overlay_does_not_advance_displaced_series(self):
        self.setup_lineup()
        self.holiday_window()
        _, before = self.generate(date(2026, 9, 30))
        self.holiday()
        rows, after = self.generate(date(2026, 10, 1))
        aired = {row['series_title'] for row in rows if row['asset_id'] < 1000}
        displaced = set(before['progress']) - aired
        self.assertEqual(len(displaced), 1)
        for series in displaced: self.assertEqual(before['progress'][series], after['progress'][series])

    def test_progress_continues_next_year_and_reruns(self):
        self.setup_lineup()
        self.holiday(episodes=2)
        _, planned = self.generate(date(2026, 10, 1))
        targets = [date.fromisoformat(item['target_date']) for item in
                   planned['holiday_lineups']['halloween']['calendar']['Holiday 0']]
        a, _ = self.generate(targets[0])
        b, _ = self.generate(targets[1])
        _, next_season = self.generate(date(2027, 10, 1))
        next_target = date.fromisoformat(
            next_season['holiday_lineups']['halloween']['calendar']['Holiday 0'][0]['target_date'])
        c, _ = self.generate(next_target)
        selected = [next(row['asset_id'] for row in rows if row['asset_id'] >= 1000) for rows in (a,b,c)]
        self.assertEqual(selected, [1000, 1001, 1000])

    def test_waiting_rotates_on_completed_runs(self):
        self.setup_lineup()
        for index in range(9): self.holiday(index, episodes=1)
        self.generate(date(2026, 10, 1))
        first, state = self.generate(date(2026, 10, 16))
        second, next_state = self.generate(date(2026, 10, 17))
        first_ids = {row['asset_id'] for row in first}
        self.assertEqual(len(first_ids), 8)
        self.assertTrue({row['asset_id'] for row in second} - first_ids)
        self.assertEqual(sum(len(q) for q in state['holiday_lineups']['halloween']['queues']['daily'].values()), 1)

    def test_decorate_and_nonrecurring_modes_keep_existing_behavior(self):
        self.setup_lineup()
        self.holiday()
        self.config.data['seasonal']['holidays']['halloween']['mode'] = 'decorate'
        rows, _ = self.generate(date(2026, 10, 1))
        self.assertTrue(all(row['asset_id'] < 1000 for row in rows))
        self.config.data['seasonal']['holidays']['halloween']['mode'] = 'replace'
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences('fox', recurring_enabled=False, unique_weekends=False)
        context = builder.rules.context('fox', datetime(2026, 10, 1, 6, tzinfo=builder.timezone))
        pools = builder._pools_for_context('fox', 'morning', context)
        self.assertEqual({item.asset_id for item in pools[0]}, set(range(1000,1005)))

    def test_overlapping_holidays_priority_and_long_program_capacity(self):
        self.setup_lineup()
        self.holiday_window()
        self.config.data['seasonal']['holidays']['autumn'] = {
            'start':'10-01','end':'10-31','mode':'boost','priority':100}
        self.holiday(0, duration=44*60)
        for index in range(1,8): self.holiday(index, holiday='autumn')
        rows, state = self.generate(date(2026, 10, 1))
        self.assertEqual(sum(1000 <= row['asset_id'] < 1100 for row in rows), 1)
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(state['holiday_lineups']['autumn']['slots']['daily:morning']), 6)

    def test_shared_daytime_and_all_day_scopes_do_not_multiply(self):
        self.setup_lineup()
        self.holiday_window()
        self.holiday(0, scope='all-day', channel=None)
        self.holiday(1, scope='daytime')
        rows, state = self.generate(date(2026, 10, 1))
        for series in ('Holiday 0','Holiday 1'):
            self.assertEqual(sum(row['series_title'] == series for row in rows), 1)
        self.assertEqual(len(state['holiday_lineups']['halloween']['slots']['daily:morning']), 1)

    def test_weekend_patterns_are_independent(self):
        self.setup_lineup(unique=True)
        self.holiday(episodes=10)
        self.generate(date(2026,10,1))
        rows, state = self.generate(date(2026,10,2))
        self.assertEqual(sum(row['asset_id'] >= 1000 for row in rows), 1)
        self.assertEqual(next(row['asset_id'] for row in rows if row['asset_id'] >= 1000), 1000)
        for target in (date(2026,10,3), date(2026,10,4)):
            rows, state = self.generate(target)
            self.assertEqual(sum(row['asset_id'] >= 1000 for row in rows), 0)
        self.assertEqual(set(state['holiday_lineups']['halloween']['slots']),
                         {'weekday:morning','weekday:night','saturday:morning','saturday:night','sunday:morning','sunday:night'})

    def test_overdue_episodes_without_remaining_dates_defer_without_parse_error(self):
        self.setup_lineup()
        self.holiday(episodes=40)
        self.generate(date(2026, 10, 1))
        rows, state = self.generate(date(2026, 10, 31))
        self.assertEqual(sum(row['asset_id'] >= 1000 for row in rows), 1)
        saved = state['holiday_lineups']['halloween']
        self.assertTrue(any(not item.get('target_date')
                            for item in saved['calendar']['Holiday 0']))
        self.assertEqual(len(saved['progress']['Holiday 0']['seen_asset_ids']), 2)
        _, next_season = self.generate(date(2027, 10, 1))
        progress = next_season['holiday_lineups']['halloween']['progress']['Holiday 0']
        self.assertTrue({1000, 1030}.issubset(set(progress['seen_asset_ids'])))

    def test_deferred_calendar_entries_with_missing_dates_are_not_due(self):
        self.setup_lineup()
        builder = ScheduleBuilder(self.connection, self.config)
        for entry in ({'target_date': ''}, {'target_date': None}, {}):
            with self.subTest(entry=entry):
                overlay = {
                    'plan': {'morning': {'360': 'Holiday 0'}},
                    'done': set(), 'pools': {}, 'series_today': set(),
                    'saved': {'progress': {}, 'calendar': {'Holiday 0': [
                        {'asset_id': 1000, 'daypart': 'morning', **entry}]}},
                }
                selected, selected_overlay, budget = builder._select_holiday_recurring_program(
                    [overlay], 'morning', datetime(2026, 10, 31, 6),
                    30 * 60_000, date(2026, 10, 31))
                self.assertIsNone(selected)
                self.assertIsNone(selected_overlay)
                self.assertEqual(budget, 30 * 60_000)

    def test_single_episode_targets_the_midpoint_and_uses_regular_fallback(self):
        self.setup_lineup()
        self.holiday(episodes=1)
        first, state = self.generate(date(2026, 10, 1))
        target = state['holiday_lineups']['halloween']['calendar']['Holiday 0'][0]
        self.assertEqual(target['target_date'], '2026-10-16')
        self.assertFalse(any(row['asset_id'] >= 1000 for row in first))
        before, _ = self.generate(date(2026, 10, 15))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in before))
        due, _ = self.generate(date(2026, 10, 16))
        self.assertEqual(sum(row['asset_id'] >= 1000 for row in due), 1)
        after, _ = self.generate(date(2026, 10, 17))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in after))

    def test_four_episodes_are_evenly_spaced_across_the_holiday(self):
        self.setup_lineup()
        self.holiday(episodes=4)
        _, state = self.generate(date(2026, 10, 1))
        calendar = state['holiday_lineups']['halloween']['calendar']['Holiday 0']
        targets = [date.fromisoformat(item['target_date']) for item in calendar]
        self.assertEqual(targets, [date(2026,10,4), date(2026,10,12),
                                   date(2026,10,20), date(2026,10,28)])
        selected = []
        for target in targets:
            rows, _ = self.generate(target)
            selected.append(next(row['asset_id'] for row in rows if row['asset_id'] >= 1000))
        self.assertEqual(selected, [1000, 1001, 1002, 1003])

    def test_same_named_holiday_series_replaces_its_regular_slot_without_advancing_it(self):
        self.setup_lineup()
        self.folderize_base_series('Regular 0')
        self.holiday(episodes=1, series='Regular 0')
        _, before = self.generate(date(2026, 10, 1))
        base_progress = before['progress']['Regular 0'].copy()
        due, after = self.generate(date(2026, 10, 16))
        holiday = next(row for row in due if row['asset_id'] >= 1000)
        self.assertEqual(holiday['series_title'], 'Regular 0')
        self.assertEqual(holiday['daypart'], 'morning')
        self.assertEqual(after['progress']['Regular 0'], base_progress)

    def test_same_named_holiday_in_another_daypart_does_not_replace_regular_daypart(self):
        self.setup_lineup()
        self.holiday(episodes=1, scope='night', series='Regular 0')
        self.generate(date(2026, 10, 1))
        rows, state = self.generate(date(2026, 10, 16))
        regular = [row for row in rows if row['asset_id'] < 1000 and row['series_title'] == 'Regular 0']
        holiday = [row for row in rows if row['asset_id'] >= 1000 and row['series_title'] == 'Regular 0']
        self.assertTrue(regular)
        self.assertEqual({row['daypart'] for row in regular}, {'morning'})
        self.assertEqual({row['daypart'] for row in holiday}, {'night'})
        self.assertIn('Regular 0', state['progress'])
        self.assertIn('Regular 0', state['holiday_lineups']['halloween']['progress'])

    def test_different_folder_name_does_not_match_even_when_series_labels_normalize_equal(self):
        self.setup_lineup()
        self.folderize_base_series('Regular 0')
        self.holiday(episodes=1, series='Regular 0')
        self.connection.execute(
            "UPDATE assets SET source_relative_path=REPLACE(source_relative_path,'/Regular 0/','/Regular_0/') WHERE id=1000"
        )
        self.connection.commit()
        self.generate(date(2026,10,1))
        rows, state = self.generate(date(2026,10,16))
        holiday = [row for row in rows if row['asset_id'] == 1000]
        regular = [row for row in rows if row['asset_id'] < 1000 and row['series_title'] == 'Regular 0']
        self.assertTrue(holiday)
        self.assertTrue(regular)
        self.assertGreater(len(state['progress']['Regular 0']['seen_asset_ids']), 2)

    def test_two_daypart_pools_share_one_calendar_and_alternate_when_eligible(self):
        self.setup_lineup()
        self.holiday(episodes=2)
        self.connection.execute("UPDATE assets SET daypart_slug='night' WHERE id=1001")
        self.connection.execute("UPDATE asset_eligibility SET daypart_slug='night' WHERE asset_id=1001")
        self.connection.commit()
        _, state = self.generate(date(2026, 10, 1))
        calendar = state['holiday_lineups']['halloween']['calendar']['Holiday 0']
        self.assertEqual([item['daypart'] for item in calendar], ['morning', 'night'])
        rows1, _ = self.generate(date.fromisoformat(calendar[0]['target_date']))
        rows2, _ = self.generate(date.fromisoformat(calendar[1]['target_date']))
        self.assertIn(1000, {row['asset_id'] for row in rows1})
        self.assertIn(1001, {row['asset_id'] for row in rows2})

    def test_new_holiday_episode_is_spread_over_remaining_dates_without_moving_existing_target(self):
        self.setup_lineup()
        self.holiday(episodes=1)
        _, initial = self.generate(date(2026, 10, 1))
        original = initial['holiday_lineups']['halloween']['calendar']['Holiday 0'][0]['target_date']
        self.holiday(1, episodes=1, series='Holiday 0')
        _, updated = self.generate(date(2026, 10, 10))
        calendar = updated['holiday_lineups']['halloween']['calendar']['Holiday 0']
        by_asset = {item['asset_id']: item['target_date'] for item in calendar}
        self.assertEqual(by_asset[1000], original)
        self.assertGreaterEqual(by_asset[1100], '2026-10-10')
        self.assertNotEqual(by_asset[1000], by_asset[1100])

    def test_library_addition_removal_and_unplaceable_media(self):
        self.setup_lineup()
        self.holiday_window('10-01', '10-02')
        self.holiday()
        self.holiday(1, duration=5*3600)
        rows, _ = self.generate(date(2026,10,1))
        self.assertEqual(sum(row['asset_id'] >= 1000 for row in rows), 1)
        self.holiday(2)
        self.connection.execute('UPDATE assets SET enabled_for_scheduling=0 WHERE id BETWEEN 1000 AND 1004')
        self.connection.commit()
        rows, state = self.generate(date(2026,10,2))
        self.assertEqual({row['series_title'] for row in rows if row['asset_id'] >= 1000}, {'Holiday 2'})

    def test_full_weekly_window_defers_holiday_without_erasing_either_grid(self):
        self.setup_lineup(broad=True)
        self.holiday_window('10-02', '10-03')
        self.holiday()
        _, initial = self.generate(date(2026,10,2))
        self.config.data['seasonal']['windows'] = [{
            'slug':'cartoons','channel':'fox','weekdays':['friday','saturday'], 'start':'06:00','end':'10:00',
            'collection':'cartoons','guide_label':'Cartoons','enabled':True}]
        self.add_asset(500, media_type='episode', daypart='all-day', duration=1380, series='Fixed show',
                       collection_type='weekly', collection_slug='cartoons')
        self.connection.commit()
        rows, covered = self.generate(date(2026,10,3))
        self.assertTrue(all(row['asset_id'] == 500 for row in rows))
        self.assertEqual(initial['slots'], covered['slots'])
        self.assertEqual(initial['holiday_lineups']['halloween']['slots'], covered['holiday_lineups']['halloween']['slots'])
        self.assertEqual(initial['holiday_lineups']['halloween']['progress'], covered['holiday_lineups']['halloween']['progress'])
        resumed, state = self.generate(date(2026,10,4))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in resumed))
        self.assertEqual(initial['slots'], state['slots'])

    def test_partial_weekly_window_moves_one_date_only(self):
        self.setup_lineup()
        self.holiday_window('10-02', '10-02')
        self.holiday()
        self.generate(date(2026,10,1))
        self.config.data['seasonal']['windows'] = [{
            'slug':'cartoons','channel':'fox','weekdays':['friday'], 'start':'06:00','end':'06:30',
            'collection':'cartoons','guide_label':'Cartoons','enabled':True}]
        self.add_asset(500,media_type='episode',daypart='all-day',duration=1380,series='Fixed',
                       collection_type='weekly',collection_slug='cartoons')
        self.connection.commit()
        shifted,state=self.generate(date(2026,10,2))
        self.assertEqual(next(row['starts_at'][11:16] for row in shifted if row['asset_id']>=1000),'06:30')
        self.assertEqual(state['holiday_lineups']['halloween']['slots']['daily:morning'], {'390':'Holiday 0'})
        normal,_=self.generate(date(2026,10,3))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in normal))

    def test_complete_block_displaces_holiday_to_next_available_slot(self):
        self.setup_lineup()
        self.holiday_window()
        self.holiday()
        self.config.data['blocks']['selection_probability']=1.0
        self.add_asset(500,media_type='complete_block',daypart='morning',duration=3600,series='Full block')
        self.connection.commit()
        rows,state=self.generate(date(2026,10,1))
        self.assertEqual(next(row['starts_at'][11:16] for row in rows if row['asset_id']==500),'06:00')
        holiday=next(row for row in rows if row['asset_id']>=1000)
        self.assertEqual(holiday['starts_at'][11:16],'07:00')
        self.assertEqual(state['holiday_lineups']['halloween']['slots']['daily:morning'], {'360':'Holiday 0'})

    def test_shadow_target_and_matching_series_name_have_separate_progress(self):
        self.setup_lineup()
        self.holiday_window()
        before,_=ScheduleBuilder(self.connection,self.config)._recurring_state_before(
            1,'fox',date(2026,10,1),unique_weekends=False)
        series=before['slots']['daily:morning']['390']
        target=before['progress'][series]['last_asset_id']+1
        self.folderize_base_series(series)
        # Regular 1's next normal episode and the holiday shadow deliberately
        # refer to the same physical file. The holiday must not advance base order.
        self.connection.execute(f'''INSERT INTO shadow_files(shadow_relative_path,target_asset_id,
            target_relative_path,media_type,channel_slug,daypart_slug,collection_type,collection_slug,
            holiday_slug,series_title,file_size,modified_ns,status,first_seen_at,last_seen_at)
            VALUES ('fox/specials/holidays/halloween/morning/episodes/{series}/Season 01/1.shadow',?,
                    ?,'episode','fox','morning','holiday','halloween',
                    'halloween',?,1,1,'ready',?,?)''',(target,f'fox/morning/episodes/{target}.mp4',series,self.now,self.now))
        self.connection.execute("INSERT INTO shadow_eligibility(shadow_id,channel_slug,daypart_slug,eligibility_source) VALUES(1,'fox','morning','test')")
        self.connection.commit()
        rows,state=self.generate(date(2026,10,1))
        origins=[json.loads(row['explanation_json'])['selection']['collection_type'] for row in rows if row['asset_id']==target]
        self.assertEqual(origins,['holiday'])
        self.assertEqual(state['progress'][series],before['progress'][series])
        self.assertEqual(state['holiday_lineups']['halloween']['progress'][series]['last_asset_id'],target)

    def test_checkpoint_survives_schedule_pruning_and_status_exposes_overlay(self):
        self.setup_lineup()
        self.holiday()
        _,planned=self.generate(date(2026,10,1))
        target_dates=[date.fromisoformat(item['target_date']) for item in
                      planned['holiday_lineups']['halloween']['calendar']['Holiday 0']]
        self.generate(target_dates[0])
        self.generate(target_dates[1])
        _,before=self.generate(target_dates[2])
        DatabaseHousekeepingService(self.connection,self.config)._checkpoint_schedule_history('2026-10-17')
        self.connection.execute("DELETE FROM schedule_generations WHERE schedule_date < '2026-10-17'")
        self.connection.commit()
        rows,state=self.generate(date(2026,10,22))
        self.assertTrue(any(row['asset_id'] >= 1000 for row in rows))
        with patch('retrotv.scheduling.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime(2026,10,22,12)
            response=ScheduleBuilder(self.connection,self.config).recurring_lineup('fox')
        self.assertEqual(response['normal_resumes_on'],'2026-11-01')
        self.assertEqual(response['holidays'][0]['slug'],'halloween')
        self.assertEqual(len(response['regular_fallback_patterns']['daily']['morning']),7)
        self.assertEqual(response['patterns']['daily']['morning'][0]['series'], before['slots']['daily:morning']['360'])

    def test_fingerprints_invalidate_unlocked_dates_and_locked_dates_are_protected(self):
        self.setup_lineup()
        self.holiday()
        self.generate(date(2026,10,1))
        builder=ScheduleBuilder(self.connection,self.config)
        self.assertTrue(builder.generation_is_current('fox',date(2026,10,1)))
        self.holiday(1)
        builder=ScheduleBuilder(self.connection,self.config)
        self.assertFalse(builder.generation_is_current('fox',date(2026,10,1)))
        self.connection.execute("UPDATE schedule_generations SET locked=1 WHERE schedule_date='2026-10-01' AND status='active'")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError,'locked'):
            builder.generate_day('fox',date(2026,10,1))

    def test_gap_before_reserved_holiday_slot_fills_without_program_block(self):
        self.setup_lineup()
        self.holiday_window('10-01', '10-02')
        self.holiday()
        _, state = self.generate(date(2026, 10, 1))
        state['holiday_lineups']['halloween']['slots']['daily:morning'] = {'390': 'Holiday 0'}
        self.connection.execute(
            "UPDATE recurring_lineup_snapshots SET state_json=? WHERE schedule_date='2026-10-01'",
            (json.dumps(state),))
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=? WHERE asset_id<1000', (40 * 60,))
        self.connection.commit()
        rows, _ = self.generate(date(2026, 10, 2))
        holiday = next(row for row in rows if row['asset_id'] >= 1000)
        self.assertEqual(holiday['starts_at'][11:16], '06:30')
        gap = self.connection.execute('''SELECT se.entry_type,se.programming_block_id,se.ends_at
            FROM schedule_entries se JOIN schedule_generations sg ON sg.id=se.generation_id
            WHERE sg.schedule_date='2026-10-02' AND sg.status='active'
              AND se.starts_at LIKE '%T06:00:%' ''').fetchone()
        self.assertIn(gap['entry_type'], ('continuity', 'graphic_interstitial'))
        self.assertIsNone(gap['programming_block_id'])
        self.assertEqual(gap['ends_at'][11:16], '06:30')

    def test_short_episodes_share_one_holiday_assignment(self):
        self.setup_lineup()
        self.holiday_window()
        self.holiday(duration=11*60)
        rows,state=self.generate(date(2026,10,1))
        holiday=[row for row in rows if row['asset_id']>=1000]
        self.assertEqual(len(holiday),2)
        self.assertEqual(len({row['programming_block_id'] for row in holiday}),1)
        self.assertEqual(state['holiday_lineups']['halloween']['progress']['Holiday 0']['seen_asset_ids'],[1000,1001])

    def test_90_and_120_minute_movies_reserve_their_full_span(self):
        self.setup_lineup()
        self.holiday_window()
        self.holiday(0,duration=90*60,media_type='movie')
        self.holiday(1,duration=120*60,media_type='movie')
        rows,state=self.generate(date(2026,10,1))
        self.assertEqual(sum(row['asset_id']>=1000 for row in rows),2)
        # Exact-length movies can also consume the final information-screen slot.
        self.assertLessEqual(sum(row['asset_id']<1000 for row in rows),1)
        self.assertEqual(len(state['holiday_lineups']['halloween']['slots']['daily:morning']),2)

    def test_all_day_covers_custom_daypart_and_episode_short_types(self):
        self.setup_lineup()
        self.holiday_window('10-01', '10-02')
        self.config.data['scheduling']['dayparts']=[
            {'slug':'over-night','start':'00:00','end':'06:00','boundary_policy':'must_fit'},
            {'slug':'morning','start':'06:00','end':'00:00','boundary_policy':'must_fit'}]
        self.holiday(0,scope='all-day',media_type='short',duration=10*60)
        rows,state=self.generate(date(2026,10,1))
        holiday=[row for row in rows if row['asset_id']>=1000]
        self.assertTrue(holiday)
        self.assertEqual(len({row['programming_block_id'] for row in holiday}),1)
        self.assertEqual({row['daypart'] for row in state['holiday_lineups']['halloween']['airings']},{'morning'})
        # With only the custom daypart eligible, it is still considered.
        self.connection.execute("UPDATE assets SET daypart_slug='over-night' WHERE id>=1000")
        self.connection.execute("UPDATE asset_eligibility SET daypart_slug='over-night' WHERE asset_id>=1000")
        self.connection.commit()
        _,state=self.generate(date(2026,10,2))
        self.assertEqual({row['daypart'] for row in state['holiday_lineups']['halloween']['airings']},{'over-night'})

    def test_explicit_shadow_dayparts_each_receive_a_placement(self):
        self.setup_lineup()
        self.holiday_window()
        self.holiday(episodes=1)
        self.connection.execute('''INSERT INTO shadow_files(shadow_relative_path,target_asset_id,
            target_relative_path,media_type,channel_slug,daypart_slug,collection_type,collection_slug,
            holiday_slug,series_title,file_size,modified_ns,status,first_seen_at,last_seen_at)
            VALUES ('fox/specials/holidays/halloween/night/episodes/Show/1.shadow',1000,
                    'target.mp4','episode','fox','night','holiday','halloween',
                    'halloween','Holiday 0',1,1,'ready',?,?)''',(self.now,self.now))
        self.connection.execute("INSERT INTO shadow_eligibility(shadow_id,channel_slug,daypart_slug,eligibility_source) VALUES(1,'fox','night','test')")
        self.connection.commit()
        rows,state=self.generate(date(2026,10,1))
        self.assertEqual(sum(row['asset_id']==1000 for row in rows),1)
        self.assertEqual(len(state['holiday_lineups']['halloween']['airings']),1)

    def test_weekend_setting_changes_keep_seasonal_progress(self):
        self.setup_lineup()
        self.holiday(episodes=10)
        self.generate(date(2026,10,1))
        self.generate(date(2026,10,2))
        ScheduleBuilder(self.connection,self.config).update_schedule_preferences(
            'fox',recurring_enabled=True,unique_weekends=True)
        rows,state=self.generate(date(2026,10,3))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in rows))
        self.assertNotIn('daily:morning',state['holiday_lineups']['halloween']['slots'])
        ScheduleBuilder(self.connection,self.config).update_schedule_preferences(
            'fox',recurring_enabled=True,unique_weekends=False)
        rows,state=self.generate(date(2026,10,4))
        self.assertFalse(any(row['asset_id'] >= 1000 for row in rows))
        self.assertNotIn('weekday:morning',state['holiday_lineups']['halloween']['slots'])


class HolidayAllocatorTests(unittest.TestCase):
    def test_sparse_allocation_preserves_exact_placements_and_broad_scope(self):
        placements = [ScopePlacement('A','morning',frozenset({'morning'})),
                      ScopePlacement('A','evening',frozenset({'evening'})),
                      ScopePlacement('A','all-day',frozenset({'morning','evening','night'}))]
        plan, _ = allocate_recurring_scopes(placements, {p:{'0','30'} for p in ('morning','evening','night')},
                                          {},{}, {}, {'A':{1}}, 'daily','2026-10-01', fill_repeats=False)
        self.assertEqual([list(plan[p].values()) for p in ('morning','evening','night')], [['A'],['A'],['A']])

    def test_long_assignment_reserves_contiguous_slots(self):
        plan, waiting = allocate_recurring_scopes(
            [ScopePlacement('A','morning',frozenset({'morning'})),ScopePlacement('B','morning',frozenset({'morning'}))],
            {'morning':{'0','30','60'}},{},{},{},{'A':{1},'B':{2}}, 'daily','2026-10-01',
            fill_repeats=False, slot_spans={'A':2,'B':1})
        self.assertEqual(plan['morning'], {'0':'A','60':'B'})
        self.assertFalse(waiting['morning'])

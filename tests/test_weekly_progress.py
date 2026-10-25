import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import test_scheduling as fixtures
from retrotv.scheduling import ScheduleBuilder
from retrotv.settings import SettingsService
from retrotv.database_maintenance import DatabaseHousekeepingService


class WeeklyProgressTests(unittest.TestCase):
    setUp = fixtures.SchedulingTests.setUp
    tearDown = fixtures.SchedulingTests.tearDown
    add_asset = fixtures.SchedulingTests.add_asset

    def setup_special(self, mode='recurring', parent=True, end='21:00'):
        ScheduleBuilder(self.connection,self.config).update_schedule_preferences('fox',recurring_enabled=parent,unique_weekends=False)
        self.config.data['seasonal'] = {'windows': {'snick': {
            'channel':'fox','collection':'snick','weekdays':['saturday'],
            'start':'20:00','end':end,'enabled':True,'schedule_mode':mode}}}
        for offset, series in ((0,'KaBlam!'), (10,'Doug')):
            for episode in range(1,7):
                self.add_asset(100+offset+episode,media_type='episode',daypart='all-day',
                               duration=1400,series=series,collection_type='weekly',collection_slug='snick')

    def generate(self, day):
        builder = ScheduleBuilder(self.connection,self.config)
        result = builder.generate_day('fox', day)
        rows = self.connection.execute('''SELECT e.asset_id,a.series_title,e.starts_at,e.programming_owner
            FROM schedule_entries e JOIN assets a ON a.id=e.asset_id
            WHERE e.generation_id=? AND e.is_program=1 AND e.media_start_ms=0
            ORDER BY e.starts_at''',(result.generation_id,)).fetchall()
        return builder,result,[dict(row) for row in rows]

    def test_both_modes_advance_weekly_episodes_independent_of_parent(self):
        self.setup_special()
        first=date(2026,10,17)
        _,_,rows=self.generate(first)
        self.assertEqual({row['asset_id'] for row in rows},{101,111})
        _,_,next_rows=self.generate(first+timedelta(days=7))
        self.assertEqual([row['series_title'] for row in next_rows],[row['series_title'] for row in rows])
        self.assertEqual({row['asset_id'] for row in next_rows},{102,112})
        self.config.data['seasonal']['windows']['snick']['schedule_mode']='random'
        ScheduleBuilder(self.connection,self.config).update_schedule_preferences('fox',recurring_enabled=False,unique_weekends=False)
        _,_,third=self.generate(first+timedelta(days=14))
        self.assertEqual({row['asset_id'] for row in third},{103,113})
        self.config.data['seasonal']['windows']['snick']['schedule_mode']='recurring'
        _,_,fourth=self.generate(first+timedelta(days=21))
        self.assertEqual({row['asset_id'] for row in fourth},{104,114})

    def test_random_special_on_recurring_parent_advances_and_ignores_saved_slots(self):
        self.setup_special(mode='random', parent=True)
        first=date(2026,10,17)
        _,result,_=self.generate(first)
        raw=self.connection.execute('SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?',(result.generation_id,)).fetchone()[0]
        state=json.loads(raw)
        for value in state['weekly_lineups'].values():value['slots']={'0':'KaBlam!','30':'KaBlam!'}
        self.connection.execute('UPDATE recurring_lineup_snapshots SET state_json=? WHERE generation_id=?',(json.dumps(state),result.generation_id))
        self.connection.commit()
        _,_,rows=self.generate(first+timedelta(days=7))
        self.assertEqual({row['asset_id'] for row in rows},{102,112})

    def test_legacy_history_seeds_next_episode_and_slots_without_reset(self):
        self.setup_special()
        first=date(2026,10,17)
        _,_,rows=self.generate(first)
        self.connection.execute('DELETE FROM recurring_lineup_snapshots')
        self.connection.commit()
        _,_,later=self.generate(first+timedelta(days=7))
        self.assertEqual({row['asset_id'] for row in later},{102,112})
        self.assertEqual([row['series_title'] for row in rows],[row['series_title'] for row in later])

    def test_rebuild_is_idempotent_and_locked_history_remains_untouched(self):
        self.setup_special(parent=False)
        first=date(2026,10,17)
        _,result,one=self.generate(first)
        _,_,rebuilt=self.generate(first)
        self.assertEqual(one,rebuilt)
        builder,later,two=self.generate(first+timedelta(days=7))
        self.assertEqual({row['asset_id'] for row in two},{102,112})
        self.assertTrue(builder.generation_is_current('fox',first+timedelta(days=7)))
        self.connection.execute('UPDATE schedule_generations SET locked=1 WHERE id=?',(later.generation_id,))
        self.connection.commit()
        self.config.data['seasonal']['windows']['snick']['schedule_mode']='random'
        builder=ScheduleBuilder(self.connection,self.config)
        self.assertTrue(builder.generation_is_current('fox',first+timedelta(days=7)))
        with self.assertRaisesRegex(ValueError,'locked'):builder.generate_day('fox',first+timedelta(days=7))

    def test_same_collection_in_two_specials_has_independent_progress(self):
        self.setup_special(end='20:30')
        other=dict(self.config.data['seasonal']['windows']['snick'],start='21:00',end='21:30')
        self.config.data['seasonal']['windows']['second']=other
        _,_,rows=self.generate(date(2026,10,17))
        first={row['programming_owner']:row['asset_id'] for row in rows}
        self.assertTrue(all(row['asset_id'] in {101,111} for row in rows))
        _,_,rows=self.generate(date(2026,10,24))
        by_owner={row['programming_owner']:row['asset_id'] for row in rows}
        # Each special advances its own previously chosen series, never shares pointers.
        self.assertEqual(by_owner,{owner:asset+1 for owner,asset in first.items()})

    def test_checkpoint_survives_pruning_old_schedules_and_restart(self):
        self.setup_special(parent=False)
        _,result,_=self.generate(date(2026,10,17))
        DatabaseHousekeepingService(self.connection,self.config)._checkpoint_schedule_history('2026-10-18')
        self.connection.execute('DELETE FROM schedule_generations WHERE id=?',(result.generation_id,))
        self.connection.commit()
        _,_,rows=self.generate(date(2026,10,24))
        self.assertEqual({row['asset_id'] for row in rows},{102,112})

    def test_rejected_rebuild_does_not_consume_special_episode(self):
        self.setup_special()
        self.generate(date(2026,10,17))
        with patch.object(ScheduleBuilder,'_validate_items',return_value=['Deliberate test rejection']):
            _,result,_=self.generate(date(2026,10,24))
            self.assertEqual(result.validation_status,'invalid')
        _,_,rows=self.generate(date(2026,10,24))
        self.assertEqual({row['asset_id'] for row in rows},{102,112})

    def test_midnight_continues_episode_progress_in_the_same_special(self):
        self.setup_special(end='00:30')
        self.config.data['seasonal']['windows']['snick']['start']='23:30'
        self.connection.execute('UPDATE assets SET enabled_for_scheduling=0 WHERE id>=111')
        self.connection.commit()
        _,_,saturday=self.generate(date(2026,10,17))
        _,_,sunday=self.generate(date(2026,10,18))
        self.assertEqual([row['asset_id'] for row in saturday],[101])
        self.assertEqual([row['asset_id'] for row in sunday],[102])

    def test_regular_and_two_weekly_shadows_keep_separate_pointers(self):
        self.setup_special(end='20:30',parent=False)
        self.connection.execute('DELETE FROM asset_eligibility')
        self.config.data['seasonal']['windows']['second']=dict(
            self.config.data['seasonal']['windows']['snick'],start='21:00',end='21:30',collection='second')
        for asset_id in range(1,5):
            self.add_asset(asset_id,media_type='episode',daypart='morning',duration=1400,series='KaBlam!')
            for collection in ('snick','second'):
                relative=f'fox/specials/weekly/{collection}/episodes/KaBlam!/{asset_id}.mp4.shadow'
                cursor=self.connection.execute('''INSERT INTO shadow_files(shadow_relative_path,target_asset_id,target_relative_path,
                    media_type,channel_slug,collection_type,collection_slug,series_title,file_size,modified_ns,status,first_seen_at,last_seen_at)
                    VALUES(?,?,?,'episode','fox','weekly',?,'KaBlam!',1,1,'ready',?,?)''',
                    (relative,asset_id,f'fox/morning/episodes/{asset_id}.mp4',collection,self.now,self.now))
                self.connection.execute("INSERT INTO shadow_eligibility(shadow_id,channel_slug,eligibility_source) VALUES(?,'fox','test')",(cursor.lastrowid,))
        self.connection.commit()
        _,_,rows=self.generate(date(2026,10,17))
        self.assertEqual({row['asset_id'] for row in rows if row['programming_owner']!='base'},{1})
        _,_,rows=self.generate(date(2026,10,24))
        self.assertEqual({row['asset_id'] for row in rows if row['programming_owner']!='base'},{2})

    def test_enabling_regular_recurrence_uses_latest_base_episode(self):
        self.setup_special(parent=False,end='20:30')
        self.add_asset(1,media_type='episode',daypart='morning',duration=1400,series='Regular')
        self.add_asset(2,media_type='episode',daypart='morning',duration=1400,series='Regular')
        self.generate(date(2026,10,17))
        self.generate(date(2026,10,18))
        builder=ScheduleBuilder(self.connection,self.config)
        expected=builder._previous_programs(1,__import__('datetime').datetime(2026,10,19,tzinfo=builder.timezone),base_only=True)['Regular']
        builder.update_schedule_preferences('fox',recurring_enabled=True,unique_weekends=False)
        state,_=ScheduleBuilder(self.connection,self.config)._recurring_state_before(1,'fox',date(2026,10,19),unique_weekends=False)
        self.assertEqual(state['progress']['Regular']['last_asset_id'],expected)

    def test_default_mode_freezes_parent_choice_and_snapshot_save_retains_it(self):
        self.setup_special()
        window=self.config.data['seasonal']['windows']['snick']
        del window['schedule_mode']
        builder=ScheduleBuilder(self.connection,self.config)
        self.assertEqual(builder.rules.windows[0].schedule_mode,'recurring')
        builder.update_schedule_preferences('fox',recurring_enabled=False,unique_weekends=False)
        self.assertEqual(ScheduleBuilder(self.connection,self.config).rules.windows[0].schedule_mode,'recurring')
        snapshot=SettingsService(self.config).snapshot([])
        self.assertEqual(snapshot['windows']['snick']['schedule_mode'],'recurring')
        cleaned=SettingsService._validate_windows({'snick':dict(window,schedule_mode='random')})
        self.assertEqual(cleaned['snick']['schedule_mode'],'random')
        for bad in ('shuffle',True,None):
            with self.assertRaises(ValueError):SettingsService._validate_windows({'snick':dict(window,schedule_mode=bad)})

    def test_legacy_list_configuration_keeps_a_working_settings_editor(self):
        self.setup_special()
        item=self.config.data['seasonal']['windows']['snick']
        self.config.data['seasonal']['windows']=[dict(item,slug='snick')]
        snapshot=SettingsService(self.config).snapshot([])
        self.assertEqual(snapshot['windows']['snick']['schedule_mode'],'recurring')


if __name__=='__main__':unittest.main()

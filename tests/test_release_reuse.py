import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT / 'scripts'))
import release_reuse as reuse

spec = importlib.util.spec_from_file_location('publisher',PROJECT / 'scripts/publish-release.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ReleaseReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'app';self.output.mkdir()
        self.registry = self.root / 'registry.json'
        self.record = {'name':'Sources.zip','tag':'v1','url':'https://github.com/owner/repo/releases/download/v1/Sources.zip',
                       'sha256':'a'*64,'size':100}
        self.comparison = {'media':'media','native':'native','python_sources':'python','locks':{}}
        self.data = {'schema':1,'repository':'owner/repo',
                     'source_bundles':[{'compatibility':self.comparison,'assets':[self.record]}],
                     'media_packs':[{**self.record,'name':'Music.zip','url':self.record['url'].replace('Sources.zip','Music.zip')}]}
        self.registry.write_text(json.dumps(self.data))

    def test_unchanged_sources_reuse_but_any_dependency_change_rejects(self):
        with patch.object(reuse,'compatibility',return_value=self.comparison):
            result = reuse.source_reference(self.output,self.registry)
        self.assertEqual(result['sources'],[self.record])
        for key in ('media','native','python_sources','locks'):
            changed = {**self.comparison,key:'changed'}
            with patch.object(reuse,'compatibility',return_value=changed):
                self.assertIsNone(reuse.source_reference(self.output,self.registry))

    def test_missing_registry_falls_back_and_unknown_schema_stops(self):
        self.registry.unlink()
        self.assertIsNone(reuse.source_reference(self.output,self.registry))
        self.registry.write_text('{"schema":2}')
        with self.assertRaises(reuse.EvidenceError):reuse.source_reference(self.output,self.registry)

    def test_published_asset_must_match_hash_size_url_and_remain_available(self):
        remote = {'name':'Sources.zip','browser_download_url':self.record['url'],'digest':'sha256:'+'a'*64,'size':100}
        with patch.object(reuse,'asset_metadata',return_value={'Sources.zip':remote}):
            reuse.verify_remote([self.record],'owner/repo')
        for changed in ({},{'Sources.zip':{**remote,'digest':'sha256:'+'b'*64}},
                        {'Sources.zip':{**remote,'size':101}},
                        {'Sources.zip':{**remote,'browser_download_url':'https://example.com/wrong'}}):
            with patch.object(reuse,'asset_metadata',return_value=changed), self.assertRaises(reuse.EvidenceError):
                reuse.verify_remote([self.record],'owner/repo')

    def test_tampered_source_references_cannot_replace_the_registered_downloads(self):
        reference={'schema':1,'repository':'owner/repo','compatibility':self.comparison,'sources':[self.record]}
        path=self.output/'SOURCE-DOWNLOADS.json';path.write_text(json.dumps(reference))
        with patch.object(reuse,'source_reference',return_value=reference),patch.object(reuse,'verify_remote') as verify:
            self.assertEqual(reuse.validated_reference(self.output),reference);verify.assert_called_once()
            changed=copy.deepcopy(reference);changed['sources'][0]['url']='https://example.com/other'
            path.write_text(json.dumps(changed))
            with self.assertRaises(reuse.EvidenceError):reuse.validated_reference(self.output)

    def test_plan_uploads_new_files_and_links_sources_and_media_without_copying(self):
        windows=self.root/'Windows.zip';windows.write_bytes(b'app')
        materials=self.root/'Build-Materials.zip';materials.write_bytes(b'current recipes')
        with patch.object(reuse,'verify_remote') as verify:
            plan=reuse.write_plan(self.output,'new',[windows,materials],[self.record],self.registry)
        self.assertEqual([a['name'] for a in plan['upload']],['Windows.zip','Build-Materials.zip','CRTV-Sim-new-SHA256SUMS.txt'])
        self.assertEqual([a['name'] for a in plan['reuse']],['Sources.zip','Music.zip'])
        self.assertFalse((self.root/'Sources.zip').exists());self.assertFalse((self.root/'Music.zip').exists())
        verify.assert_called_once()
        checksum=(self.root/'CRTV-Sim-new-SHA256SUMS.txt').read_text()
        self.assertIn('a'*64+'  Sources.zip',checksum)

    def test_dead_source_link_prevents_release_plan(self):
        windows=self.root/'Windows.zip';windows.write_bytes(b'app')
        with patch.object(reuse,'verify_remote',side_effect=reuse.EvidenceError('missing')),self.assertRaises(reuse.EvidenceError):
            reuse.write_plan(self.output,'new',[windows],[self.record],self.registry)
        self.assertFalse((self.root/'RELEASE-DOWNLOADS.json').exists())

    def test_changed_media_pack_is_uploaded_instead_of_linking_the_old_copy(self):
        project=self.root/'project';local=project/'dist/Music.zip';local.parent.mkdir(parents=True)
        with zipfile.ZipFile(local,'w') as archive:archive.writestr('music.mp3',b'new track')
        local.with_suffix('.zip.sha256').write_text(reuse.sha256(local)+'  Music.zip\n')
        windows=self.root/'Windows.zip';windows.write_bytes(b'app')
        with patch.object(reuse,'PROJECT',project),patch.object(reuse,'verify_remote'):
            plan=reuse.write_plan(self.output,'new',[windows],[self.record],self.registry)
        self.assertIn('Music.zip',[a['name'] for a in plan['upload']])
        self.assertNotIn('Music.zip',[a['name'] for a in plan['reuse']])
        self.assertEqual((self.root/'Music.zip').read_bytes(),local.read_bytes())

    def frozen_fixture(self):
        (self.output/'CRTV-Sim.exe').write_bytes(b'app')
        (self.output/'CRTV-Sim-Safe-Stop.exe').write_bytes(b'safe-stop')
        (self.output/'_internal').mkdir()
        (self.output/'_internal/python.dll').write_bytes(b'runtime')
        with patch.object(reuse,'input_fingerprint',return_value='inputs'):
            reuse.record_frozen(self.output,'inputs')

    def test_compilation_reuse_requires_same_inputs_and_every_compiled_file(self):
        self.frozen_fixture();reuse.check_frozen(self.output,'inputs')
        with self.assertRaises(reuse.EvidenceError):reuse.check_frozen(self.output,'new source')
        (self.output/'_internal/python.dll').write_bytes(b'tampered')
        with self.assertRaises(reuse.EvidenceError):reuse.check_frozen(self.output,'inputs')

    def test_new_native_file_or_live_profile_prevents_compilation_reuse(self):
        self.frozen_fixture()
        extra=self.output/'_internal/extra.dll';extra.write_bytes(b'unapproved')
        with self.assertRaises(reuse.EvidenceError):reuse.check_frozen(self.output,'inputs')
        extra.unlink();(self.output/'user-data').mkdir()
        with self.assertRaises(reuse.EvidenceError):reuse.check_frozen(self.output,'inputs')

    def test_drift_during_compilation_prevents_receipt(self):
        with patch.object(reuse,'input_fingerprint',return_value='new'),self.assertRaises(reuse.EvidenceError):
            reuse.record_frozen(self.output,'old')
        self.assertFalse((self.output/'FROZEN-BUILD.json').exists())

    def test_changed_compiled_cache_cli_falls_back_instead_of_aborting(self):
        self.frozen_fixture()
        result=subprocess.run([sys.executable,str(PROJECT/'scripts/release_reuse.py'),'check-frozen',
                               '--output',str(self.output),'--fingerprint','new inputs'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('Traceback',result.stderr)

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'),'Windows PowerShell required')
    def test_build_reuses_compiled_files_without_invoking_pyinstaller(self):
        project=self.root/'project';scripts=project/'scripts';scripts.mkdir(parents=True)
        shutil.copy2(PROJECT/'scripts/build-portable-windows.ps1',scripts/'build-portable-windows.ps1')
        for name,content in [('pyproject.toml','version metadata'),('config/base.example.toml','[application]\n'),('docs/RELEASE_0.9.55.md','notes'),
                             ('assets/test.txt','asset'),('migrations/test.sql','sql'),
                             ('scripts/rollback-internet-media.ps1','helper'),('scripts/rollback-internet-media.cmd','helper'),
                             ('dist/cache/CRTV-Sim.exe','app'),('dist/cache/CRTV-Sim-Safe-Stop.exe','stop'),
                             ('dist/cache/_internal/runtime.dll','runtime')]:
            path=project/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        stub=scripts/'python.ps1'
        stub.write_text(r'''
            $global:LASTEXITCODE=0
            Add-Content -LiteralPath (Join-Path $PSScriptRoot 'calls.txt') -Value ($args -join '|')
            if ($args -contains 'PyInstaller') { throw 'Unexpected recompilation' }
            if ($args[0] -eq '-c') { Write-Output '0.9.55' }
            elseif ($args -contains 'fingerprint') { Write-Output 'inputs' }
        ''')
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
                               str(scripts/'build-portable-windows.ps1'),'-Python',str(stub),
                               '-Output','dist/candidate','-ReuseBuild','dist/cache','-SkipArchive'],
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertEqual((project/'dist/candidate/CRTV-Sim.exe').read_text(),'app')
        self.assertEqual((project/'dist/candidate/_internal/runtime.dll').read_text(),'runtime')
        calls=(scripts/'calls.txt').read_text()
        self.assertNotIn('PyInstaller',calls)
        self.assertIn('check-frozen',calls)
        self.assertIn('record-frozen',calls)
        self.assertIn('check-media-tools.py',calls)
        self.assertIn('smoke-distribution.py',calls)

    def plan_fixture(self):
        (self.output/'THIRD-PARTY-MANIFEST.json').write_text(json.dumps({'mode':'public candidate','problems':[]}))
        path=self.root/'Windows.zip';path.write_bytes(b'app')
        plan={'schema':1,'version':'1','repository':'owner/repo','output':'app',
              'upload':[{'name':path.name,'sha256':reuse.sha256(path),'size':path.stat().st_size}],
              'reuse':[self.record]}
        plan_path=self.root/'RELEASE-DOWNLOADS.json';plan_path.write_text(json.dumps(plan))
        proof={'schema':1,'version':'1','inputs':'inputs','media_checks':True,'zip_smoke_checks':True,
               'checks':{'media':'a'*64,'zip':'b'*64},'plan_sha256':reuse.sha256(plan_path)}
        (self.root/'RELEASE-VALIDATION.json').write_text(json.dumps(proof))
        return plan,proof

    def test_publication_reuses_verified_zip_and_rejects_stale_checks_or_changed_artifacts(self):
        plan,proof=self.plan_fixture()
        with patch.object(publisher,'input_fingerprint',return_value='inputs'),patch.object(publisher,'verify_remote'):
            self.assertEqual(publisher.checked_plan(self.root),plan)
            (self.root/'Windows.zip').write_bytes(b'changed')
            with self.assertRaises(reuse.EvidenceError):publisher.checked_plan(self.root)
        self.plan_fixture()
        with patch.object(publisher,'input_fingerprint',return_value='changed'),self.assertRaises(reuse.EvidenceError):
            publisher.checked_plan(self.root)

    def test_plan_edit_or_skipped_smoke_check_prevents_publication(self):
        plan,proof=self.plan_fixture()
        with patch.object(publisher,'input_fingerprint',return_value='inputs'):
            proof['zip_smoke_checks']=False
            (self.root/'RELEASE-VALIDATION.json').write_text(json.dumps(proof))
            with self.assertRaises(reuse.EvidenceError):publisher.checked_plan(self.root)
            self.plan_fixture();plan['reuse']=[]
            (self.root/'RELEASE-DOWNLOADS.json').write_text(json.dumps(plan))
            with self.assertRaises(reuse.EvidenceError):publisher.checked_plan(self.root)

    def test_internal_package_is_rejected_even_with_a_validation_receipt(self):
        self.plan_fixture()
        (self.output/'INTERNAL-BUILD-NOT-FOR-DISTRIBUTION.txt').write_text('internal')
        with patch.object(publisher,'input_fingerprint',return_value='inputs'),self.assertRaises(reuse.EvidenceError):
            publisher.checked_plan(self.root)

    def test_utf16_powershell_logs_are_read_correctly(self):
        log=self.root/'checks.log';log.write_text('PASS: media\n',encoding='utf-16')
        self.assertEqual(reuse.read_log(log),'PASS: media\n')

    def test_record_validation_requires_passed_logs(self):
        self.plan_fixture();media=self.root/'media.log';smoke=self.root/'smoke.log'
        media.write_text('failed');smoke.write_text('not run')
        with patch.object(reuse,'input_fingerprint',return_value='inputs'), \
                patch.object(reuse,'verify_output'),self.assertRaises(reuse.EvidenceError):
            reuse.record_validation(self.output,'inputs','1',media,smoke)

    def test_reuse_archive_creates_only_small_current_materials_and_download_links(self):
        spec=importlib.util.spec_from_file_location('reuse_preparation',PROJECT/'scripts/prepare-distribution.py')
        prep=importlib.util.module_from_spec(spec);spec.loader.exec_module(prep)
        reference={'schema':1,'repository':'owner/repo','sources':[self.record],'compatibility':self.comparison}
        (self.output/'THIRD-PARTY-MANIFEST.json').write_text(json.dumps({'mode':'public candidate','problems':[],'media':{}}))
        (self.output/'SOURCE-DOWNLOADS.json').write_text(json.dumps(reference))
        (self.output/'MICROSOFT-RUNTIME.json').write_text('{}')
        approved={p.name:reuse.sha256(p) for p in self.output.iterdir() if p.is_file()}
        (self.output/'PACKAGE-FILES.json').write_text(json.dumps(approved))
        (self.root/'Media/_channel_template/specials/holidays/halloween/over-night/episodes').mkdir(parents=True)
        with patch.object(prep,'verify_output'),patch.object(reuse,'validated_reference',return_value=reference), \
                patch.object(reuse,'verify_remote'):
            prep.archive(self.output,'new')
        self.assertFalse((self.root/'CRTV-Sim-new-ThirdParty-Sources.zip').exists())
        materials=self.root/'CRTV-Sim-new-Build-Materials.zip'
        with zipfile.ZipFile(materials) as archive:
            self.assertIn('release-recipes/scripts/release_reuse.py',archive.namelist())
            self.assertEqual(json.loads(archive.read('SOURCE-DOWNLOADS.json')),reference)
        plan=json.loads((self.root/'RELEASE-DOWNLOADS.json').read_text())
        self.assertEqual(len(plan['upload']),3)
        self.assertIn(self.record,plan['reuse'])

    def test_notes_point_to_existing_downloads_next_to_the_windows_link(self):
        plan,_=self.plan_fixture()
        body=publisher.release_body(plan,'# Release\nChanges\n## Downloads\nOld wrong links')
        self.assertIn(self.record['url'],body)
        self.assertIn('/download/v1/Windows.zip',body)
        self.assertNotIn('Old wrong links',body)

    def test_publisher_uploads_only_new_assets_and_keeps_visibility(self):
        plan,_=self.plan_fixture();calls=[];assets=[]
        release={'id':1,'tag_name':'v1','draft':True,'html_url':'https://github.com/owner/repo/releases/tag/v1'}
        class FakeGitHub:
            def api(self,path='',method='GET',value=None):
                calls.append((path,method))
                if path.startswith('/git/ref'):return {'object':{'type':'commit','sha':'head'}}
                if not path:return {'visibility':'public'}
                if path.startswith('/releases?'):return []
                if path.endswith('/assets?per_page=100'):return assets
                if method=='POST':return release
                if method=='PATCH':return {**release,'draft':False}
                raise AssertionError(path)
            def upload(self,release,path):
                self.path=path
                item={'name':path.name,'digest':'sha256:'+reuse.sha256(path),'size':path.stat().st_size}
                assets.append(item);return item
        github=FakeGitHub()
        with patch.object(publisher,'checked_plan',return_value=plan), \
                patch.object(publisher.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
                patch.object(publisher.subprocess,'check_output',return_value='head\n'):
            result=publisher.publish(self.root,plan,'notes',github)
        self.assertFalse(result['draft'])
        self.assertEqual([a['name'] for a in assets],['Windows.zip'])
        self.assertFalse(any(method=='DELETE' for _,method in calls))
        self.assertFalse(any(path=='' and method!='GET' for path,method in calls))


if __name__=='__main__':unittest.main()

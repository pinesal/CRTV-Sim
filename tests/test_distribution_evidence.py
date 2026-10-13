import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import windows_toolchain as tools


class DistributionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specification = importlib.util.spec_from_file_location('prepare_distribution_evidence', PROJECT / 'scripts/prepare-distribution.py')
        cls.preparation = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.preparation)

    def test_archive_paths_cannot_escape(self):
        for value in ("../secret", "/etc/passwd", "C:/secret", "a\\b", "a/../../b", ""):
            with self.subTest(value=value), self.assertRaises(tools.EvidenceError):
                tools.safe_relative(value)
        self.assertEqual(tools.safe_relative("source/code/PKGBUILD"), "source/code/PKGBUILD")

    def test_system_dll_allowlist_does_not_accept_arbitrary_system32_file(self):
        self.assertTrue(tools.system_import("KERNEL32.dll"))
        self.assertTrue(tools.system_import("api-ms-win-crt-runtime-l1-1-0.dll"))
        self.assertFalse(tools.system_import("third-party-installed-in-system32.dll"))
        self.assertFalse(tools.system_import("libx264-165.dll"))

    def test_missing_dll_fails_closure(self):
        with self.assertRaisesRegex(tools.EvidenceError, "libx264"):
            tools.closure("ffmpeg.exe", {"ffmpeg.exe": {"imports": ["libx264.dll"]}})

    def test_delay_imports_and_shared_dependencies_have_one_closure_entry(self):
        binaries = {"mpv.exe": {"imports": ["a.dll", "b.dll", "kernel32.dll"]}, "a.dll": {"imports": ["b.dll"]}, "b.dll": {"imports": []}}
        self.assertEqual(tools.closure("mpv.exe", binaries), {"mpv.exe", "a.dll", "b.dll"})

    def test_source_recipe_must_match_binary_build(self):
        recipe = b"build() { make; }\n"
        checksum = hashlib.sha256(recipe).hexdigest()
        package = {"base": "sample", "name": "sample", "version": "1-1", "recipe_sha256": checksum}
        members = {"sample/PKGBUILD": recipe, ".BUILDINFO": b"pkgbuild_sha256sum = " + b"0" * 64, ".PKGINFO": b"pkgname = sample\npkgver = 1-1\n"}
        with patch.object(tools, "tar_bytes", side_effect=lambda path, member: members[member]):
            with self.assertRaisesRegex(tools.EvidenceError, "recipe does not match"):
                tools.source_check(package, Path("source"), Path("binary"))

    def test_declared_source_checksum_and_missing_source_are_enforced(self):
        recipe, code = b"build() { make; }", b"actual source"
        checksum = hashlib.sha256(recipe).hexdigest()
        package = {"base": "sample", "name": "sample", "version": "1-1", "recipe_sha256": checksum}
        members = {"sample/PKGBUILD": recipe, ".BUILDINFO": f"pkgbuild_sha256sum = {checksum}".encode(), ".PKGINFO": b"pkgname = sample\npkgver = 1-1\n", "sample/.SRCINFO": f"source = https://example.com/code.tar.xz\nsha256sums = {hashlib.sha256(code).hexdigest()}".encode(), "sample/code.tar.xz": code}
        with patch.object(tools, "tar_bytes", side_effect=lambda path, member: members[member]), patch.object(tools, "tar_names", return_value=list(members)):
            self.assertEqual(tools.source_check(package, Path("source"), Path("binary")), [])
            members['sample/code.tar.xz'] = b"changed"
            with self.assertRaisesRegex(tools.EvidenceError, "checksum mismatch"):
                tools.source_check(package, Path("source"), Path("binary"))
        with patch.object(tools, "tar_bytes", side_effect=lambda path, member: members[member]), patch.object(tools, "tar_names", return_value=[]):
            with self.assertRaisesRegex(tools.EvidenceError, "Missing declared source"):
                tools.source_check(package, Path("source"), Path("binary"))

    def test_unresolved_source_cannot_be_public_even_if_internal_build_passes(self):
        binaries = {name: {"imports": []} for name in ("ffmpeg.exe", "ffprobe.exe", "mpv.exe")}
        lock = {"schema": 1, "packages": [], "binaries": binaries, "unresolved": ["Missing static library source"]}
        # No actual bytes need inspection in this focused gating fixture.
        for value in binaries.values():
            value.update(package="sample", path="bin/any", sha256="a" * 64)
        with patch.object(tools, "closure", return_value=set()), patch.object(tools, "fetch"):
            # A genuinely empty binary map is invalidated by closure in real use.
            lock['binaries'] = {}
            with self.assertRaisesRegex(tools.EvidenceError, "Public packaging blocked"):
                tools.verify(lock, Path("cache"))
            self.assertIn("INTERNAL", tools.verify(lock, Path("cache"), internal=True)['status'])

    def test_hash_corruption_fails(self):
        (PROJECT / '.test-tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder:
            path = Path(folder) / "ffmpeg.exe"
            path.write_bytes(b"approved")
            expected = tools.sha256(path)
            tools.check_hash(path, expected)
            path.write_bytes(b"unapproved")
            with self.assertRaises(tools.EvidenceError):
                tools.check_hash(path, expected)

    def test_removing_static_input_records_cannot_waive_source_requirement(self):
        from release_source_checks import verify_inputs
        package = {'name': 'sample', 'base': 'sample', 'binary': {'filename': 'sample.pkg.tar.zst'},
                   'source': {'filename': 'sample.src.tar.zst'}}
        lock = {'packages': [package], 'source_inputs': {'packages': [], 'cargo': [], 'upstream_notices': {}}}
        members = {'.BUILDINFO': b'installed = mingw-w64-ucrt-x86_64-dlfcn-1.4.2-1-any\n',
                   '.PKGINFO': b'depend = mingw-w64-ucrt-x86_64-dlfcn\n', 'sample/.SRCINFO': b''}
        with patch.object(tools, 'tar_bytes', side_effect=lambda path, member: members[member]):
            problems = verify_inputs(lock, Path('cache'), {})
        self.assertIn('Missing exact incorporated source: mingw-w64-ucrt-x86_64-dlfcn-1.4.2-1-any.pkg.tar.zst', problems)

    def test_removing_cargo_records_cannot_waive_recipe_fetch(self):
        from release_source_checks import verify_inputs
        package = {'name': 'sample', 'base': 'sample', 'notices': ['LICENSE'],
                   'binary': {'filename': 'sample.pkg.tar.zst'}, 'source': {'filename': 'sample.src.tar.zst'}}
        lock = {'packages': [package], 'source_inputs': {'packages': [], 'cargo': [], 'upstream_notices': {}}}
        members = {'.BUILDINFO': b'installed = compiler-1\n', '.PKGINFO': b'', 'sample/.SRCINFO': b'',
                   'sample/PKGBUILD': b'prepare() { cargo fetch --locked; }'}
        with patch.object(tools, 'tar_bytes', side_effect=lambda path, member: members[member]):
            problems = verify_inputs(lock, Path('cache'), {'sample': (package, Path('binary'), Path('source'))})
        self.assertIn('sample: matching locked Cargo vendor source is missing', problems)

    def runtime_fixture(self, root):
        from retrotv.runtime_terms import NOTICE
        notice = root / 'licenses/microsoft' / NOTICE
        notice.parent.mkdir(parents=True)
        notice.write_text('Microsoft runtime test terms', encoding='utf-8')
        (root / 'MICROSOFT-RUNTIME.json').write_text(json.dumps({'notices': {NOTICE: {'sha256': tools.sha256(notice)}}}), encoding='utf-8')
        return notice

    def test_runtime_acknowledgement_is_required_and_bound_to_terms(self):
        from retrotv.runtime_terms import accepted, acknowledge
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder:
            root = Path(folder)
            self.assertTrue(accepted(root))  # Existing source/development workflow.
            notice = self.runtime_fixture(root)
            self.assertFalse(accepted(root))
            acknowledge(root)
            self.assertTrue(accepted(root))
            notice.write_text('changed terms', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'missing or changed'):
                accepted(root)

    def test_setup_cannot_complete_without_runtime_consent(self):
        from retrotv.onboarding import SetupService
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder:
            root = Path(folder)
            self.runtime_fixture(root)
            media = root / 'Media'
            media.mkdir()
            service = SetupService(root)
            values = {'media': str(media), 'timezone': 'UTC'}
            with self.assertRaisesRegex(ValueError, 'Accept the bundled Microsoft'):
                service.save(values)
            self.assertFalse((root / 'user-data/config').exists())
            service.save({**values, 'runtime_terms': True})
            from retrotv.runtime_terms import accepted
            self.assertTrue(accepted(root))

    def test_build_tools_alone_cannot_authorize_microsoft_distribution(self):
        import microsoft_runtime
        with patch.object(microsoft_runtime, 'licensed_visual_studio', return_value=[]):
            with self.assertRaisesRegex(tools.EvidenceError, 'Build Tools alone'):
                microsoft_runtime.stage(Path('unused'))

    def test_missing_or_changed_source_companion_blocks_packaging(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder:
            root = Path(folder)
            extra = root / 'Build-Inputs.zip'
            with zipfile.ZipFile(extra, 'w') as archive:
                archive.writestr('source/code', b'complete source')
            manifest = {'media': {}, 'sources': {}, 'additional_archives': {extra.name: {
                'sha256': tools.sha256(extra), 'sources': {'source/code': {'sha256': hashlib.sha256(b'complete source').hexdigest()}}}}}
            self.assertEqual(tools.verify_source_companions(root/'Sources.zip', manifest), [extra])
            extra.write_bytes(b'changed')
            with self.assertRaises(tools.EvidenceError):
                tools.verify_source_companions(root/'Sources.zip', manifest)
            extra.unlink()
            with self.assertRaises((tools.EvidenceError, FileNotFoundError)):
                tools.verify_source_companions(root/'Sources.zip', manifest)

    def test_dropping_companion_manifest_cannot_waive_required_inputs(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder:
            root = Path(folder)
            lock_path = root/'lock.json'
            lock_path.write_text(json.dumps({'packages': [], 'source_inputs': {
                'packages': [{'source': {'filename': 'required-static-source.tar.zst', 'sha256': 'a'*64}}], 'cargo': []}}))
            manifest = {'media': {'lock_sha256': 'pinned'}, 'sources': {}}
            with patch.object(tools, 'LOCK', lock_path), self.assertRaisesRegex(tools.EvidenceError, 'coverage differs'):
                tools.verify_source_companions(root/'Sources.zip', manifest)

    def test_fields_preserve_empty_values(self):
        self.assertEqual(tools.fields("\tlicense = \nversion = 1\n"), {"license": [""], "version": ["1"]})

    def test_public_build_does_not_discover_media_on_path(self):
        script = (PROJECT / "scripts/build-portable-windows.ps1").read_text()
        self.assertNotIn("Get-Command ffmpeg", script)
        self.assertNotIn("Get-Command mpv", script)
        self.assertIn("windows_toolchain.py", script)
        self.assertIn("InternalBuild", script)

    def candidate(self, folder, *, internal=False):
        output = Path(folder) / 'app'
        output.mkdir()
        (output / 'LICENSE').write_text('test notice')
        manifest = {'mode': 'INTERNAL' if internal else 'public candidate', 'problems': [], 'media': {'test': 'mapping'}}
        (output / 'THIRD-PARTY-MANIFEST.json').write_text(json.dumps(manifest))
        (output / 'PACKAGE-FILES.json').write_text(json.dumps({p.name: tools.sha256(p) for p in output.iterdir() if p.is_file()}))
        source = output.parent / ('CRTV-Sim-test' + ('-INTERNAL' if internal else '') + '-ThirdParty-Sources.zip')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('SOURCE-MANIFEST.json', json.dumps({'media': manifest['media'], 'sources': {}}))
        return output, source

    def test_unknown_output_file_and_live_user_data_cannot_be_archived(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder, patch.object(self.preparation, 'verify_output'):
            output, _ = self.candidate(folder)
            (output / 'user-data').mkdir()
            (output / 'user-data/database.sqlite3').write_bytes(b'private')
            with self.assertRaisesRegex(tools.EvidenceError, 'Unapproved'):
                self.preparation.archive(output, 'test')

    def test_modified_package_file_cannot_be_archived(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder, patch.object(self.preparation, 'verify_output'):
            output, _ = self.candidate(folder)
            (output / 'LICENSE').write_text('changed')
            with self.assertRaisesRegex(tools.EvidenceError, 'changed after validation'):
                self.preparation.archive(output, 'test')

    def test_source_mapping_mismatch_prevents_archive(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder, patch.object(self.preparation, 'verify_output'):
            output, source = self.candidate(folder)
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('SOURCE-MANIFEST.json', json.dumps({'media': {'different': 'binary'}, 'sources': {}}))
            with self.assertRaisesRegex(tools.EvidenceError, 'mapping differs'):
                self.preparation.archive(output, 'test')

    def test_internal_artifact_cannot_be_relabelled_public(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.test-tmp') as folder, patch.object(self.preparation, 'verify_output'):
            output, _ = self.candidate(folder, internal=True)
            with self.assertRaisesRegex(tools.EvidenceError, 'internal/incomplete'):
                self.preparation.archive(output, 'test')


if __name__ == "__main__":
    unittest.main()

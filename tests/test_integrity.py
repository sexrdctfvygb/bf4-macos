# SPDX-License-Identifier: MIT
import io
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from bf4 import configure_game, configure_reshade, content_ids, ea_directory, environment, ensure_redists, install_legacy_effects, isolate_user_links, languages, load_config, native_effect_compiler, preferred_language, start_ea, windows_path
from bootstrap import configure_vulkan, extract
from frame_ring import FrameRing, HEADER, RECORD
from privacy_scan import inspect_bytes, scan_git, scan_archive, publishable
from telemetry import Metrics


class Statistics(unittest.TestCase):
    def test_low_is_mean_of_slowest_frames_and_keeps_real_stall(self):
        m = Metrics()
        for _ in range(999): m.add(5)
        m.add(100)
        s = m.snapshot()
        self.assertAlmostEqual(s['avg_fps'], 1000000 / 5095)
        self.assertAlmostEqual(s['low_1_fps'], 1000 / 14.5)
        self.assertEqual(s['low_01_fps'], 10)
        self.assertEqual(s['max_frame_ms'], 100)
        self.assertEqual((s['over_16_7_ms'], s['over_33_3_ms'], s['over_50_ms']), (1, 1, 1))
        self.assertAlmostEqual(s['current_fps'], 120000 / (119 * 5 + 100))

    def test_low_rounds_count_up_and_stays_preliminary(self):
        m = Metrics()
        for _ in range(1000): m.add(4)
        m.add(2000)
        s = m.snapshot()
        self.assertAlmostEqual(s['low_01_fps'], 1000 / 1002)
        self.assertTrue(s['low_01_preliminary'])
        self.assertEqual(s['max_frame_ms'], 2000)

    def test_nonfinite_and_nonpositive_rejected(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError): Metrics().add(value)


class Isolation(unittest.TestCase):
    def test_clean_game_uses_its_own_standard_profile_and_fps(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder); game = data / 'prefix/drive_c/game'; game.mkdir(parents=True)
            self.assertIsNone(load_config(data)['fps_limit'])
            with patch('bf4.game_directory', return_value=game), patch('bf4.languages', return_value=['en_US']), \
                 patch('bf4.preferred_language', return_value='en_US'), patch('bf4.reg'), patch('bf4.configure_reshade'):
                configure_game(data)
            self.assertFalse((game / 'user.cfg').exists())
            self.assertEqual(list((data / 'prefix').rglob('PROFSAVE*')), [])

    def test_explicit_fps_limit_preserves_existing_controls_and_graphics(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder); game = data / 'prefix/drive_c/game'; game.mkdir(parents=True)
            profile = data / 'prefix/drive_c/bf4-user/Documents/Battlefield 4/settings/PROFSAVE_profile'
            profile.parent.mkdir(parents=True); profile.write_bytes(b'player-owned controls and graphics\n')
            cfg = game / 'user.cfg'; before = 'RenderDevice.VSyncEnable 1\nGameTime.MaxVariableFps 200\nUser.Command 7\n'
            cfg.write_text(before)
            config = load_config(data); config.update(language='en_US', fps_limit=400)
            (data / 'config.json').write_text(json.dumps(config))
            with patch('bf4.game_directory', return_value=game), patch('bf4.languages', return_value=['en_US']), \
                 patch('bf4.reg'), patch('bf4.configure_reshade'):
                configure_game(data)
                configured = cfg.read_bytes(); configure_game(data)
            self.assertEqual(profile.read_bytes(), b'player-owned controls and graphics\n')
            self.assertEqual(cfg.read_bytes(), configured)
            self.assertIn('RenderDevice.VSyncEnable 1\n', cfg.read_text())
            self.assertIn('User.Command 7\n', cfg.read_text())
            self.assertEqual(cfg.read_text().count('GameTime.MaxVariableFps '), 1)
            self.assertEqual((data / 'user-cfg.backup').read_text(), before)

    def test_effect_compiler_rejects_builtin_wrong_arch_and_external_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'private'
            compiler = data / 'prefix/drive_c/windows/system32/d3dcompiler_43.dll'
            compiler.parent.mkdir(parents=True)
            self.assertFalse(native_effect_compiler(data))
            header = bytearray(256); header[:2] = b'MZ'
            struct.pack_into('<I', header, 0x3c, 256)
            compiler.write_bytes(header + b'PE\x00\x00\x64\x86')
            self.assertTrue(native_effect_compiler(data))
            header[80:96] = b'Wine builtin DLL'
            compiler.write_bytes(header + b'PE\x00\x00\x64\x86')
            self.assertFalse(native_effect_compiler(data))
            header[80:96] = b'\x00' * 16
            compiler.write_bytes(header + b'PE\x00\x00\x4c\x01')
            self.assertFalse(native_effect_compiler(data))
            compiler.unlink()
            external = Path(folder) / 'external.dll'
            external.write_bytes(header + b'PE\x00\x00\x64\x86')
            compiler.symlink_to(external)
            self.assertFalse(native_effect_compiler(data))

    def test_effects_missing_compiler_does_not_modify_game_or_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'private'; game = Path(folder) / 'game'
            data.mkdir(); game.mkdir()
            dll = game / 'dxgi.dll'; dll.write_bytes(b'user-owned')
            with patch('bf4.game_directory', return_value=game), \
                 patch('bf4.load_config', return_value={'reshade': True}), patch('bf4.reg') as registry:
                with self.assertRaisesRegex(RuntimeError, 'redists'):
                    configure_reshade(data)
                self.assertEqual(dll.read_bytes(), b'user-owned')
                registry.assert_not_called()

    def test_custom_preset_survives_effects_toggles_and_repeated_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); data = root / 'private'; game = root / 'game'; project = root / 'project'
            game.mkdir(); (data / 'cache').mkdir(parents=True); (project / 'assets').mkdir(parents=True)
            compiler = data / 'prefix/drive_c/windows/system32/d3dcompiler_43.dll'
            compiler.parent.mkdir(parents=True)
            header = bytearray(256); header[:2] = b'MZ'
            struct.pack_into('<I', header, 0x3c, 256)
            compiler.write_bytes(header + b'PE\x00\x00\x64\x86')
            (project / 'assets/PortableSweetFX.ini').write_text('generic preset')
            (project / 'assets/ReShadeUI.fxh').write_text('header')
            (project / 'assets/WorkingSweetFX.fx').write_text('fixture adapter')
            custom = 'user-edited preset\n'
            preset = game / 'PortableSweetFX.ini'; preset.write_text(custom)
            effects = data / 'effects/SweetFX-fixture/Shaders/SweetFX'; effects.mkdir(parents=True)
            (effects / 'Curves.fx').write_text('fixture shader')
            textures = data / 'effects/SweetFX-fixture/Textures/SweetFX'; textures.mkdir(parents=True)
            for name in ('Layer.png', 'AreaTex.png', 'SearchTex.png'):
                (textures / name).write_bytes(b'fixture texture: ' + name.encode())
            headers = data / 'effects-headers/reshade-shaders-fixture/Shaders'; headers.mkdir(parents=True)
            (headers / 'ReShade.fxh').write_text('fixture header')
            blob = b'fixture-owned-dll'; installer = root / 'installer.zip'
            with zipfile.ZipFile(installer, 'w') as z: z.writestr('ReShade64.dll', blob)
            legacy = root / 'legacy.tar'; legacy_hashes = {}
            with tarfile.open(legacy, 'w') as archive:
                for name in ('LumaSharpen.h', 'LiftGammaGain.h', 'Curves.h', 'Dither.h'):
                    content = ('fixture ' + name).encode()
                    info = tarfile.TarInfo('SweetFX/Shaders/' + name); info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                    legacy_hashes[name] = hashlib.sha256(content).hexdigest()
            lock = {'downloads': {'reshade': {}, 'reshade_headers': {}, 'sweetfx_legacy': {'fixture': 'legacy'}},
                    'legacy_headers': legacy_hashes,
                    'reshade_dll_sha256': hashlib.sha256(blob).hexdigest(),
                    'sweetfx_commit': 'fixture', 'reshade_headers_commit': 'fixture'}
            (project / 'dependencies.lock.json').write_text(json.dumps(lock))
            config = {'reshade': True}
            with patch('bf4.PROJECT', project), patch('bf4.game_directory', return_value=game), \
                 patch('bf4.load_config', return_value=config), patch('bf4.reg') as registry, \
                 patch('bootstrap.fetch', side_effect=lambda item, cache: legacy if item.get('fixture') == 'legacy' else installer):
                configure_reshade(data)
                self.assertEqual(preset.read_text(), custom)
                self.assertEqual((game / 'dxgi.dll').read_bytes(), blob)
                for texture in textures.iterdir():
                    self.assertEqual((game / 'reshade-shaders' / texture.name).read_bytes(), texture.read_bytes())
                ini = game / 'ReShade.ini'
                self.assertIn('TutorialProgress=4', ini.read_text())
                custom_ini = ini.read_text() + '\n; user overlay preferences\n'
                ini.write_text(custom_ini)
                self.assertIn(((data, r'HKCU\Software\Wine\AppDefaults\bf4.exe\DllOverrides', 'd3dcompiler_47', ''), {}), registry.call_args_list)
                self.assertIn(((data, r'HKCU\Software\Wine\AppDefaults\bf4.exe\DllOverrides', 'd3dcompiler_43', 'native,builtin'), {}), registry.call_args_list)
                configure_reshade(data)
                self.assertEqual(preset.read_text(), custom)
                config['reshade'] = False; configure_reshade(data)
                self.assertFalse((game / 'dxgi.dll').exists())
                self.assertEqual(registry.call_args_list[-2].args[-2:], ('d3dcompiler_47', 'builtin'))
                self.assertEqual(registry.call_args_list[-1].args[-2:], ('d3dcompiler_43', 'builtin'))
                self.assertEqual(preset.read_text(), custom)
                config['reshade'] = True; configure_reshade(data)
                self.assertEqual(preset.read_text(), custom)
                self.assertEqual(ini.read_text(), custom_ini)

    def test_prerequisites_install_once_and_failed_update_does_not_mark_success(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder); game = data / 'prefix/drive_c/game'
            dx = game / '__Installer/directx/redist/DXSETUP.exe'
            vc = game / '__Installer/vc/x64/vcredist_x64.exe'
            for p in (dx, vc): p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'fixture exe')
            with patch('bf4.game_directory', return_value=game), patch('bf4.native_effect_compiler', return_value=True), \
                 patch('bf4.wine') as execute:
                execute.return_value.returncode = 0
                ensure_redists(data); self.assertEqual(execute.call_count, 2)
                ensure_redists(data); self.assertEqual(execute.call_count, 2)
                marker = data / 'redists-installed.json'; original = marker.read_bytes()
                vc.write_bytes(b'updated fixture')
                execute.return_value.returncode = 1
                with self.assertRaisesRegex(RuntimeError, 'prerequisite'): ensure_redists(data)
                self.assertEqual(marker.read_bytes(), original)
                execute.return_value.returncode = 3010
                ensure_redists(data)
                self.assertNotEqual(marker.read_bytes(), original)

    def test_legacy_shader_hash_failure_does_not_install_partial_set(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); archive = root / 'legacy.tar'; game = root / 'game'; game.mkdir()
            hashes = {}
            with tarfile.open(archive, 'w') as output:
                for name in ('LumaSharpen.h', 'LiftGammaGain.h', 'Curves.h', 'Dither.h'):
                    content = name.encode(); info = tarfile.TarInfo('SweetFX/Shaders/' + name); info.size = len(content)
                    output.addfile(info, io.BytesIO(content)); hashes[name] = hashlib.sha256(content).hexdigest()
            lock = {'downloads': {'sweetfx_legacy': {}}, 'legacy_headers': hashes}
            lock['legacy_headers']['Dither.h'] = '0' * 64
            with patch('bootstrap.fetch', return_value=archive):
                with self.assertRaisesRegex(RuntimeError, 'hash mismatch'): install_legacy_effects(root, game, lock)
            self.assertFalse((game / 'reshade-shaders/SweetFX/Legacy').exists())

    def test_language_auto_selection_is_limited_to_installed_packages(self):
        with patch('bf4.subprocess.run') as execute:
            execute.return_value.stdout = '("ru-RU", "en-US")'
            self.assertEqual(preferred_language(['en_US', 'pl_PL', 'ru_RU']), 'ru_RU')
            self.assertEqual(preferred_language(['en_US', 'pl_PL']), 'en_US')
            self.assertEqual(preferred_language(['pl_PL']), 'pl_PL')

    def test_ea_session_lookup_cannot_reuse_another_prefix(self):
        with tempfile.TemporaryDirectory(prefix='bf4 space ') as folder:
            data = Path(folder)
            ea = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop/EA Desktop'
            ea.mkdir(parents=True)
            command = windows_path(data, ea / 'EADesktop.exe')
            with patch('bf4.reg'), patch('bf4.stop_session') as stop, patch('bf4.processes', return_value=[(10, command), (20, command)]), \
                 patch('bf4.belongs_to_prefix', side_effect=lambda d, pid: pid == 20), \
                 patch('bf4.wine') as execute:
                self.assertEqual(start_ea(data, ea), 20)
                execute.assert_not_called()
                stop.assert_not_called()
            with patch('bf4.reg'), patch('bf4.stop_session') as stop, patch('bf4.processes', return_value=[(10, command)]), \
                 patch('bf4.belongs_to_prefix', return_value=False), \
                 patch('bf4.wine') as execute:
                execute.return_value.pid = 30
                self.assertEqual(start_ea(data, ea), 30)
                stop.assert_called_once_with(data)
                self.assertEqual(execute.call_args.args[1], [command, '--in-process-gpu', '--log-level=1', '--no-sandbox'])

    def test_nested_host_folder_links_are_isolated_without_deleting_host_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); data = root / 'private'; external = root / 'external'
            external.mkdir(); (external / 'keep.txt').write_text('keep')
            link = data / 'prefix/drive_c/users/test/AppData/Roaming/Microsoft/Windows/Templates'
            link.parent.mkdir(parents=True); link.symlink_to(external, target_is_directory=True)
            isolate_user_links(data)
            self.assertFalse(link.is_symlink()); self.assertTrue(link.is_dir())
            self.assertEqual((external / 'keep.txt').read_text(), 'keep')

    def test_vulkan_manifest_still_resolves_after_moving_private_data(self):
        with tempfile.TemporaryDirectory(prefix='bf4 space ') as folder:
            root = Path(folder); data = root / 'before'
            framework = data / 'frameworks'; framework.mkdir(parents=True)
            (framework / 'libvulkan_kosmickrisp.dylib').touch()
            configure_vulkan(data)
            moved = root / 'after'; data.rename(moved)
            manifest = moved / 'frameworks/vulkan/icd.d/kosmickrisp_mesa_icd.json'
            library = json.loads(manifest.read_text())['ICD']['library_path']
            self.assertFalse(Path(library).is_absolute())
            self.assertEqual((manifest.parent / library).resolve(),
                             (moved / 'frameworks/libvulkan_kosmickrisp.dylib').resolve())

    def test_environment_overwrites_foreign_wine_and_has_independent_monitor_option(self):
        with tempfile.TemporaryDirectory(prefix='bf4 space ') as folder:
            data = Path(folder)
            c = {'monitoring': False}
            with patch.dict(os.environ, {'WINEPREFIX': '/unrelated-prefix', 'DXMT_CONFIG': 'foreign', 'WINEMSYNC': '0'}):
                env = environment(data, c)
            self.assertEqual(env['WINEPREFIX'], str(data / 'prefix'))
            self.assertEqual(env['WINEMSYNC'], '1')
            self.assertEqual(env['DXMT_BF4_FRAME_CAPTURE'], '0')
            self.assertNotIn('DXMT_CONFIG', env)
            self.assertIn('BF4_EXPECTED_EXE', env)

    def test_paths_with_spaces_and_rejects_host_path(self):
        with tempfile.TemporaryDirectory(prefix='bf4 space ') as folder:
            data = Path(folder)
            p = data / 'prefix/drive_c/Program Files/Game/bf4.exe'
            self.assertEqual(windows_path(data, p), r'C:\Program Files\Game\bf4.exe')
            with self.assertRaises(ValueError): windows_path(data, Path('/tmp/foreign.exe'))

    def test_ea_rolling_version_is_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            base = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop'
            for version in ('13.100.0.0', '13.999.1.2-12345'):
                child = base / version / 'EA Desktop'; child.mkdir(parents=True)
                for n in ('EALauncher.exe', 'EADesktop.exe', 'EABackgroundService.exe'): (child / n).touch()
            self.assertEqual(ea_directory(data).parent.name, '13.999.1.2-12345')

    def test_ea_updater_alias_is_retargeted_without_pinning_an_old_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            base = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop'
            for version in ('13.999.1.2-100', '13.999.1.2-200'):
                child = base / version / 'EA Desktop'; child.mkdir(parents=True)
                for name in ('EALauncher.exe', 'EADesktop.exe', 'EABackgroundService.exe'):
                    (child / name).touch()
            alias = base / 'EA Desktop'
            alias.symlink_to('13.999.1.2-100/EA Desktop', target_is_directory=True)
            latest = ea_directory(data)
            self.assertEqual(latest.parent.name, '13.999.1.2-200')
            with patch('bf4.reg'), patch('bf4.stop_session'), patch('bf4.processes', return_value=[]), patch('bf4.wine'):
                start_ea(data, latest)
            self.assertEqual(alias.resolve(), latest.resolve())
            self.assertFalse(Path(os.readlink(alias)).is_absolute())

    def test_ea_directory_rejects_an_external_prefix_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); data = root / 'private'; external = root / 'external'
            external.mkdir()
            for name in ('EALauncher.exe', 'EADesktop.exe', 'EABackgroundService.exe'):
                (external / name).touch()
            alias = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop/EA Desktop'
            alias.parent.mkdir(parents=True); alias.symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError): ea_directory(data)

    def test_content_ids_and_languages_come_from_public_game_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            game = Path(folder)
            (game / '__Installer').mkdir(); (game / 'Data/Win32/Loc').mkdir(parents=True)
            (game / '__Installer/installerdata.xml').write_text('<game><contentID>BF4-PC</contentID><contentID>bad &amp; data</contentID></game>')
            for n in ('ru', 'pl', 'invalid'):
                for extension in ('.toc', '.sb'): (game / ('Data/Win32/Loc/' + n + extension)).touch()
            self.assertEqual(content_ids(game), ['BF4-PC'])
            self.assertEqual(languages(game), ['pl_PL', 'ru_RU'])


class ArchivesAndPrivacy(unittest.TestCase):
    def test_archive_rejects_parent_traversal_and_absolute_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, name in enumerate(('../escape', 'valid/link')):
                archive = root / ('bad%d.tar' % index)
                with tarfile.open(archive, 'w') as t:
                    m = tarfile.TarInfo(name)
                    if index:
                        m.type = tarfile.SYMTYPE; m.linkname = '/etc/passwd'; t.addfile(m)
                    else:
                        m.size = 1; t.addfile(m, io.BytesIO(b'x'))
                with self.assertRaises(ValueError): extract(archive, root / 'output')

    def test_own_home_detected_without_exposing_value(self):
        findings = inspect_bytes(str(Path.home()).encode(), 'opaque-test-id')
        self.assertTrue(any(f['category'] == 'local-home' for f in findings))
        self.assertNotIn(str(Path.home()), json.dumps(findings))

    def test_deleted_secret_is_still_detected_in_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.run(['git', '-C', str(root)] + list(args), check=True, capture_output=True)
            git('init', '-q')
            git('config', 'user.name', 'BF4 Portable contributors')
            git('config', 'user.email', 'contributors@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            secret = root / 'sample.txt'
            secret.write_text('password=' + 'x' * 48)
            git('add', '.'); git('commit', '-qm', 'temporary fixture')
            secret.unlink(); git('add', '-u'); git('commit', '-qm', 'remove fixture')
            _, failures = scan_git(root)
            self.assertTrue(any(x['category'] == 'credential' for x in failures))
            self.assertNotIn('x' * 48, json.dumps(failures))

    def test_publication_allowlist_rejects_unexpected_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'config.json').write_text('{}')
            with self.assertRaises(ValueError): publishable(root)

    def test_publication_rejects_nested_extra_files_and_directory_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); public = root / 'public'; public.mkdir()
            source = public / 'src'; source.mkdir()
            extra = source / 'auth.json'; extra.write_text('{}')
            with self.assertRaises(ValueError): publishable(public)
            extra.unlink(); source.rmdir()
            external = root / 'private'; external.mkdir()
            (external / 'bf4.py').write_text('private')
            source.symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError): publishable(public)

    def test_other_home_path_and_email_are_reported_without_values(self):
        fixture = b'/Users/' + b'another-user/Documents/private email=' + b'person@' + b'private-domain.invalid'
        findings = inspect_bytes(fixture, 'opaque-test-id')
        self.assertEqual({x['category'] for x in findings}, {'personal-path', 'personal-email'})
        self.assertNotIn('another-user', json.dumps(findings))
        self.assertNotIn('private-domain', json.dumps(findings))
        self.assertEqual(inspect_bytes(b'LumaSharpen@LumaSharpen.fx,Curves@Curves.fx', 'preset'), [])

    def test_archive_rejects_extra_private_json_and_zip_links(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'source.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('bf4-macos/src/auth.json', '{}')
                link = zipfile.ZipInfo('bf4-macos/README.md')
                link.external_attr = 0o120777 << 16
                z.writestr(link, '../private')
            _, failures = scan_archive(archive)
            self.assertTrue({'unexpected-archive-member', 'archive-link'} <= {f['category'] for f in failures})


class SharedRing(unittest.TestCase):
    def test_atomic_records_and_overrun_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'telemetry.bin'
            capacity = 65536
            blob = bytearray(HEADER.size + capacity * RECORD.size)
            HEADER.pack_into(blob, 0, b'BF4FT02\0', 2, capacity, 1, RECORD.size, 0, 1000, 1)
            path.write_bytes(blob)
            ring = FrameRing(path); self.assertEqual(ring.read(), [])
            with path.open('r+b') as f:
                f.seek(HEADER.size); f.write(RECORD.pack(1, 5, 5, 3, 0))
                f.seek(24); f.write(struct.pack('<q', 1))
            self.assertEqual(ring.read(), [(1, 5, 5.0, 3)])
            self.assertEqual(ring.read(), [])
            with path.open('r+b') as f:
                f.seek(24); f.write(struct.pack('<q', capacity + 3))
            ring.read(); self.assertGreater(ring.missed, 0); self.assertGreater(ring.torn, 0)
            ring.close()


if __name__ == '__main__': unittest.main()

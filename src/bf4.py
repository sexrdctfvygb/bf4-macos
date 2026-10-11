#!/usr/bin/env python3
"""BF4 Portable. Private state stays outside the source tree; no account automation."""
# SPDX-License-Identifier: MIT
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
DEFAULT_DATA = Path.home() / 'Library/Application Support/BF4 Portable'
GAME_RELATIVE = Path('drive_c/Program Files/EA Games/Battlefield 4')


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def load_config(data):
    path = data / 'config.json'
    return json.loads(path.read_text()) if path.exists() else {
        'schema': 1, 'language': 'auto', 'monitoring': True,
        'mouse_fix': True, 'reshade': True, 'fps_limit': None}


def environment(data, config=None):
    config = config or load_config(data)
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ('WINE', 'DXMT_', 'DXVK_', 'CX_', 'DYLD_', 'VK_'))}
    prefix = data / 'prefix'
    framework = data / 'frameworks'
    renderer = data / 'renderer/wine'
    wine = data / 'wine'
    env.update({
        'WINEPREFIX': str(prefix), 'CX_ROOT': str(wine), 'SikarugirAppWine11': '1',
        'DYLD_LIBRARY_PATH': str(framework), 'DYLD_FALLBACK_LIBRARY_PATH': str(framework),
        'VK_DRIVER_FILES': str(framework / 'vulkan/icd.d/kosmickrisp_mesa_icd.json'),
        'WINEDLLPATH': str(wine / 'lib/wine'),
        'WINEDLLPATH_DXMT': str(renderer), 'WINEDLLPATH_DXVK': str(renderer),
        'WINEDLLPATH_D9VK': str(framework / 'renderer/d9vk/wine'),
        'WINEDLLOVERRIDES': 'd3d11,dxgi,d3d10core=n,b',
        'WINEESYNC': '1', 'WINEMSYNC': '1', 'WINEDEBUG': '-all',
        'DXMT_ALLOW_CROSS_PROCESS_SWAPCHAIN': '1',
        'DXMT_BF4_FRAME_CAPTURE': r'C:\bf4-tools\telemetry.bin' if config['monitoring'] else '0',
        'BF4_EXPECTED_EXE': r'C:\Program Files\EA Games\Battlefield 4\bf4.exe',
    })
    game = game_directory(data)
    if game: env['BF4_EXPECTED_EXE'] = windows_path(data, game / 'bf4.exe')
    return env


def wine_executable(data):
    return data / 'wine/lib/wine/x86_64-unix/wine'


def wine(data, args, wait=True, timeout=None, log='wine.log', cwd=None, env=None):
    runtime = data / 'runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime / log).open('ab') as output:
        command = [str(wine_executable(data))] + [str(x) for x in args]
        if wait:
            return subprocess.run(command, env=env or environment(data), cwd=cwd,
                                  stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        return subprocess.Popen(command, env=env or environment(data), cwd=cwd,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def reg(data, key, name, value, kind='REG_SZ'):
    result = wine(data, ['reg', 'add', key, '/v', name, '/t', kind, '/d', str(value), '/f'], timeout=30)
    if result.returncode: raise RuntimeError('Wine registry update failed')


def windows_path(data, path):
    path = Path(path).resolve()
    drive = (data / 'prefix/drive_c').resolve()
    if not path.is_relative_to(drive):
        raise ValueError('The game must be installed or copied into this private prefix')
    return 'C:\\' + str(path.relative_to(drive)).replace('/', '\\')


def isolate_user_links(data):
    drive = (data / 'prefix/drive_c').resolve()
    profiles = drive / 'users'
    if not profiles.is_dir(): return
    for link in list(profiles.rglob('*')):
        if link.is_symlink() and not link.resolve().is_relative_to(drive):
            if link.is_file():
                raise RuntimeError('Unexpected external user file link in the private prefix')
            link.unlink()
            link.mkdir()


def initialize_prefix(data, refresh=False):
    prefix = data / 'prefix'
    if prefix.exists():
        if not (data / 'prefix-initialized.json').exists():
            raise RuntimeError('Incomplete prefix. Inspect private logs; choose a new empty data directory to retry safely.')
        if not refresh: return
    else:
        prefix.mkdir()
    result = wine(data, ['wineboot', '-u'], timeout=180, log='prefix-setup.log')
    if result.returncode: raise RuntimeError('Wineboot failed; see private prefix-setup.log')
    # Wine also creates nested Templates links; isolate all generated host folder links.
    isolate_user_links(data)
    local = prefix / 'drive_c/bf4-user'
    folders = {'Personal': 'Documents', 'Desktop': 'Desktop', 'My Pictures': 'Pictures',
               'My Music': 'Music', 'My Video': 'Videos', 'Downloads': 'Downloads',
               'Templates': 'Templates'}
    for name, folder in folders.items():
        (local / folder).mkdir(parents=True, exist_ok=True)
        for key in (r'HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders',
                    r'HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'):
            reg(data, key, name, 'C:\\bf4-user\\' + folder)
    (prefix / 'drive_c/bf4-tools').mkdir(exist_ok=True)
    reg(data, r'HKCU\Software\Wine', 'Version', 'win10')
    atomic_json(data / 'prefix-initialized.json', {'schema': 1, 'created_unix': time.time(),
                                                  'isolated_user_folders': True})


def ea_directory(data):
    desktop = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop'
    required = ('EALauncher.exe', 'EADesktop.exe', 'EABackgroundService.exe')
    canonical = desktop / 'EA Desktop'
    drive = (data / 'prefix/drive_c').resolve()
    if canonical.is_symlink() and not canonical.resolve().is_relative_to(drive):
        raise ValueError('EA directory points outside this private prefix')
    # EA can install a real unversioned directory. Our compatibility alias must
    # not pin discovery to an older payload after the updater installs a new one.
    if not canonical.is_symlink() and all((canonical / n).is_file() for n in required):
        return canonical
    choices = []
    if desktop.exists():
        for p in desktop.iterdir():
            m = re.fullmatch(r'(\d+\.\d+\.\d+\.\d+)(?:-\d+)?', p.name)
            child = p / 'EA Desktop'
            if m and all((child / n).is_file() for n in required):
                if not child.resolve().is_relative_to(drive):
                    raise ValueError('EA payload points outside this private prefix')
                stamp = int(p.name.split('-', 1)[1]) if '-' in p.name else 0
                choices.append(((tuple(map(int, m.group(1).split('.'))), stamp), child))
    if choices: return max(choices, key=lambda x: x[0])[1]
    return canonical if all((canonical / n).is_file() for n in required) else None


def start_ea(data, ea):
    canonical = data / 'prefix/drive_c/Program Files/Electronic Arts/EA Desktop/EA Desktop'
    executable = windows_path(data, ea / 'EADesktop.exe')
    parent = windows_path(data, canonical.parent).lower() + '\\'
    # Reuse only this prefix, including its still-running older EA version.
    # Do not rewrite its service/link while an updater is using it.
    current = next((pid for pid, command in processes()
                    if command.lower().startswith(parent) and command.lower().endswith('\\eadesktop.exe')
                    and belongs_to_prefix(data, pid)), None)
    if current: return current
    if canonical.is_symlink() and not canonical.resolve().is_relative_to((data / 'prefix/drive_c').resolve()):
        raise ValueError('EA directory points outside this private prefix')
    if not canonical.exists() or (canonical.is_symlink() and canonical.resolve() != ea.resolve()):
        # EA's own service/updater uses this unversioned path. Keep the target
        # relative and entirely inside the newly installed private prefix.
        temporary = canonical.with_name('.bf4-ea-link-%d.tmp' % os.getpid())
        temporary.symlink_to(ea.resolve().relative_to(canonical.parent.resolve()), target_is_directory=True)
        try: temporary.replace(canonical)
        finally:
            if temporary.is_symlink(): temporary.unlink()
    # The MSI can retain its unversioned path although only a versioned payload
    # exists. Apply discovery to the service before the very first client start.
    reg(data, r'HKLM\SYSTEM\CurrentControlSet\Services\EABackgroundService',
        'ImagePath', '"' + windows_path(data, ea / 'EABackgroundService.exe') + '"')
    # Registry/service preparation starts Wine itself. In this locked runtime,
    # EA displayed its library when started first in a new Wine session, but
    # stayed blank in the session used for preparation. Reset only our prefix
    # after preparation; a running EA was already returned above.
    stop_session(data)
    # In-process rendering showed sign-in once, but remained pending on restart.
    # Disabling CEF's Windows sandbox also displayed the authenticated library.
    # This is scoped to EA; no debugger is enabled. See the validation matrix.
    return wine(data, [executable, '--in-process-gpu', '--log-level=1', '--no-sandbox'],
                wait=False, cwd=ea, log='ea-launch.log').pid


def install_ea(data):
    if ea_directory(data): return
    from bootstrap import fetch
    lock = json.loads((PROJECT / 'dependencies.lock.json').read_text())
    installer = fetch(lock['downloads']['ea'], data / 'cache')
    package = fetch(lock['downloads']['ea_msi'], data / 'cache')
    guid = lock['ea_bundle']['provider_key']
    if not re.fullmatch(r'\{[0-9a-fA-F-]{36}\}', guid):
        raise ValueError('Invalid locked EA bundle identity')
    # The official 32-bit bootstrapper's dialog fails under this Wine build.
    # Its verified MSI uses the same bundle registration during ConfigureRegistry.
    # This is private installer metadata, never an account/entitlement import.
    cache = data / 'prefix/drive_c/ProgramData/Package Cache' / guid
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copy2(installer, cache / 'EAappInstaller.exe')
    for base in (r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                 r'HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
        key = base + '\\' + guid
        reg(data, key, 'DisplayName', 'EA app')
        reg(data, key, 'BundleCachePath', windows_path(data, cache / 'EAappInstaller.exe'))
    properties = ['ARPSYSTEMCOMPONENT=1', 'MSIFASTINSTALL=7', 'JUNO_CREATE_DESKTOP_SHORTCUT=0',
                  'BUNDLE_PROVIDERKEY=' + guid, 'PRODUCT_DISPLAY_NAME=EAapp',
                  'EAX_BUNDLE_EXECUTABLE_NAME=EAappInstaller.exe', 'EAX_LAUNCH_CLIENT=0']
    print('Installing the verified official EA MSI. Account sign-in comes afterwards.', flush=True)
    result = wine(data, ['msiexec', '/i', package, '/qn', '/norestart', '/L*v',
                        r'C:\bf4-tools\ea-msi-install.log'] + properties,
                  timeout=900, log='ea-install.log', cwd=package.parent)
    ea = ea_directory(data)
    if result.returncode not in (0, 194, 3010) or ea is None:
        raise RuntimeError('EA installation failed. Inspect private ea-install.log / bf4-tools/ea-msi-install.log; no account was imported.')
    atomic_json(data / 'ea-install.json', {'schema': 1, 'method': 'official-msi',
                'package_sha256': lock['downloads']['ea_msi']['sha256'], 'installed_unix': time.time()})
    print('EA installed. Sign in yourself in the new client window.', flush=True)
    start_ea(data, ea)


def content_ids(game):
    xml = game / '__Installer/installerdata.xml'
    if not xml.is_file(): raise RuntimeError('Official BF4 installer metadata is missing')
    tree = ET.parse(xml)
    result = []
    for element in tree.iter('contentID'):
        value = (element.text or '').strip()
        if value and re.fullmatch(r'[A-Za-z0-9._-]+', value) and value not in result:
            result.append(value)
    if not result: raise RuntimeError('No official BF4 content ID was found')
    return result


def game_directory(data):
    canonical = data / 'prefix' / GAME_RELATIVE
    if (canonical / 'bf4.exe').is_file(): return canonical
    # Detect an EA-created installation elsewhere inside this prefix.
    root = data / 'prefix/drive_c/Program Files'
    for base in ('EA Games', 'Origin Games'):
        parent = root / base
        if parent.exists():
            for p in parent.iterdir():
                if p.is_dir() and (p / 'bf4.exe').is_file(): return p
    return None


def attach(data, source):
    if game_pid(data): raise RuntimeError('Close this BF4 instance before attaching game files')
    source = Path(source).expanduser().resolve()
    if not (source / 'bf4.exe').is_file(): raise ValueError('Select the folder containing bf4.exe')
    content_ids(source)
    destination = data / 'prefix' / GAME_RELATIVE
    if destination.exists(): raise ValueError('Game folder already exists; use EA repair to update it')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # APFS clone preserves the original; no prefix, account, saves or Wine registry copied.
    result = subprocess.run(['/bin/cp', '-cR', str(source), str(destination)], capture_output=True)
    if result.returncode:
        if destination.exists(): shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=False)
    for name in ('dxgi.dll', 'd3d9.dll', 'dinput8.dll', 'ReShade.ini', 'ReShade.log',
                 'reshade-shaders', 'SweetFX', 'user.cfg'):
        p = destination / name
        if p.is_dir(): shutil.rmtree(p)
        elif p.exists(): p.unlink()
    for p in destination.glob('*.ini'): p.unlink()
    for p in destination.rglob('*.log'): p.unlink()
    for p in destination.rglob('InstallLog.txt'): p.unlink()
    ensure_redists(data)
    configure_game(data)
    print('Legal game files copied into the new prefix. EA may need Locate/Repair and activation.', flush=True)


def languages(game):
    if game is None: return []
    directory = game / 'Data/Win32/Loc'
    locale_map = {'en': 'en_US', 'fr': 'fr_FR', 'it': 'it_IT', 'de': 'de_DE',
                  'es': 'es_ES', 'pl': 'pl_PL', 'ru': 'ru_RU', 'pt': 'pt_BR',
                  'ja': 'ja_JP', 'cz': 'cs_CZ', 'cs': 'cs_CZ', 'ko': 'ko_KR',
                  'zh': 'zh_TW', 'ch': 'zh_TW'}
    # BF4 ships short basenames (ru.toc/ru.sb), but EA's registry uses ru_RU.
    result = set()
    for p in directory.glob('*.toc'):
        if not p.with_suffix('.sb').is_file(): continue
        if p.stem in locale_map: result.add(locale_map[p.stem])
        elif re.fullmatch(r'[a-z]{2}_[A-Z]{2}', p.stem): result.add(p.stem)
    return sorted(result)


def preferred_language(available):
    try:
        result = subprocess.run(['/usr/bin/defaults', 'read', '-g', 'AppleLanguages'],
                                text=True, capture_output=True, timeout=5)
        preferred = re.findall(r'\b([a-z]{2,3})(?:[-_][A-Za-z]{2,4})?\b', result.stdout)
    except (OSError, subprocess.SubprocessError):
        preferred = []
    for language in preferred:
        match = next((v for v in available if v.split('_')[0] == language), None)
        if match: return match
    return 'en_US' if 'en_US' in available else available[0]


def configure_game(data):
    game = game_directory(data)
    if not game: return
    config = load_config(data)
    available = languages(game)
    if config['language'] not in available:
        if available: config['language'] = preferred_language(available)
        else: raise RuntimeError('No installed language package detected; repair in EA')
    atomic_json(data / 'config.json', config)
    install = windows_path(data, game) + '\\'
    for key in (r'HKLM\SOFTWARE\EA Games\Battlefield 4',
                r'HKLM\SOFTWARE\WOW6432Node\EA Games\Battlefield 4'):
        reg(data, key, 'Install Dir', install)
        reg(data, key, 'Locale', config['language'])
        reg(data, key, 'GDFBinary', install + 'GDFBinary_' + config['language'] + '.dll')
    # Let BF4 create its own standard graphics and controls. Never seed or
    # rewrite PROFSAVE; an existing player's profile also remains theirs.
    if config['fps_limit'] is not None:
        cfg = game / 'user.cfg'
        if cfg.exists() and not (data / 'user-cfg.backup').exists():
            shutil.copy2(cfg, data / 'user-cfg.backup')
        old = cfg.read_text() if cfg.exists() else ''
        lines = [line for line in old.splitlines()
                 if not line.strip().startswith('GameTime.MaxVariableFps ')]
        lines.append('GameTime.MaxVariableFps %d' % config['fps_limit'])
        updated = '\n'.join(lines) + '\n'
        if old != updated: cfg.write_text(updated)
    configure_reshade(data)


def native_effect_compiler(data):
    """Require the x64 compiler installed by the game's official DirectX redist."""
    drive = (data / 'prefix/drive_c').resolve()
    compiler = drive / 'windows/system32/d3dcompiler_43.dll'
    if not compiler.resolve().is_relative_to(drive): return False
    try:
        with compiler.open('rb') as source:
            header = source.read(256)
            if len(header) < 64 or header[:2] != b'MZ' or b'Wine builtin DLL' in header:
                return False
            offset = struct.unpack_from('<I', header, 0x3c)[0]
            source.seek(offset)
            pe = source.read(6)
        return pe[:4] == b'PE\x00\x00' and pe[4:] == b'\x64\x86'
    except OSError:
        return False


def configure_reshade(data):
    game = game_directory(data)
    if not game: return
    config = load_config(data)
    dll = game / 'dxgi.dll'
    ownership = data / 'reshade-owned.json'
    overrides = r'HKCU\Software\Wine\AppDefaults\bf4.exe\DllOverrides'
    if not config['reshade']:
        if ownership.exists() and dll.exists():
            from bootstrap import digest
            old = json.loads(ownership.read_text())
            if digest(dll) != old['sha256']:
                raise RuntimeError('dxgi.dll changed outside this project. Preserve it and inspect before toggling.')
            dll.unlink()
        reg(data, overrides, 'dxgi', 'builtin')
        reg(data, overrides, 'd3dcompiler_47', 'builtin')
        reg(data, overrides, 'd3dcompiler_43', 'builtin')
        return
    if not native_effect_compiler(data):
        raise RuntimeError('ReShade needs the native x64 D3DCompiler 43 from the official BF4 DirectX installer. '
                           'Run the redists command for this data directory, then enable ReShade again.')
    from bootstrap import fetch, digest, extract
    lock = json.loads((PROJECT / 'dependencies.lock.json').read_text())
    installer = fetch(lock['downloads']['reshade'], data / 'cache')
    import zipfile
    with zipfile.ZipFile(installer) as z:
        blob = z.read('ReShade64.dll')
    sha = __import__('hashlib').sha256(blob).hexdigest()
    if sha != lock['reshade_dll_sha256']: raise RuntimeError('Unexpected ReShade DLL')
    if dll.exists() and (not ownership.exists() or digest(dll) != json.loads(ownership.read_text())['sha256']):
        raise RuntimeError('Unmanaged dxgi.dll present. Preserve it before enabling ReShade.')
    dll.write_bytes(blob)
    atomic_json(ownership, {'sha256': sha})
    effects = data / 'effects'
    if not effects.exists():
        extract(fetch(lock['downloads']['sweetfx'], data / 'cache'), effects)
    source = effects / ('SweetFX-' + lock['sweetfx_commit'])
    # Clean adapter uses original MIT shaders; no third-party preset is distributed.
    shutil.copytree(source / 'Shaders/SweetFX', game / 'reshade-shaders/SweetFX', dirs_exist_ok=True)
    # Layer and SMAA need upstream textures even when they are initially disabled.
    # Keep these in the directory used by the generated TextureSearchPaths.
    shutil.copytree(source / 'Textures/SweetFX', game / 'reshade-shaders', dirs_exist_ok=True)
    install_legacy_effects(data, game, lock)
    shutil.copy2(PROJECT / 'assets/WorkingSweetFX.fx', game / 'reshade-shaders/SweetFX/WorkingSweetFX.fx')
    header_archive = fetch(lock['downloads']['reshade_headers'], data / 'cache')
    header_folder = data / 'effects-headers'
    if not header_folder.exists(): extract(header_archive, header_folder)
    headers = header_folder / ('reshade-shaders-' + lock['reshade_headers_commit']) / 'Shaders'
    shutil.copy2(headers / 'ReShade.fxh', game / 'reshade-shaders/SweetFX/ReShade.fxh')
    shutil.copy2(PROJECT / 'assets/ReShadeUI.fxh', game / 'reshade-shaders/SweetFX/ReShadeUI.fxh')
    preset = game / 'PortableSweetFX.ini'
    if not preset.exists():
        shutil.copy2(PROJECT / 'assets/PortableSweetFX.ini', preset)
    config_path = game / 'ReShade.ini'
    if not config_path.exists():
        config_path.write_text('[GENERAL]\nEffectSearchPaths=.\\reshade-shaders\\SweetFX\n'
            'TextureSearchPaths=.\\reshade-shaders\nPresetPath=.\\PortableSweetFX.ini\n'
            'PerformanceMode=1\n[INPUT]\nKeyOverlay=118,0,0,0\nKeyEffects=117,0,0,0\n'
            '[OVERLAY]\nTutorialProgress=4\n')
    # ReShade 6.8 falls back to 43 when 47 cannot be loaded. Scope this to BF4;
    # choosing native 43 restored rendering during SweetFX loading in testing.
    reg(data, overrides, 'd3dcompiler_47', '')
    reg(data, overrides, 'd3dcompiler_43', 'native,builtin')
    reg(data, overrides, 'dxgi', 'native,builtin')


def install_legacy_effects(data, game, lock):
    from bootstrap import fetch, digest
    archive = fetch(lock['downloads']['sweetfx_legacy'], data / 'cache')
    destination = game / 'reshade-shaders/SweetFX/Legacy'
    names = ('LumaSharpen.h', 'LiftGammaGain.h', 'Curves.h', 'Dither.h')
    if all((destination / n).is_file() and digest(destination / n) == lock['legacy_headers'][n] for n in names):
        return
    blobs = {}
    for name in names:
        # Extract only a named member to stdout; no archive paths or links are materialized.
        blob = subprocess.check_output(['/usr/bin/tar', '-xOf', str(archive), 'SweetFX/Shaders/' + name])
        if __import__('hashlib').sha256(blob).hexdigest() != lock['legacy_headers'][name]:
            raise RuntimeError('Official SweetFX legacy shader hash mismatch')
        blobs[name] = blob
    destination.mkdir(parents=True, exist_ok=True)
    for name, blob in blobs.items(): (destination / name).write_bytes(blob)


def processes():
    result = subprocess.check_output(['/bin/ps', '-axo', 'pid=,comm='], text=True)
    return [(int(parts[0]), parts[1]) for line in result.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2]


def belongs_to_prefix(data, pid):
    result = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-Fn'], text=True, capture_output=True)
    prefix = str(data / 'prefix') + '/'
    return any(line.startswith('n' + prefix) for line in result.stdout.splitlines())


def game_pid(data):
    game = game_directory(data)
    if not game: return None
    expected = windows_path(data, game / 'bf4.exe').lower()
    return next((pid for pid, command in processes() if command.lower() == expected
                 and belongs_to_prefix(data, pid)), None)


def companion(data):
    config = load_config(data)
    if not config['mouse_fix'] and not config['monitoring']: return None
    binary = data / 'apps/Battlefield 4 Portable.app/Contents/MacOS/BF4Companion'
    current = next((pid for pid, command in processes() if command == str(binary)), None)
    if current: return current
    runtime = data / 'runtime'
    runtime.mkdir(exist_ok=True)
    atomic_json(data / 'wine-environment.json', environment(data))
    with (runtime / 'companion.log').open('ab') as log:
        return subprocess.Popen([str(binary), str(data)], stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True).pid


def desktop(data):
    app = data / 'apps/Battlefield 4 Portable.app'
    link = Path.home() / 'Desktop/Battlefield 4 Portable.app'
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != app.resolve():
            raise RuntimeError('A different desktop item already uses this name; it was preserved')
        return
    link.symlink_to(app, target_is_directory=True)


def stop_session(data):
    # Stop the native helper before its Windows bridge. Otherwise a surviving
    # helper would keep a dead pipe across the next Wine session.
    stop_companion(data)
    # wineserver -k is scoped exclusively by this installation's WINEPREFIX.
    server = data / 'wine/bin/wineserver'
    if not server.is_file(): raise RuntimeError('Private wineserver is missing')
    result = subprocess.run([str(server), '-k'], env=environment(data), timeout=30)
    # Wine returns 1 when no server exists. Stopping an already closed session is valid.
    if result.returncode not in (0, 1): result.check_returncode()
    subprocess.run([str(server), '-w'], env=environment(data), check=True, timeout=30)


def stop_companion(data):
    binary = str(data / 'apps/Battlefield 4 Portable.app/Contents/MacOS/BF4Companion')
    for pid, command in processes():
        if command == binary:
            try: os.kill(pid, 15)
            except ProcessLookupError: pass


def uninstall(data, delete_data=False):
    if game_pid(data): raise RuntimeError('Close this BF4 instance before uninstalling')
    stop_session(data)
    stop_companion(data)
    app = data / 'apps/Battlefield 4 Portable.app'
    link = Path.home() / 'Desktop/Battlefield 4 Portable.app'
    if link.is_symlink() and link.resolve() == app.resolve(): link.unlink()
    if app.exists(): shutil.rmtree(app)
    if delete_data:
        if not (data / '.bf4-portable-data').is_file() or data in (Path.home(), Path('/')):
            raise ValueError('Refusing to remove an unowned data directory')
        shutil.rmtree(data)
    print('Launcher removed.' + (' Private data deleted.' if delete_data else ' Private game/account data preserved.'))


def launch(data, ea_only=False):
    runtime = data / 'runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime / 'launch.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        pid = game_pid(data)
        if pid:
            companion(data)
            (runtime / 'activate-request').write_text('%d %d\n' % (pid, time.time_ns()))
            return
        ea = ea_directory(data)
        if not ea:
            install_ea(data); return
        game = game_directory(data)
        if game and not ea_only:
            ensure_redists(data)
            configure_game(data)
        start_ea(data, ea)
        if game and not ea_only:
            args = [windows_path(data, ea / 'EALauncher.exe')]
            args.append('origin2://game/launch?offerIds=' + ','.join(content_ids(game)) + '&autoDownload=1')
            wine(data, args, wait=False, cwd=ea, log='ea-launch.log')
            companion(data)
        print('EA opened. Sign in / finish updates if requested. Private prefix only.', flush=True)


def game_installers(data):
    game = game_directory(data)
    if not game: raise RuntimeError('Install or attach BF4 first')
    installers = [game / '__Installer/directx/redist/DXSETUP.exe']
    installers += sorted((game / '__Installer/vc').glob('**/vcredist*.exe'))
    installers = [p for p in installers if p.is_file()]
    if not installers: raise RuntimeError('Official game prerequisites are missing. Repair BF4 in EA App.')
    from bootstrap import digest
    return installers, {str(p.relative_to(game)): digest(p) for p in installers}


def install_redists(data):
    installers, fingerprint = game_installers(data)
    for p in installers:
        switches = ['/silent'] if p.name.upper() == 'DXSETUP.EXE' else ['/quiet', '/norestart']
        result = wine(data, [windows_path(data, p)] + switches, timeout=180, log='redists.log', cwd=p.parent)
        if result.returncode not in (0, 3010):
            raise RuntimeError('A game prerequisite installer failed; see private redists.log')
    atomic_json(data / 'redists-installed.json', {'schema': 1, 'installers': fingerprint})
    print('Official bundled prerequisite installers completed. PunkBuster setup is available through EA / pbsvc.exe.')


def ensure_redists(data):
    _, fingerprint = game_installers(data)
    marker = data / 'redists-installed.json'
    try: installed = json.loads(marker.read_text()).get('installers') == fingerprint
    except (OSError, ValueError, AttributeError): installed = False
    if installed and (not load_config(data)['reshade'] or native_effect_compiler(data)): return
    print('Preparing official BF4 prerequisites automatically. This is needed once per game version.', flush=True)
    install_redists(data)


def options(data, args):
    if game_pid(data): raise RuntimeError('Close BF4 before changing persistent options')
    config = load_config(data)
    for key in ('monitoring', 'mouse_fix', 'reshade'):
        value = getattr(args, key, None)
        if value is not None: config[key] = value == 'on'
    if args.language:
        if args.language != 'auto' and args.language not in languages(game_directory(data)):
            raise ValueError('Language files are not installed. Choose a language offered by EA and repair/download it first.')
        config['language'] = args.language
    if args.fps_limit is not None:
        if not 30 <= args.fps_limit <= 1000: raise ValueError('FPS limit must be between 30 and 1000')
        config['fps_limit'] = args.fps_limit
    previous = data / 'config.json'
    if previous.exists(): shutil.copy2(previous, data / 'config.backup.json')
    before = load_config(data)
    stop_session(data)
    # Stop only the companion for this installation; input cleanup runs on SIGTERM.
    stop_companion(data)
    try:
        atomic_json(previous, config)
        if game_directory(data): ensure_redists(data)
        configure_game(data)
    except Exception:
        atomic_json(previous, before)
        configure_game(data)
        raise
    print('Saved options:', json.dumps(config, ensure_ascii=False))


def doctor(data):
    config = load_config(data)
    app = data / 'apps/Battlefield 4 Portable.app'
    result = {'private_data_marker': (data / '.bf4-portable-data').exists(),
              'new_prefix_initialized': (data / 'prefix-initialized.json').exists(),
              'wine_present': wine_executable(data).is_file(), 'ea_installed': ea_directory(data) is not None,
              'vulkan_manifest_present': (data / 'frameworks/vulkan/icd.d/kosmickrisp_mesa_icd.json').is_file(),
              'vulkan_driver_present': (data / 'frameworks/libvulkan_kosmickrisp.dylib').is_file(),
              'game_present': game_directory(data) is not None,
              'native_effect_compiler_present': native_effect_compiler(data),
              'available_languages': languages(game_directory(data)), 'options': config,
              'app_present': app.is_dir(), 'game_running': game_pid(data) is not None,
              'user_folder_host_links': 0}
    profiles = data / 'prefix/drive_c/users'
    if profiles.exists():
        private_drive = (data / 'prefix/drive_c').resolve()
        result['user_folder_host_links'] = sum(p.is_symlink() and not p.resolve().is_relative_to(private_drive)
                                              for p in profiles.rglob('*'))
    print(json.dumps(result, indent=2))


def interactive(data):
    print('BF4 Portable: 1 Play, 2 EA App, 3 Copy legal game folder, 4 Options, 5 Diagnose')
    choice = input('Choose [1]: ').strip() or '1'
    if choice == '1': launch(data)
    elif choice == '2': launch(data, ea_only=True)
    elif choice == '3':
        folder = subprocess.check_output(['/usr/bin/osascript', '-e',
            'POSIX path of (choose folder with prompt "Choose the folder containing bf4.exe")'], text=True).strip()
        attach(data, folder)
    elif choice == '4':
        game = game_directory(data)
        print('Installed languages:', ', '.join(languages(game)))
        print('Close BF4 first. Press Enter to preserve the current setting.')
        ns = argparse.Namespace(language=input('Language code: ').strip() or None, fps_limit=None)
        for key in ('monitoring', 'mouse_fix', 'reshade'):
            value = input(key + ' [on/off]: ').strip().lower()
            if value not in ('', 'on', 'off'): raise ValueError('Use on or off')
            setattr(ns, key, value or None)
        options(data, ns)
    elif choice == '5': doctor(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup'); sub.add_parser('play'); sub.add_parser('ea')
    sub.add_parser('desktop'); sub.add_parser('doctor'); sub.add_parser('menu'); sub.add_parser('redists')
    sub.add_parser('stop'); sub.add_parser('update')
    p = sub.add_parser('uninstall'); p.add_argument('--delete-data', action='store_true')
    p = sub.add_parser('attach'); p.add_argument('game_folder', type=Path)
    p = sub.add_parser('options')
    for name in ('monitoring', 'mouse-fix', 'reshade'): p.add_argument('--' + name, choices=('on', 'off'))
    p.add_argument('--language'); p.add_argument('--fps-limit', type=int)
    args = parser.parse_args()
    data = args.data_dir.expanduser().resolve()
    if data == PROJECT or data.is_relative_to(PROJECT): raise ValueError('Private data must be outside the repository')
    if args.command != 'setup' and not (data / '.bf4-portable-data').is_file():
        raise RuntimeError('Run setup first with this data directory')
    if args.command == 'setup':
        if game_pid(data): raise RuntimeError('Close this BF4 instance before running setup again')
        if (data / 'prefix-initialized.json').exists():
            stop_session(data); stop_companion(data)
        from bootstrap import build
        build(data)
        if not (data / 'config.json').exists(): atomic_json(data / 'config.json', load_config(data))
        initialize_prefix(data); desktop(data); install_ea(data)
    elif args.command == 'play': launch(data)
    elif args.command == 'ea': launch(data, ea_only=True)
    elif args.command == 'desktop': desktop(data)
    elif args.command == 'attach': attach(data, args.game_folder)
    elif args.command == 'options': options(data, args)
    elif args.command == 'redists': install_redists(data)
    elif args.command == 'doctor': doctor(data)
    elif args.command == 'menu': interactive(data)
    elif args.command == 'stop': stop_session(data)
    elif args.command == 'uninstall': uninstall(data, args.delete_data)
    elif args.command == 'update':
        if game_pid(data): raise RuntimeError('Close BF4 before updating')
        stop_session(data); stop_companion(data)
        backup = data / 'backups' / time.strftime('%Y%m%d-%H%M%S')
        backup.mkdir(parents=True)
        owned = ('apps', 'renderer', 'wine', 'frameworks', 'tools')
        for name in owned:
            shutil.copytree(data / name, backup / name, symlinks=True)
        from bootstrap import build
        try:
            build(data); initialize_prefix(data, refresh=True); configure_game(data)
        except Exception:
            for name in owned:
                if (data / name).exists(): shutil.rmtree(data / name)
                shutil.copytree(backup / name, data / name, symlinks=True)
            raise


if __name__ == '__main__':
    try: main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

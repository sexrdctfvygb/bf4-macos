#!/usr/bin/env python3
"""Build private runtimes from locked official downloads. Python 3.9+, stdlib only."""
# SPDX-License-Identifier: MIT
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import shutil
import subprocess
import tarfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]


def check_system():
    if platform.system() != 'Darwin':
        raise RuntimeError('This installer requires macOS')
    major = int(platform.mac_ver()[0].split('.')[0])
    arm = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'], text=True).strip()
    if arm != '1' or major < 26:
        raise RuntimeError('This locked build targets Apple Silicon and macOS 26+. Other environments are not validated.')
    for tool in ('swiftc', 'clang'):
        subprocess.run(['/usr/bin/xcrun', '--find', tool], check=True, capture_output=True)
    subprocess.run(['/usr/bin/arch', '-x86_64', '/usr/bin/true'], check=True, capture_output=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(args, **kw):
    print('Building:', Path(str(args[0])).name, flush=True)
    subprocess.run([str(x) for x in args], check=True, **kw)


def fetch(item, cache):
    path = cache / item['name']
    if path.exists() and digest(path) == item['sha256']:
        return path
    tmp = path.with_suffix(path.suffix + '.part')
    print('Downloading:', item['name'], flush=True)
    run(['/usr/bin/curl', '--fail', '--location', '--retry', '3', '--proto', '=https',
         '--tlsv1.2', '--output', tmp, item['url']])
    if digest(tmp) != item['sha256']:
        tmp.unlink(missing_ok=True)
        raise RuntimeError('Download hash mismatch for ' + item['name'] +
                           '. No unverified component was installed; see docs/UPDATING.md.')
    tmp.replace(path)
    return path


def extract(archive, destination, select=None):
    """Reject escaping paths/links before extraction; optional official asset subset."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as t:
        members = [m for m in t.getmembers() if select is None or select(m.name)]
        for m in members:
            p = PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or m.isdev() or m.isfifo():
                raise ValueError('Unsafe archive member')
            if m.issym() or m.islnk():
                link = PurePosixPath(m.linkname)
                candidate = p.parent / link if m.issym() else link
                depth = 0
                for part in candidate.parts:
                    if part == '..': depth -= 1
                    elif part not in ('.', ''): depth += 1
                    if depth < 0: raise ValueError('Escaping archive link')
                if link.is_absolute(): raise ValueError('Absolute archive link')
            target = destination / m.name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Archive path traverses a symlink')
        t.extractall(destination, members=members)


def merge_tree(source, destination):
    # copytree(dirs_exist_ok=True) does not replace existing symlinks on retry.
    for p in source.rglob('*'):
        if p.is_symlink():
            target = destination / p.relative_to(source)
            if target.is_symlink(): target.unlink()
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def configure_vulkan(data):
    # Wine 11 discovers display devices through Vulkan, including for non-Vulkan games.
    # Relative to this generated manifest; no wrapper or owner path is required.
    framework = data / 'frameworks'
    if not (framework / 'libvulkan_kosmickrisp.dylib').is_file():
        raise RuntimeError('The locked template Vulkan driver is missing')
    folder = framework / 'vulkan/icd.d'
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {'file_format_version': '1.0.1',
                'ICD': {'api_version': '1.4.363',
                        'library_path': '../../libvulkan_kosmickrisp.dylib'}}
    (folder / 'kosmickrisp_mesa_icd.json').write_text(json.dumps(manifest, indent=2) + '\n')


def make_app(data, compiler):
    app = data / 'apps/Battlefield 4 Portable.app'
    contents = app / 'Contents'
    resources = contents / 'Resources'
    executable = contents / 'MacOS'
    resources.mkdir(parents=True, exist_ok=True)
    executable.mkdir(parents=True, exist_ok=True)
    payload = resources / 'project'
    if payload.exists(): shutil.rmtree(payload)
    payload.mkdir()
    for name in ('src', 'scripts', 'assets', 'patches', 'licenses', 'docs'):
        shutil.copytree(PROJECT / name, payload / name, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('dependencies.lock.json', 'LICENSE', 'README.md'):
        shutil.copy2(PROJECT / name, payload / name)
    # Local generated location is private, never part of a source release.
    (resources / 'data-location.txt').write_text(str(data) + '\n')
    tools = data / 'tools'
    tools.mkdir(exist_ok=True)
    run(['/usr/bin/xcrun', 'swiftc', '-O', PROJECT / 'src/Launcher.swift',
         '-o', executable / 'BF4Launcher'])
    run(['/usr/bin/xcrun', 'swiftc', '-O', PROJECT / 'src/Companion.swift',
         '-o', executable / 'BF4Companion'])
    run([compiler / 'bin/x86_64-w64-mingw32-clang', '-O2', '-static',
         PROJECT / 'src/MouseBridge.c', '-o', tools / 'MouseBridge.exe', '-luser32'])
    info = {'CFBundleIdentifier': 'org.bf4portable.launcher',
            'CFBundleName': 'Battlefield 4 Portable', 'CFBundleDisplayName': 'Battlefield 4 Portable',
            'CFBundleExecutable': 'BF4Launcher', 'CFBundlePackageType': 'APPL',
            'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '1',
            'LSMinimumSystemVersion': '26.0', 'LSUIElement': True,
            'NSHighResolutionCapable': True,
            'NSAccessibilityUsageDescription': 'Side buttons are translated only while Battlefield 4 is active.'}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', app])
    return app


def build(data):
    check_system()
    data = Path(data).expanduser().resolve()
    if data == PROJECT or data.is_relative_to(PROJECT):
        raise ValueError('User data must be outside the source repository')
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = data / '.bf4-portable-data'
    if any(data.iterdir()) and not marker.exists():
        raise ValueError('Choose an empty data directory or an existing BF4 Portable data directory')
    marker.write_text('BF4 Portable private data v1\n')
    data.chmod(0o700)
    lock = json.loads((PROJECT / 'dependencies.lock.json').read_text())
    cache = data / 'cache'
    work = data / 'build'
    cache.mkdir(exist_ok=True); work.mkdir(exist_ok=True)
    files = {key: fetch(item, cache) for key, item in lock['downloads'].items()
             if key not in ('ea_msi', 'sweetfx', 'sweetfx_legacy', 'reshade', 'reshade_headers')}
    # Some SDK members are read-only. Re-extract into an empty staging tree.
    shutil.rmtree(work)
    work.mkdir()
    # Only Frameworks from the official template, never its prefix/launcher/Creator.
    if (work / 'template').exists(): shutil.rmtree(work / 'template')
    extract(files['template'], work / 'template',
            lambda n: '/Contents/Frameworks' in n and '/d3dmetal' not in n.lower()
            and not n.endswith('/renderer/apple_gptk'))
    extract(files['engine'], work / 'engine')
    framework = work / 'template/Template-1.0.21.app/Contents/Frameworks'
    engine = work / 'engine/wswine.bundle'
    if (data / 'frameworks').exists(): shutil.rmtree(data / 'frameworks')
    merge_tree(framework, data / 'frameworks')
    configure_vulkan(data)
    if (data / 'wine').exists(): shutil.rmtree(data / 'wine')
    merge_tree(engine, data / 'wine')
    extract(files['sdk'], work / 'sdk')
    extract(files['compiler'], work / 'compiler')
    compiler = work / 'compiler/llvm-mingw-20260908-ucrt-macos-universal'
    sdk = work / 'sdk'
    if (work / 'dxmt-source').exists(): shutil.rmtree(work / 'dxmt-source')
    extract(files['dxmt'], work / 'dxmt-source')
    source = work / 'dxmt-source' / ('dxmt-' + lock['dxmt_commit'])
    # Applying to a freshly extracted tree on every build prevents stacked patches.
    run(['/usr/bin/patch', '-p1', '--batch', '-i', PROJECT / 'patches/dxmt-bf4.patch'], cwd=source)
    extract(files['headers'], work / 'headers')
    headers = work / 'headers' / ('mingw-directx-headers-' + lock['headers_commit'])
    merge_tree(headers, source / 'include/native/directx')
    shader = lock['shader']
    original = framework / 'renderer/dxmt/wine/x86_64-windows/d3d11.dll'
    if digest(original) != shader['container_sha256']:
        raise RuntimeError('Pinned DXMT shader container differs')
    blob = original.read_bytes()[shader['offset']:shader['offset'] + shader['size']]
    if hashlib.sha256(blob).hexdigest() != shader['sha256']:
        raise RuntimeError('Pinned DXMT shader differs')
    precompiled = source / 'src/dxmt/precompiled'
    precompiled.mkdir(exist_ok=True)
    (precompiled / 'dxmt_command.h').write_text('unsigned char dxmt_command[] = {\n' +
        ','.join('0x%02x' % b for b in blob) + '\n};\nunsigned int dxmt_command_len = %d;\n' % len(blob))
    venv = work / 'python'
    if not (venv / 'bin/python3').exists():
        run(['/usr/bin/python3', '-m', 'venv', venv])
    run([venv / 'bin/python3', '-m', 'pip', 'install', '--no-deps', '--no-index',
         files['meson'], files['ninja']])
    env = os.environ.copy()
    env['PATH'] = str(venv / 'bin') + ':' + env['PATH']
    renderer = data / 'renderer/wine'
    if renderer.exists(): shutil.rmtree(renderer)
    merge_tree(framework / 'renderer/dxmt/wine', renderer)
    for triplet, family, target in [('x86_64', 'x86_64', 'x86_64-windows'),
                                    ('i686', 'x86', 'i386-windows')]:
        cross = work / ('cross-' + triplet + '.txt')
        binaries = {'c': 'clang', 'cpp': 'clang++', 'ar': 'ar', 'strip': 'strip', 'windres': 'windres'}
        def quote(value):
            return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"
        cross.write_text('[binaries]\n' + '\n'.join('%s = %s' % (key, quote(
            compiler / ('bin/' + triplet + '-w64-mingw32-' + binary))) for key, binary in binaries.items()) +
            "\n[properties]\nneeds_exe_wrapper = true\n[host_machine]\nsystem = 'windows'\n" +
            'cpu_family = %s\ncpu = %s\nendian = \'little\'\n' % (quote(family), quote(triplet)))
        output = work / ('dxmt-build-' + triplet)
        if output.exists(): shutil.rmtree(output)
        run([venv / 'bin/meson', 'setup', output, source, '--cross-file', cross,
             '-Dbuildtype=release', '-Ddebug=false', '-Dstrip=false',
             '-Dwine_install_path=' + str(sdk), '-Dnative_llvm_path=' + str(work / 'unused-llvm'),
             '-Denable_tests=false', '-Denable_nvapi=false', '-Denable_nvngx=false',
             '-Denable_d3d12=false', '-Dbuild_airconv_for_windows=false', '-Dwine_builtin_dll=true'], env=env)
        run([venv / 'bin/ninja', '-C', output, '-j', str(min(os.cpu_count() or 4, 8))], env=env)
        for module in ('d3d11', 'dxgi', 'd3d10core', 'winemetal'):
            directory = 'd3d10' if module == 'd3d10core' else module
            binary = output / ('src/' + directory + '/' + module + '.dll')
            # Wine loader metadata is essential; this is also a Meson postprocess target.
            run([sdk / 'bin/winebuild', '--builtin', binary])
            destination = renderer / target / (module + '.dll')
            destination.unlink(missing_ok=True)
            shutil.copy2(binary, destination)
    app = make_app(data, compiler)
    manifest = {'schema': 1, 'dxmt_commit': lock['dxmt_commit'],
                'lock_sha256': digest(PROJECT / 'dependencies.lock.json'),
                'patch_sha256': digest(PROJECT / 'patches/dxmt-bf4.patch'),
                'renderer_sha256': {str(p.relative_to(renderer)): digest(p)
                    for p in renderer.rglob('*') if p.is_file() and p.suffix in ('.dll', '.so')}}
    (data / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Runtime build complete. Private app:', app, flush=True)
    return app

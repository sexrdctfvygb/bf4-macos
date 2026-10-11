#!/usr/bin/env python3
"""Scan source, complete reachable Git history and release archives without printing secrets."""
# SPDX-License-Identifier: MIT
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import zipfile

TOP_FILES = {'.gitignore', 'LICENSE', 'README.md', 'dependencies.lock.json',
             'Setup.command', 'Manage.command', 'Launch.command'}
TOP_DIRS = {'src', 'scripts', 'patches', 'assets', 'docs', 'licenses', 'tests'}
PUBLISH_FILES = TOP_FILES | {
    'src/bf4.py', 'src/Launcher.swift', 'src/Companion.swift', 'src/MouseBridge.c',
    'src/frame_ring.py', 'src/telemetry.py', 'scripts/bootstrap.py',
    'scripts/privacy_scan.py', 'scripts/package_source.py', 'patches/dxmt-bf4.patch',
    'assets/PortableSweetFX.ini', 'assets/ReShadeUI.fxh', 'assets/WorkingSweetFX.fx',
    'tests/test_integrity.py',
    'licenses/DXMT-LICENSE', 'licenses/DXMT-COPYING.LIB', 'licenses/SweetFX-MIT.txt',
    'docs/DEPENDENCIES.md', 'docs/MONITORING.md', 'docs/RELATED_PROJECTS.md',
    'docs/UPDATING.md', 'docs/TROUBLESHOOTING.md', 'docs/PUBLISHING.md', 'docs/VALIDATION.md',
}
FORBIDDEN = {'.log', '.exe', '.dll', '.so', '.dylib', '.reg', '.dlf', '.key',
             '.pem', '.pcap', '.pcapng', '.csv', '.pyc'}


def publishable(root):
    result = []
    for item in sorted(root.iterdir()):
        if item.is_symlink(): raise ValueError('Publication links are forbidden')
        if item.name == '.git': continue
        if item.name in TOP_FILES and item.is_file(): result.append(item)
        elif item.name in TOP_DIRS and item.is_dir():
            for p in sorted(item.rglob('*')):
                if p.is_symlink(): raise ValueError('Publication links are forbidden')
                if p.is_file(): result.append(p)
        else: raise ValueError('Unexpected top-level publication item')
    for path in result:
        if str(path.relative_to(root)) not in PUBLISH_FILES:
            raise ValueError('File is not in the explicit source publication allowlist')
        if path.is_symlink() or path.suffix.lower() in FORBIDDEN or '__pycache__' in path.parts:
            raise ValueError('Forbidden publication file type or symlink')
    if {str(p.relative_to(root)) for p in result} != PUBLISH_FILES:
        raise ValueError('Required publication source or license file is missing')
    return result


def rules():
    home = str(Path.home()).encode()
    user = getpass.getuser().encode()
    return [
        ('local-home', re.compile(re.escape(home))),
        ('local-login', re.compile(rb'(?<![\w-])' + re.escape(user) + rb'(?![\w-])', re.I)),
        ('personal-path', re.compile(rb'/(?:Users|home)/[^/\s"\x27]+/')),
        ('personal-email', re.compile(rb'\b[A-Za-z0-9._%+-]+@(?!example\.(?:invalid|com|org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', re.I)),
        ('private-key', re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
        ('vpn-profile', re.compile(rb'(?:vless|vmess|trojan)://', re.I)),
        ('account-cookie', re.compile(rb'(?i)(?:EA_SESSION|remid|sid|access_token|refresh_token)\s*[=:]\s*["\x27]?[A-Za-z0-9_./+=-]{24,}')),
        ('credential', re.compile(rb'(?i)(?:password|passwd|api[_-]?key|secret)\s*[=:]\s*["\x27]?[A-Za-z0-9_./+=-]{24,}')),
    ]


def inspect_bytes(blob, label, patterns=None):
    patterns = patterns or rules()
    # No matched values or personal paths are reported, only category and opaque identifier.
    shader_references = {b'LumaSharpen@LumaSharpen.fx', b'LiftGammaGain@LiftGammaGain.fx',
                         b'Curves@Curves.fx', b'WorkingSweetFX@WorkingSweetFX.fx'}
    findings = []
    for name, pattern in patterns:
        matches = pattern.finditer(blob)
        # These exact ReShade technique@shader references are not mail addresses.
        if any(name != 'personal-email' or m.group() not in shader_references for m in matches):
            findings.append({'category': name, 'location': label})
    return findings


def scan_tree(root):
    files = publishable(root)
    failures = []
    for index, path in enumerate(files):
        failures += inspect_bytes(str(path.relative_to(root)).encode() + b'\n' + path.read_bytes(), 'source-file-%d' % index)
    return files, failures


def scan_git(root):
    if not (root / '.git').is_dir(): raise ValueError('A separate Git repository must be initialized before history verification')
    objects = subprocess.check_output(['git', '-C', str(root), 'rev-list', '--objects', '--all'], text=True)
    failures = []
    count = 0
    for line in objects.splitlines():
        oid = line.split()[0]
        blob = subprocess.check_output(['git', '-C', str(root), 'cat-file', '-p', oid])
        failures += inspect_bytes(blob, 'git-object-' + oid[:12]); count += 1
    # Include reflog-only and dangling objects; no old private commit may remain hidden.
    all_objects = subprocess.check_output(['git', '-C', str(root), 'cat-file', '--batch-all-objects',
                                           '--batch-check=%(objectname)'], text=True).splitlines()
    for oid in set(all_objects) - {l.split()[0] for l in objects.splitlines()}:
        failures += inspect_bytes(subprocess.check_output(['git', '-C', str(root), 'cat-file', '-p', oid]),
                                  'git-object-' + oid[:12]); count += 1
    config = subprocess.check_output(['git', '-C', str(root), 'config', '--local', '--list'])
    failures += inspect_bytes(config, 'local-git-config')
    for index, path in enumerate(sorted((root / '.git/logs').rglob('*'))):
        if path.is_symlink(): raise ValueError('Git reflog links are forbidden')
        if path.is_file(): failures += inspect_bytes(path.read_bytes(), 'git-reflog-%d' % index)
    return count, failures


def scan_archive(path):
    failures = []; count = 0; seen = set()
    def check_member(name):
        label = 'archive-file-%d' % count
        if not name.startswith('bf4-macos/') or name[len('bf4-macos/'):] not in PUBLISH_FILES:
            failures.append({'category': 'unexpected-archive-member', 'location': label})
        if name in seen: failures.append({'category': 'duplicate-archive-member', 'location': label})
        seen.add(name)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir(): continue
                check_member(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    failures.append({'category': 'archive-link', 'location': 'archive-file-%d' % count})
                failures += inspect_bytes(info.filename.encode() + b'\n' + z.read(info), 'archive-file-%d' % count)
                if Path(info.filename).suffix.lower() in FORBIDDEN: failures.append({'category': 'forbidden-type', 'location': 'archive-file-%d' % count})
                count += 1
    else:
        with tarfile.open(path) as t:
            for member in t.getmembers():
                if member.isfile():
                    check_member(member.name)
                    failures += inspect_bytes(member.name.encode() + b'\n' + t.extractfile(member).read(), 'archive-file-%d' % count)
                    if Path(member.name).suffix.lower() in FORBIDDEN: failures.append({'category': 'forbidden-type', 'location': 'archive-file-%d' % count})
                    count += 1
                elif not member.isdir(): failures.append({'category': 'archive-link', 'location': 'archive-member'})
    if seen != {'bf4-macos/' + name for name in PUBLISH_FILES}:
        failures.append({'category': 'incomplete-source-archive', 'location': 'archive-layout'})
    return count, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    files, failures = scan_tree(args.root)
    objects, more = scan_git(args.root); failures += more
    count = 0
    if args.archive:
        count, more = scan_archive(args.archive); failures += more
    print(json.dumps({'source_files': len(files), 'git_objects': objects, 'archive_files': count,
                      'findings': failures, 'passed': not failures}, indent=2))
    raise SystemExit(1 if failures else 0)


if __name__ == '__main__': main()

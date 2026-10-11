#!/usr/bin/env python3
"""Build a source-only archive after scanning the independent repository and its history."""
# SPDX-License-Identifier: MIT
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from privacy_scan import scan_tree, scan_git, scan_archive

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(root): raise ValueError('Release output must be outside the repository')
    files, failures = scan_tree(root)
    _, more = scan_git(root); failures += more
    if failures: raise ValueError('Privacy scan failed; no archive created')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for path in files:
            name = 'bf4-macos/' + str(path.relative_to(root))
            info = zipfile.ZipInfo(name, (2026, 10, 10, 0, 0, 0))
            info.external_attr = ((0o100755 if path.suffix == '.command' else 0o100644) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    count, failures = scan_archive(args.output)
    if failures:
        args.output.unlink(); raise ValueError('Archive scan failed; archive removed')
    print(json.dumps({'files': count, 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      'source_only': True, 'privacy_passed': True}, indent=2))

if __name__ == '__main__': main()

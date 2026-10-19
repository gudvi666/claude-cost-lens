#!/usr/bin/env python3
"""Pack allowlisted sources from a clean directory (including unusual source paths)."""
import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Also run npm publish --dry-run')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    package = json.loads((root / 'package.json').read_text())
    npm = shutil.which('npm.cmd' if __import__('os').name == 'nt' else 'npm')
    if not npm:
        parser.error('npm is required')
    with tempfile.TemporaryDirectory(prefix='claude-cost-lens-pack-') as temporary:
        stage = Path(temporary)
        for relative in ['package.json', *package['files']]:
            source = root / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            target.chmod(0o755 if relative.startswith("bin/") else 0o644)
        result = subprocess.run([npm, 'pack', '--json'], cwd=stage, check=True,
                                capture_output=True, text=True)
        packed = json.loads(result.stdout)[0]
        expected = {'package.json', *package['files']}
        actual = {item['path'] for item in packed['files']}
        if actual != expected:
            raise RuntimeError('Unexpected tarball content: ' + repr(actual ^ expected))
        if args.dry_run:
            subprocess.run([npm, 'publish', packed['filename'], '--dry-run'], cwd=stage, check=True)
        destination = root / 'release' / packed['filename']
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(stage / packed['filename'], destination)
        print(json.dumps(packed, indent=2))
        print('Saved: ' + str(destination))


if __name__ == '__main__':
    main()

"""Install the user LaunchAgent that periodically checks the durable refresh queue."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


LABEL = 'io.github.garlicvread.mensa-refresh'


def generate_plist(base, python):
    """Return a LaunchAgent definition without changing files or loading a job."""
    base = Path(base).expanduser().resolve()
    python = Path(python).expanduser().absolute()
    search_path = [str(python.parent), '/opt/homebrew/bin', '/usr/local/bin',
                   '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    return {
        'Label': LABEL,
        'ProgramArguments': [str(python), '-m', 'scripts.local_refresh', '--base', str(base)],
        'WorkingDirectory': str(base / 'checkout'),
        'RunAtLoad': True,
        'StartInterval': 900,
        'ProcessType': 'Background',
        'LowPriorityIO': True,
        'Nice': 10,
        'ThrottleInterval': 60,
        'EnvironmentVariables': {'PATH': ':'.join(dict.fromkeys(search_path))},
        'StandardOutPath': str(base / 'logs/worker.stdout.log'),
        'StandardErrorPath': str(base / 'logs/worker.stderr.log'),
    }


def _launchctl(*args, check=True):
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True,
                            text=True, timeout=30, check=False)
    if check:
        result.check_returncode()
    return result


def install(base, load=True):
    """Write this user's agent atomically and optionally register only this label."""
    base = Path(base).expanduser().resolve()
    if not (base / 'checkout/scripts/local_refresh.py').is_file():
        raise ValueError(f'Dedicated checkout is missing scripts/local_refresh.py: {base / "checkout"}')

    definition = generate_plist(base, sys.executable)
    destination = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
    destination.parent.mkdir(parents=True, exist_ok=True)
    (base / 'logs').mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=destination.parent,
                                         prefix=f'.{LABEL}.', delete=False) as stream:
            staging = Path(stream.name)
            plistlib.dump(definition, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, destination)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)

    if load:
        domain = f'gui/{os.getuid()}'
        service = f'{domain}/{LABEL}'
        if _launchctl('print', service, check=False).returncode == 0:
            _launchctl('bootout', service)
        _launchctl('bootstrap', domain, str(destination))
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path,
                        default=Path.home() / 'Library/Application Support/Mensa')
    parser.add_argument('--write-only', action='store_true',
                        help='Write the plist without loading or stopping a LaunchAgent')
    args = parser.parse_args(argv)
    try:
        destination = install(args.base, load=not args.write_only)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Worker installation failed: {exc}', file=sys.stderr)
        return 1
    state = 'not loaded' if args.write_only else 'loaded'
    print(f'Wrote {destination} ({state})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

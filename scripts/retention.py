"""작업자 잠금 안에서 생성 자료의 JSON 정리 계획을 보거나 명시적으로 적용합니다."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from mensa.config import load_worker_config
from mensa.drive_publication import DrivePublisher
from mensa.maintenance import apply_maintenance, plan_maintenance
from mensa.queue import exclusive_lock, load_state
from mensa.releases import _json_bytes, _release_id
from mensa.schedule import BERLIN


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--backup-dir', type=Path, help='Explicit owned migration-backup root')
    parser.add_argument('--apply', action='store_true', help='Apply the displayed plan under worker.lock')
    args = parser.parse_args(argv)
    try:
        config = load_worker_config(args.config.expanduser().resolve())
        with exclusive_lock(config.paths.state_dir / 'worker.lock'):
            state = load_state(config.paths.state_dir / 'queue.json')
            if state['pending'] is not None:
                raise ValueError('Maintenance requires an idle queue; preserve pending recovery data')
            pinned = []
            if config.publication.mode == 'drive':
                settings = config.publication
                publisher = DrivePublisher(settings.remote, settings.folder_id, file_id=settings.file_id)
                pair = publisher.load_current()
                if pair is None:
                    raise ValueError('The configured Drive current publication is missing')
                pinned.append(_release_id(*(_json_bytes(value) for value in pair)))
            # Even dry-run binds the actual Drive reference through read-only lsjson/cat.
            plan = plan_maintenance(config, today=datetime.now(BERLIN).date(), pinned=pinned,
                                    backup_dir=None if args.backup_dir is None else args.backup_dir.expanduser().absolute())
            result = {'dry_run': not args.apply, 'plan': plan}
            if args.apply:
                result['applied'] = apply_maintenance(config, plan)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f'Retention maintenance failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

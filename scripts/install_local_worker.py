"""현재 사용자의 macOS LaunchAgent 파일을 설치하고 선택적으로 launchd에 등록합니다.

LaunchAgent는 전용 체크아웃에서 scripts.local_refresh를 실행하도록 정의합니다.
이 설치기는 작업 폴더를 소유자만 접근하도록 설정하고 plist와 로그 디렉터리를 만들지만 큐 작업을 직접 실행하거나 모델을
내려받지 않습니다. 등록 후의 작업자 실행은 launchd가 정의된 조건에 따라 수행합니다.
"""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


LABEL = 'io.github.garlicvread.mensa-refresh'


def generate_plist(base, python):
    """작업자 base 경로와 Python 실행 경로로 LaunchAgent 정의 사전을 반환합니다.

    등록 시 실행하고 이후 900초 간격으로 --base 작업자를 호출하도록 정의하며, 명령
    검색 경로와 표준 출력·오류 로그 경로도 포함합니다. 경로를 정리하여 정의를 만들기만
    하며 파일이나 디렉터리를 만들거나 launchd에 작업을 등록하지 않습니다.
    """
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
    """launchctl을 최대 30초 실행하여 결과를 반환하고 check가 참이면 실패 종료를 오류로 전달합니다."""
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True,
                            text=True, timeout=30, check=False)
    if check:
        result.check_returncode()
    return result


def install(base, load=True):
    """전용 체크아웃을 확인한 뒤 사용자 plist를 교체 저장하고 저장된 파일 경로를 반환합니다.

    기존 작업 기록을 보존하고 base를 소유자만 접근할 수 있는 권한(0700)으로 설정합니다.
    ~/Library/LaunchAgents와 base/logs를 준비합니다. load가 참이면 현재 사용자의
    같은 LABEL 작업만 조회·해제한 뒤 새 정의를 등록합니다. load가 거짓이어도 파일과
    디렉터리는 쓰지만 등록 상태는 변경하지 않습니다. 실패가 발생하면 호출자에게
    오류를 전달하며, 등록 실패 이전에 저장한 plist는 남을 수 있습니다.
    """
    base = Path(base).expanduser().resolve()
    if not (base / 'checkout/scripts/local_refresh.py').is_file():
        raise ValueError(f'Dedicated checkout is missing scripts/local_refresh.py: {base / "checkout"}')

    # 설치기가 실행 프로그램의 비공개 상태 폴더 조건을 준비합니다. 기존 큐·모델·메뉴
    # 파일은 수정하지 않고 전용 폴더 자체의 접근 권한만 제한합니다.
    base.chmod(0o700)

    definition = generate_plist(base, sys.executable)
    destination = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
    destination.parent.mkdir(parents=True, exist_ok=True)
    (base / 'logs').mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        # 목적지와 같은 디렉터리에 임시 plist를 쓰고 파일을 동기화한 뒤 교체하여,
        # launchd나 사용자가 완성된 정의 파일을 읽도록 합니다.
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
        # 현재 사용자의 GUI 영역과 이 LABEL만 대상으로 하며 다른 LaunchAgent는 해제하지 않습니다.
        domain = f'gui/{os.getuid()}'
        service = f'{domain}/{LABEL}'
        if _launchctl('print', service, check=False).returncode == 0:
            _launchctl('bootout', service)
        _launchctl('bootstrap', domain, str(destination))
    return destination


def main(argv=None):
    """설치 인자를 해석하고 성공 시 0, 파일·설정·launchctl 오류 시 1을 반환합니다.

    --write-only도 plist를 저장하지만 기존 작업의 해제나 새 작업 등록은 생략합니다.
    설치 결과와 오류는 표준 출력·표준 오류에 알리고 큐 상태를 갱신하지 않습니다.
    """
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

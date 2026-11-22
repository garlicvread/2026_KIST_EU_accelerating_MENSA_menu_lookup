"""소유 프로세스 그룹의 누적 CPU 사용량을 읽어 시작 작업의 진행 여부를 비교합니다."""

from decimal import Decimal
import os
import re
import subprocess


class ProcessObservationError(RuntimeError):
    """ProcessObservationError는 프로세스 상태를 신뢰할 수 있게 읽지 못했음을 알립니다."""

    def __init__(self):
        super().__init__('Could not observe the owned process group.')


def _cpu_seconds(value):
    # macOS는 MM:SS.cc, Linux는 [[DD-]HH:]MM:SS 형식을 사용합니다.
    days = 0
    if '-' in value:
        day_text, value = value.split('-', 1)
        if not re.fullmatch(r'[0-9]+', day_text):
            raise ValueError
        days = int(day_text)
        has_days = True
    else:
        has_days = False
    fields = value.split(':')
    if len(fields) not in (2, 3) or (has_days and len(fields) != 3):
        raise ValueError
    if (any(not re.fullmatch(r'[0-9]+', field) for field in fields[:-1])
            or not re.fullmatch(r'[0-9]{2}(?:\.[0-9]+)?', fields[-1])):
        raise ValueError
    seconds = Decimal(fields[-1])
    minutes = int(fields[-2])
    hours = int(fields[0]) if len(fields) == 3 else 0
    if seconds >= 60 or (len(fields) == 3 and minutes >= 60) or (has_days and hours >= 24):
        raise ValueError
    return Decimal(days * 86400 + hours * 3600 + minutes * 60) + seconds


def process_activity(process):
    """process_activity는 직접 시작한 세션의 생존 프로세스 ID와 CPU 초를 정렬한 튜플로 반환합니다.

    호출자는 start_new_session=True로 시작한 대표 프로세스를 전달해야 합니다. 대표
    프로세스가 먼저 끝나도 같은 그룹의 하위 프로세스를 읽습니다. 빈 튜플은 관찰에
    성공했지만 생존 구성원이 없다는 뜻이며, 관찰 실패는 별도 예외로 전달합니다.
    """
    if type(process.pid) is not int or process.pid <= 0:
        raise ProcessObservationError() from None
    try:
        snapshot = subprocess.run(
            ['ps', '-axo', 'pid=,pgid=,stat=,time='],
            capture_output=True, text=True, timeout=1,
            env={**os.environ, 'LC_ALL': 'C'}, check=False)
        if snapshot.returncode != 0:
            raise ValueError
        members = []
        for row in snapshot.stdout.splitlines():
            if not row.strip():
                continue
            fields = row.split()
            if (len(fields) != 4
                    or not re.fullmatch(r'[0-9]+', fields[0])
                    or not re.fullmatch(r'[0-9]+', fields[1])):
                raise ValueError
            pid, group = int(fields[0]), int(fields[1])
            if pid <= 0:
                raise ValueError
            state, cpu_time = fields[2:]
            if group == process.pid and not state.startswith('Z'):
                members.append((pid, _cpu_seconds(cpu_time)))
        return tuple(sorted(members))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise ProcessObservationError() from None

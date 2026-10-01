"""자원 측정 함수가 가용 메모리·CPU 부하·전원 상태를 읽고 resource_status가 설정된 실행 조건과 비교합니다.

Linux에서는 선언된 서버 정책에 따라 AC 전원을 측정하지 않고 power_allowed를
참으로 설정합니다. linux_resources는 보이는 cgroup-v2 계층만 검사합니다. 가용 자원과
부하 검사는 자원을 예약하거나 모델의 메모리 적재 성공을 예측하지 않습니다. 모듈을
가져올 때는 호스트 자원을 측정하지 않습니다.
"""

from dataclasses import dataclass
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from mensa.config import ResourceSettings


@dataclass(frozen=True)
class ResourceSnapshot:
    available_bytes: int
    load: int | float
    cpus: int | float
    power_allowed: bool


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value, *, positive=False):
    return type(value) in (int, float) and (not isinstance(value, float) or math.isfinite(value)) and (value > 0 if positive else value >= 0)


def _ports(**ports):
    _require(all(port is None or callable(port) for port in ports.values()), 'Resource measurement ports must be callable')


def _text(value):
    _require(isinstance(value, str) and len(value) <= 1024 ** 2, 'Invalid or oversized resource measurement text')
    return value


def _command_output(command):
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    return result.stdout


def _load():
    return os.getloadavg()[0]


def _cpu_capacity(cpu_count):
    cpus = cpu_count()
    _require(type(cpus) is int and cpus > 0, 'Unknown CPU capacity')
    return cpus


def _validate_snapshot(snapshot):
    _require(isinstance(snapshot, ResourceSnapshot), 'Invalid resource snapshot')
    _require(type(snapshot.available_bytes) is int and snapshot.available_bytes >= 0, 'Invalid available memory measurement')
    _require(_number(snapshot.load), 'Invalid CPU load measurement')
    _require(_number(snapshot.cpus, positive=True), 'Unknown CPU capacity')
    _require(type(snapshot.power_allowed) is bool, 'Invalid power measurement')
    return snapshot


def macos_resources(*, command=None, load=None, cpu_count=None):
    """macos_resources가 vm_stat의 free/inactive/speculative 메모리, 시스템 부하·CPU 수 및 pmset의 전원 상태를 측정합니다."""
    _ports(command=command, load=load, cpu_count=cpu_count)
    command = _command_output if command is None else command
    load = _load if load is None else load
    cpu_count = os.cpu_count if cpu_count is None else cpu_count
    vm = _text(command(['/usr/bin/vm_stat']))
    page_size = re.search(r'page size of (\d+) bytes', vm)
    _require(page_size is not None and int(page_size.group(1)) > 0, 'Missing memory page size')
    pages = []
    for kind in ('free', 'inactive', 'speculative'):
        matches = re.findall(rf'^Pages {kind}:\s+(\d+)\.\s*$', vm, re.MULTILINE)
        _require(len(matches) == 1, f'Missing memory counter: {kind}')
        pages.append(int(matches[0]))
    available = sum(pages) * int(page_size.group(1))
    measured_load = load()
    cpus = _cpu_capacity(cpu_count)
    power = _text(command(['/usr/bin/pmset', '-g', 'batt']))
    on_ac = bool(re.search(r'\bAC Power\b', power, re.IGNORECASE))
    on_battery = bool(re.search(r'\bBattery Power\b', power, re.IGNORECASE))
    has_battery = bool(re.search(r'InternalBattery|\bdischarging\b|\bcharging\b', power, re.IGNORECASE))
    no_battery = bool(re.fullmatch(r'\s*No batteries\.?\s*', power, re.IGNORECASE))
    if on_battery or (has_battery and not on_ac):
        allowed = False
    else:
        _require(on_ac or no_battery, 'Unknown power measurement')
        allowed = True
    return _validate_snapshot(ResourceSnapshot(available, measured_load, cpus, allowed))


def _read_text(path):
    with path.open(encoding='utf-8') as stream:
        return _text(stream.read(1024 ** 2 + 1))


def _affinity():
    if not hasattr(os, 'sched_getaffinity'):
        raise OSError('CPU affinity measurement unavailable')
    return os.sched_getaffinity(0)


def _hierarchy_path(value):
    _require(value.startswith('/') and '\x00' not in value, 'Invalid cgroup hierarchy path')
    _require(value == '/' or all(part not in ('', '.', '..') for part in value[1:].split('/')), 'Invalid cgroup hierarchy path')
    return PurePosixPath(value)


def _mount_path(value):
    escapes = {'040': ' ', '011': '\t', '012': '\n', '134': '\\'}
    def decode(match):
        _require(match.group(1) in escapes, 'Invalid mount path escape')
        return escapes[match.group(1)]
    decoded = re.sub(r'\\([0-7]{3})', decode, value)
    _require(not re.search(r'\\(?![0-7]{3})', value), 'Invalid mount path escape')
    return _hierarchy_path(decoded)


def _current_group(proc_root, cgroup_root, read_text):
    groups = []
    for line in _text(read_text(proc_root / 'self/cgroup')).splitlines():
        parts = line.split(':', 2)
        _require(len(parts) == 3 and parts[0].isdigit(), 'Malformed cgroup membership')
        if parts[0] == '0' and parts[1] == '':
            groups.append(_hierarchy_path(parts[2]))
    _require(len(groups) == 1, 'Unified cgroup membership unavailable or ambiguous')
    mounts = []
    for line in _text(read_text(proc_root / 'self/mountinfo')).splitlines():
        before, separator, after = line.partition(' - ')
        fields, filesystem = before.split(), after.split()
        _require(separator and len(fields) >= 6 and len(filesystem) >= 3, 'Malformed mount metadata')
        if filesystem[0] == 'cgroup2':
            root = _mount_path(fields[3])
            mount = _mount_path(fields[4])
            if Path(str(mount)) == cgroup_root:
                mounts.append(root)
    _require(len(mounts) == 1, 'Visible cgroup2 mount unavailable or ambiguous')
    _require(groups[0].is_relative_to(mounts[0]), 'Current cgroup is outside the visible mount')
    relative = groups[0].relative_to(mounts[0])
    return cgroup_root.joinpath(*relative.parts)


def _counter(value, label, *, positive=False):
    value = value.strip()
    _require(re.fullmatch(r'[0-9]+', value) is not None, f'Invalid {label}')
    result = int(value)
    _require(not positive or result > 0, f'Invalid {label}')
    return result


def _optional_text(path, read_text):
    try:
        return _text(read_text(path)).strip()
    except FileNotFoundError:
        return None


def _limits(directory, root, read_text):
    maximum = _optional_text(directory / 'memory.max', read_text)
    current = _optional_text(directory / 'memory.current', read_text)
    cpu = _optional_text(directory / 'cpu.max', read_text)
    _require((maximum is None and current is None and directory == root) or (maximum is not None and current is not None), 'Missing cgroup memory controller pair')
    _require(cpu is not None or directory == root, 'Missing cgroup CPU controller')
    memory_limit = None
    if maximum is not None:
        used = _counter(current, 'cgroup memory.current')
        if maximum != 'max':
            memory_limit = max(0, _counter(maximum, 'cgroup memory.max') - used)
    cpu_limit = None
    if cpu is not None:
        parts = cpu.split()
        _require(len(parts) == 2, 'Invalid cgroup cpu.max')
        period = _counter(parts[1], 'cgroup CPU period', positive=True)
        if parts[0] != 'max':
            cpu_limit = _counter(parts[0], 'cgroup CPU quota', positive=True) / period
            _require(_number(cpu_limit, positive=True), 'Invalid cgroup CPU capacity')
    return memory_limit, cpu_limit


def linux_resources(*, proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup'), read_text=None, load=None, cpu_count=None, affinity=None):
    """linux_resources가 호스트와 보이는 cgroup-v2 계층의 메모리·CPU 제한을 함께 측정하며, 서버 전원은 정책에 따라 허용합니다."""
    _ports(read_text=read_text, load=load, cpu_count=cpu_count, affinity=affinity)
    _require(all(isinstance(path, Path) and path.is_absolute() for path in (proc_root, cgroup_root)), 'Resource roots must be explicit absolute Paths')
    read_text = _read_text if read_text is None else read_text
    load = _load if load is None else load
    cpu_count = os.cpu_count if cpu_count is None else cpu_count
    affinity = _affinity if affinity is None else affinity
    meminfo = _text(read_text(proc_root / 'meminfo'))
    matches = re.findall(r'^MemAvailable:\s+([0-9]+)\s+kB\s*$', meminfo, re.MULTILINE)
    _require(len(matches) == 1, 'Missing or invalid MemAvailable measurement')
    available = int(matches[0]) * 1024
    cpus = _cpu_capacity(cpu_count)
    ids = affinity()
    _require(type(ids) in (set, frozenset) and ids and all(type(cpu) is int and cpu >= 0 for cpu in ids), 'Invalid CPU affinity measurement')
    cpus = min(cpus, len(ids))
    directory = _current_group(proc_root, cgroup_root, read_text)
    while True:
        memory_limit, cpu_limit = _limits(directory, cgroup_root, read_text)
        if memory_limit is not None:
            available = min(available, memory_limit)
        if cpu_limit is not None:
            cpus = min(cpus, cpu_limit)
        if directory == cgroup_root:
            break
        directory = directory.parent
    return _validate_snapshot(ResourceSnapshot(available, load(), cpus, True))


def resource_status(settings, *, probe=None, minimum_load=0):
    """resource_status가 자원을 한 번 측정하고 선언된 메모리·부하·전원 조건과 비교합니다.

    현재 실행 가능 여부와 이유를 반환하며, 측정 오류가 나면 오류 메시지의 길이를 제한합니다.
    """
    _require(isinstance(settings, ResourceSettings), 'ResourceSettings are required')
    _require(settings.platform in ('macos', 'linux'), 'Unknown resource platform')
    _require(type(settings.min_available_bytes) is int and settings.min_available_bytes > 0, 'Invalid minimum available memory')
    _require(_number(settings.max_load_per_cpu, positive=True), 'Invalid maximum load per CPU')
    _require(_number(minimum_load), 'Invalid minimum load floor')
    _ports(probe=probe)
    if probe is None:
        probe = macos_resources if settings.platform == 'macos' else linux_resources
    try:
        snapshot = _validate_snapshot(probe())
        available, load = snapshot.available_bytes, snapshot.load
        if available < settings.min_available_bytes:
            return False, f'Available memory {available / 1024 ** 3:.1f} GiB is below {settings.min_available_bytes / 1024 ** 3:g} GiB'
        threshold = max(minimum_load, snapshot.cpus * settings.max_load_per_cpu)
        if load > threshold:
            return False, f'CPU load {load:.2f} exceeds {threshold:.2f}'
        if not snapshot.power_allowed:
            return False, 'Battery power: waiting for AC power'
        power = ', AC or no battery' if settings.platform == 'macos' else ''
        return True, f'Ready: {available / 1024 ** 3:.1f} GiB available, CPU load {load:.2f}/{threshold:.2f}{power}'
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError, OverflowError) as exc:
        return False, f'Resource measurement unavailable: {str(exc)[:500]}'

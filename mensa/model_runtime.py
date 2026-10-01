"""model_session이 설정된 모델 프로세스를 직접 시작·종료하거나, 외부 소유 엔드포인트의 접속 설정을 빌려 제공합니다.

관리 모드에서는 태그 메타데이터로 준비 상태와 설정된 모델·리비전을 확인하지만 모델
가중치를 인증하지는 않습니다. 운영자는 사용 중 비공개 모델 디렉터리가 안정적으로
유지되도록 해야 합니다.
"""

from collections.abc import Mapping
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request

from mensa.config import InferenceSettings
from mensa.errors import GenerationError


MAX_TAG_BYTES = 1024 * 1024


def choose_loopback_port():
    """choose_loopback_port가 임시 루프백 포트를 잠시 예약하고 번호를 반환하며, 이후 바인딩은 모델 서버가 담당합니다."""
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        return reservation.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        response.close()
        raise GenerationError('redirect') from None


def _models(envelope):
    if not isinstance(envelope, Mapping):
        raise GenerationError('invalid_envelope')
    models = envelope.get('models')
    if not isinstance(models, list) or any(not isinstance(model, Mapping) for model in models):
        raise GenerationError('invalid_envelope')
    return models


def probe_models(host, timeout):
    """probe_models가 프록시나 리디렉션 없이 로컬 /api/tags 응답을 크기 제한 안에서 읽고 JSON 구조를 검증합니다."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(host + '/api/tags', timeout=timeout) as response:
            data = response.read(MAX_TAG_BYTES + 1)
    except GenerationError as error:
        raise GenerationError(error.code) from None
    except urllib.error.HTTPError as error:
        error.close()
        raise GenerationError('redirect' if 300 <= error.code < 400 else 'network') from None
    except TimeoutError:
        raise GenerationError('timeout') from None
    except (OSError, urllib.error.URLError):
        raise GenerationError('network') from None
    if len(data) > MAX_TAG_BYTES:
        raise GenerationError('response_too_large')
    try:
        envelope = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise GenerationError('invalid_json') from None
    _models(envelope)
    return envelope


def stop_model(process):
    """stop_model이 직접 시작한 대표 프로세스의 종료 상태를 회수하고, 고아 실행 프로세스를 포함한 소유 프로세스 그룹을 종료합니다."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # stop_model은 대표 프로세스가 하위 프로세스보다 먼저 종료되어도 소유 그룹의 정리를 끝까지 수행합니다.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _positive_number(value):
    try:
        return type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def _validate_settings(settings, key, startup_timeout, poll_interval):
    # 전체 설정 선언과 URL 검증은 load_worker_config가 담당합니다. _validate_settings는 호출자가
    # 설정을 직접 구성한 경우에도 프로세스 경계를 보호하도록 필수 값과 모드를 검사합니다.
    if not isinstance(settings, InferenceSettings) or not isinstance(key, str):
        raise GenerationError('invalid_config')
    if not all(isinstance(value, str) and value.strip()
               for value in (settings.mode, settings.provider, settings.model, settings.revision)):
        raise GenerationError('invalid_config')
    if not _positive_number(startup_timeout) or not _positive_number(poll_interval):
        raise GenerationError('invalid_config')
    if settings.mode == 'external':
        if (settings.provider not in ('ollama', 'openai')
                or not isinstance(settings.url, str) or not settings.url.strip()
                or any(value is not None for value in (settings.binary, settings.models_dir, settings.log_dir))):
            raise GenerationError('invalid_config')
    elif settings.mode == 'managed':
        if (settings.provider != 'ollama' or settings.url is not None
                or not all(isinstance(path, Path) and path.is_absolute()
                           for path in (settings.binary, settings.models_dir, settings.log_dir))):
            raise GenerationError('invalid_config')
    else:
        raise GenerationError('invalid_config')


def _private_kind(metadata, kind):
    return kind(metadata.st_mode) and not metadata.st_mode & 0o077


def _validate_paths(settings):
    try:
        if (not settings.binary.is_file() or not settings.models_dir.is_dir()
                or not settings.log_dir.parent.is_dir()):
            raise GenerationError('invalid_config')
        try:
            log_directory = settings.log_dir.lstat()
        except FileNotFoundError:
            return
        if not _private_kind(log_directory, stat.S_ISDIR):
            raise GenerationError('invalid_config')
        try:
            log_file = (settings.log_dir / 'model.log').lstat()
        except FileNotFoundError:
            return
        if not _private_kind(log_file, stat.S_ISREG):
            raise GenerationError('invalid_config')
    except (OSError, ValueError):
        raise GenerationError('invalid_config') from None


@contextmanager
def _private_log(directory):
    """_private_log가 로그 디렉터리·파일의 디스크립터와 비공개 권한을 검사한 뒤 파일을 비우며, 교체된 심볼릭 링크를 따라가지 않습니다."""
    directory_fd = file_fd = None
    try:
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if not _private_kind(os.fstat(directory_fd), stat.S_ISDIR):
                raise GenerationError('invalid_config')
            # _private_log는 NONBLOCK으로 열어 파일이 FIFO로 교체되어도 멈추지 않고 거부합니다.
            file_fd = os.open('model.log', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=directory_fd)
            if not _private_kind(os.fstat(file_fd), stat.S_ISREG):
                raise GenerationError('invalid_config')
            os.ftruncate(file_fd, 0)
            log = os.fdopen(file_fd, 'w', encoding='utf-8')
            file_fd = None  # 이제 log 스트림이 이 파일 디스크립터를 소유하고 닫는 책임을 맡습니다.
        except (OSError, ValueError):
            raise GenerationError('invalid_config') from None
        with log:
            yield log
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _now(clock):
    try:
        value = clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise GenerationError('invalid_config')
        return value
    except Exception:
        raise GenerationError('invalid_config') from None


def _readiness(process, settings, host, deadline, poll_interval, probe, clock, wait):
    while True:
        remaining = deadline - _now(clock)
        if remaining <= 0:
            raise GenerationError('timeout')
        try:
            exited = process.poll() is not None
        except Exception:
            raise GenerationError('network') from None
        if exited:
            raise GenerationError('network')
        available = False
        try:
            envelope = probe(host, remaining)
            available = True
        except GenerationError as error:
            if error.code not in ('network', 'timeout'):
                raise GenerationError(error.code) from None
        except json.JSONDecodeError:
            raise GenerationError('invalid_json') from None
        except (OSError, TimeoutError, urllib.error.URLError):
            pass
        except (ValueError, TypeError):
            raise GenerationError('invalid_envelope') from None
        except Exception:
            raise GenerationError('network') from None
        remaining = deadline - _now(clock)
        if remaining <= 0:
            raise GenerationError('timeout')
        if available:
            models = _models(envelope)
            if not any(model.get('name') == settings.model
                       and isinstance(model.get('digest'), str) and model['digest'].strip()
                       and model['digest'] == settings.revision for model in models):
                raise GenerationError('invalid_config')
            # _readiness는 준비 상태 요청 후에도 모델 프로세스가 살아 있는지 확인합니다.
            try:
                exited = process.poll() is not None
            except Exception:
                raise GenerationError('network') from None
            if exited:
                raise GenerationError('network')
            return
        wait(min(poll_interval, remaining))


@contextmanager
def model_session(settings, *, key='', startup_timeout=30, poll_interval=.25,
                  launch=subprocess.Popen, choose_port=choose_loopback_port,
                  probe=probe_models, clock=time.monotonic, wait=time.sleep,
                  stop=stop_model):
    """model_session이 생성 요청용 접속 설정을 제공합니다.

    관리 모드에서는 모델 프로세스 그룹을 시작하고 준비 상태를 확인한 뒤 접속 설정을
    제공하며, 사용이 끝나면 소유한 그룹을 정리합니다. 외부 모드에서는 프로세스를
    시작하거나 정리하지 않습니다.
    """
    _validate_settings(settings, key, startup_timeout, poll_interval)
    result = {'provider': settings.provider, 'model': settings.model, 'url': settings.url,
              'key': key, 'backend_revision': settings.revision}
    if settings.mode == 'external':
        yield result
        return
    if not all(callable(port) for port in (launch, choose_port, probe, clock, wait, stop)):
        raise GenerationError('invalid_config')
    _validate_paths(settings)
    try:
        port = choose_port()
        if type(port) is not int or not 1 <= port <= 65535:
            raise GenerationError('invalid_config')
    except Exception:
        raise GenerationError('invalid_config') from None
    deadline = _now(clock) + startup_timeout
    if not math.isfinite(deadline):
        raise GenerationError('invalid_config')
    host = f'http://127.0.0.1:{port}'
    result['url'] = host + '/api/chat'
    env = dict(os.environ, OLLAMA_HOST=f'127.0.0.1:{port}',
               OLLAMA_MODELS=str(settings.models_dir), OLLAMA_NO_CLOUD='1',
               OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1')
    with _private_log(settings.log_dir) as log:
        try:
            process = launch([str(settings.binary), 'serve'], env=env, stdout=log,
                             stderr=log, start_new_session=True)
        except Exception:
            raise GenerationError('network') from None
        try:
            _readiness(process, settings, host, deadline, poll_interval, probe, clock, wait)
            yield result
        except BaseException:
            try:
                stop(process)
            except BaseException:
                # model_session은 종료 정리 실패가 주된 오류나 취소를 대체하지 않도록 합니다.
                pass
            raise
        else:
            stop(process)

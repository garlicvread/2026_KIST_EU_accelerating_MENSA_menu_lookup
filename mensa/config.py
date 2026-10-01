"""설정 로더가 TOML에 명시된 작업자 경로·추론·자원·공개 설정을 읽고 검증하며, 작업자를 준비하거나 시작하지 않습니다."""

from dataclasses import dataclass
from itertools import combinations
import math
import re
from pathlib import Path
import tomllib
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WorkerPaths:
    """WorkerPaths는 설정 파일 기준으로 해석된 체크아웃·비공개 상태·공개 출력 경로를 담으며, 세 디렉터리 트리는 서로 겹치지 않습니다."""

    checkout_dir: Path
    state_dir: Path
    public_dir: Path


@dataclass(frozen=True)
class InferenceSettings:
    """InferenceSettings는 공급자·모델·리비전과 추론 모드를 담고, 외부 모드에는 URL을, 관리 모드에는 실행 파일·모델·로그 경로를 담습니다."""

    mode: str
    provider: str
    model: str
    revision: str
    url: str | None
    binary: Path | None
    models_dir: Path | None
    log_dir: Path | None


@dataclass(frozen=True)
class ResourceSettings:
    """ResourceSettings는 플랫폼·최소 가용 메모리·CPU별 최대 부하를 담으며, 설정 로더는 실제 호스트 자원을 측정하지 않습니다."""

    platform: str
    min_available_bytes: int
    max_load_per_cpu: int | float


@dataclass(frozen=True)
class PublicationSettings:
    """PublicationSettings는 디렉터리 또는 GitHub 공개 방식과 해당 설정을 담으며, 명령 전송이나 원격 준비 상태를 확인하지 않습니다."""

    mode: str
    repository: str | None
    workflow: str | None
    remote: str | None


@dataclass(frozen=True)
class WorkerConfig:
    """WorkerConfig는 검증된 경로·추론·자원·공개 설정을 묶으며, 엔드포인트나 모델의 실행 준비 상태를 보장하지 않습니다."""

    paths: WorkerPaths
    inference: InferenceSettings
    resources: ResourceSettings
    publication: PublicationSettings


def _read_toml(config_path: Path) -> dict:
    try:
        content = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"Cannot read worker config {config_path}: {error}") from error
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Worker config {config_path} has invalid TOML: {error}") from error


def _require_keys(values: dict, names: tuple[str, ...], config_path: Path, section: str) -> None:
    missing = set(names) - set(values)
    unknown = set(values) - set(names)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing keys: " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown keys/tables: " + ", ".join(sorted(unknown)))
        raise ValueError(f"Worker config {config_path}, {section}: {'; '.join(details)}")


def _require_table(value: object, name: str, config_path: Path) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Worker config {config_path}: {name} must be a [{name}] table")
    return value


def _require_string(values: dict, name: str, config_path: Path, section: str) -> str:
    value = values[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Worker config {config_path}: {section}.{name} must be a nonempty string")
    return value


def _resolve_path(value: str, config_path: Path, name: str, kind: str) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            path = config_path.parent / path
        path = path.resolve()
        correct_kind = path.is_dir if kind == "directory" else path.is_file
        if path.exists() and not correct_kind():
            raise ValueError(f"{name} must be a {kind}, but {path} is an existing wrong-kind path")
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"Worker config {config_path}: invalid {name}: {error}") from error
    return path


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _parse_paths(value: object, config_path: Path) -> WorkerPaths:
    values = _require_table(value, "paths", config_path)
    names = ("checkout_dir", "state_dir", "public_dir")
    _require_keys(values, names, config_path, "[paths]")
    resolved = {
        name: _resolve_path(_require_string(values, name, config_path, "paths"), config_path, f"paths.{name}", "directory")
        for name in names
    }
    for first, second in combinations(names, 2):
        left, right = resolved[first], resolved[second]
        if _overlap(left, right):
            raise ValueError(
                f"Worker config {config_path}: paths.{first} ({left}) and paths.{second} "
                f"({right}) overlap; choose disjoint directory trees"
            )
    return WorkerPaths(**resolved)


def load_worker_paths(config_path: Path) -> WorkerPaths:
    """load_worker_paths가 paths 표만 포함하는 엄격한 TOML 설정을 읽으며 디렉터리를 생성하지 않습니다.

    상대 경로는 설정 파일의 디렉터리를 기준으로 해석합니다. 홈 디렉터리 및 환경 변수
    표기는 확장하지 않고 경로 구성 요소의 문자 그대로 취급합니다.
    """
    config = _read_toml(config_path)
    if set(config) != {"paths"}:
        extras = ", ".join(sorted(set(config) - {"paths"}))
        detail = f"; unknown root keys/tables: {extras}" if extras else ""
        raise ValueError(f"Worker config {config_path} must contain only [paths]{detail}")
    return _parse_paths(config["paths"], config_path)


def _validate_url(url: str, config_path: Path) -> str:
    try:
        if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
            raise ValueError("whitespace and control characters are forbidden")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise ValueError("must be an absolute http/https URL with a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are forbidden")
        if ":" not in parsed.hostname:
            # _validate_url은 URI 등록 이름 문법을 로컬에서 검증합니다. IDNA는 유효한 국제화 이름을
            # 사용할 수 있게 하며, 두 단계 모두 호스트의 주소를 조회하지 않습니다.
            hostname = parsed.hostname.encode("idna").decode("ascii")
            if not re.fullmatch(r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[0-9A-Fa-f]{2})+", hostname):
                raise ValueError("hostname contains invalid URI characters")
        if "#" in url:
            raise ValueError("fragments are forbidden")
        # _validate_url은 port 속성에 접근하여 숫자 형식과 범위를 검증합니다. 명시적인 빈 포트도
        # 거부하며, 포트 생략은 허용하고 스킴의 기본 포트를 사용합니다.
        parsed.port
        if parsed.netloc.endswith(":"):
            raise ValueError("port must be valid when supplied")
    except ValueError as error:
        raise ValueError(f"Worker config {config_path}: invalid inference.url: {error}") from error
    return url


def _parse_inference(value: object, paths: WorkerPaths, config_path: Path) -> InferenceSettings:
    values = _require_table(value, "inference", config_path)
    common = ("mode", "provider", "model", "revision")
    # _parse_inference는 모드별 허용 키를 검사하기 전에 managed/external 모드를 확정합니다.
    if "mode" not in values:
        raise ValueError(f"Worker config {config_path}, [inference]: missing keys: mode")
    mode = _require_string(values, "mode", config_path, "inference")
    if mode not in {"managed", "external"}:
        raise ValueError(f"Worker config {config_path}: inference.mode must be managed or external")
    managed_names = ("binary", "models_dir", "log_dir")
    _require_keys(values, common + (managed_names if mode == "managed" else ("url",)), config_path, "[inference]")
    identity = {name: _require_string(values, name, config_path, "inference") for name in common}
    providers = {"ollama"} if mode == "managed" else {"ollama", "openai"}
    if identity["provider"] not in providers:
        raise ValueError(f"Worker config {config_path}: invalid inference.provider for {mode} mode; expected {', '.join(sorted(providers))}")
    if mode == "external":
        url = _validate_url(_require_string(values, "url", config_path, "inference"), config_path)
        return InferenceSettings(**identity, url=url, binary=None, models_dir=None, log_dir=None)

    resolved = {
        name: _resolve_path(_require_string(values, name, config_path, "inference"), config_path, f"inference.{name}", "file" if name == "binary" else "directory")
        for name in managed_names
    }
    for name in ("models_dir", "log_dir"):
        for boundary in ("checkout_dir", "public_dir"):
            protected = getattr(paths, boundary)
            if _overlap(resolved[name], protected):
                raise ValueError(
                    f"Worker config {config_path}: inference.{name} ({resolved[name]}) and paths.{boundary} "
                    f"({protected}) overlap; choose disjoint directory trees"
                )
    binary = resolved["binary"]
    if binary == paths.public_dir or paths.public_dir in binary.parents:
        raise ValueError(f"Worker config {config_path}: inference.binary ({binary}) must not be inside paths.public_dir ({paths.public_dir})")
    return InferenceSettings(**identity, url=None, **resolved)


def _parse_resources(value: object, config_path: Path) -> ResourceSettings:
    values = _require_table(value, "resources", config_path)
    _require_keys(values, ("platform", "min_available_bytes", "max_load_per_cpu"), config_path, "[resources]")
    platform = _require_string(values, "platform", config_path, "resources")
    if platform not in {"linux", "macos"}:
        raise ValueError(f"Worker config {config_path}: resources.platform must be linux or macos")
    minimum = values["min_available_bytes"]
    if type(minimum) is not int or minimum <= 0:
        raise ValueError(f"Worker config {config_path}: resources.min_available_bytes must be a positive integer")
    load = values["max_load_per_cpu"]
    if type(load) not in (int, float) or load <= 0 or (isinstance(load, float) and not math.isfinite(load)):
        raise ValueError(f"Worker config {config_path}: resources.max_load_per_cpu must be a positive finite number")
    return ResourceSettings(platform=platform, min_available_bytes=minimum, max_load_per_cpu=load)


def _publication_remote(remote: str, config_path: Path) -> str:
    try:
        if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in remote):
            raise ValueError("whitespace and control characters are forbidden")
        if Path(remote).is_absolute():
            return remote
        parsed = urlsplit(remote)
        if parsed.scheme != "https" or "?" in remote or "#" in remote:
            raise ValueError("expected an absolute local path or https URL without query/fragment")
        # _publication_remote는 검토된 URL 문법·자격 증명·포트 검사를 재사용하며 네트워크를 탐색하지 않습니다.
        _validate_url(remote, config_path)
    except ValueError as error:
        raise ValueError(f"Worker config {config_path}: invalid publication.remote: {error}") from error
    return remote


def _parse_publication(value: object, config_path: Path) -> PublicationSettings:
    values = _require_table(value, "publication", config_path)
    if "mode" not in values:
        raise ValueError(f"Worker config {config_path}, [publication]: missing keys: mode")
    mode = _require_string(values, "mode", config_path, "publication")
    if mode not in {"directory", "github"}:
        raise ValueError(f"Worker config {config_path}: publication.mode must be directory or github")
    names = ("mode",) if mode == "directory" else ("mode", "repository", "workflow", "remote")
    _require_keys(values, names, config_path, "[publication]")
    if mode == "directory":
        return PublicationSettings(mode=mode, repository=None, workflow=None, remote=None)
    repository = _require_string(values, "repository", config_path, "publication")
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", repository) is None:
        raise ValueError(f"Worker config {config_path}: invalid publication.repository; expected owner/name")
    workflow = _require_string(values, "workflow", config_path, "publication")
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml", workflow) is None:
        raise ValueError(f"Worker config {config_path}: invalid publication.workflow; expected a .yml/.yaml basename")
    remote = _publication_remote(_require_string(values, "remote", config_path, "publication"), config_path)
    return PublicationSettings(mode=mode, repository=repository, workflow=workflow, remote=remote)


def load_worker_config(config_path: Path) -> WorkerConfig:
    """load_worker_config가 전체 작업자 TOML 설정을 검증하며 호스트 탐색, 실행 환경 준비 또는 작업 시작은 수행하지 않습니다.

    작업자 및 추론 경로는 설정 파일의 디렉터리를 기준으로 해석하고 심볼릭 링크를
    따라갑니다. 홈 디렉터리 및 환경 변수 표기는 문자 그대로 취급합니다. 공개 대상의
    remote 문자열은 저장소 식별 정보와 별개로 정확히 유지합니다. 설정 선언만으로
    엔드포인트, 모델 가중치 또는 원격 준비 상태를 확인하거나 작업자를 활성화하지 않습니다.
    """
    config = _read_toml(config_path)
    _require_keys(config, ("paths", "inference", "resources", "publication"), config_path, "root")
    paths = _parse_paths(config["paths"], config_path)
    inference = _parse_inference(config["inference"], paths, config_path)
    resources = _parse_resources(config["resources"], config_path)
    publication = _parse_publication(config["publication"], config_path)
    return WorkerConfig(paths=paths, inference=inference, resources=resources, publication=publication)

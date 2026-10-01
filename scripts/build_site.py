"""build_site가 검증된 메뉴·번역과 컴파일된 프런트엔드를 새 정적 사이트 디렉터리에 구성합니다.

TypeScript 컴파일은 호출자가 먼저 수행해야 하며, 이 빌더는 이미 생성된 JavaScript를
읽습니다. main.js에서 정적 상대 경로로 도달하는 모듈만 묶어 내용별 경로에 저장하고,
DirectoryPublisher가 메뉴·번역 쌍과 current.json을 설치합니다. 호출자는 입력과
겹치지 않는 새 출력 경로를 선택해야 합니다. 실패 시 이 호출이 생성한 출력 디렉터리를
정리하지만, 출력 준비 중 생성한 상위 디렉터리는 남을 수 있습니다.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from mensa.publication import validate_publication
from mensa.releases import DirectoryPublisher
from scripts.notices import load_glossary


ROOT = Path(__file__).resolve().parents[1]
# 다음 정규식은 부작용 전용 import를 포함한 한 줄짜리 컴파일 출력의 가져오기·재내보내기를 인식합니다.
_BINDINGS = r'[\w$*,{} \t]+'
_SPECIFIER = r'''(?P<quote>["'])(?P<path>[^"'\r\n\\]+)(?P=quote)'''
_IMPORT = re.compile(rf'^[ \t]*import[ \t]+(?:{_BINDINGS}[ \t]+from[ \t]+)?{_SPECIFIER}[ \t]*;[ \t]*$', re.M)
_REEXPORT = re.compile(rf'^[ \t]*export[ \t]+(?:\*(?:[ \t]+as[ \t]+[\w$]+)?|\{{[\w$, \t]*\}})[ \t]+from[ \t]+{_SPECIFIER}[ \t]*;[ \t]*$', re.M)
_LOCAL_EXPORT = re.compile(r'^[ \t]*export[ \t]+\{[\w$, \t]*\}[ \t]*;[ \t]*$', re.M)
# _dependencies는 지원하지 않는 import/export 토큰을 찾기 전에 주석과 문자열을 가립니다.
_LITERALS = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')


def _require_kind(path, predicate):
    """path를 lstat으로 확인하여 필요한 파일 유형이 아니거나 없으면 ValueError를 발생시킵니다."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise ValueError(f'Missing build input: {path}') from error
    if not predicate(mode):
        raise ValueError(f'Unexpected build input type (including symlink): {path}')


def _public_input(root, relative):
    """root 아래 상대 경로의 디렉터리·일반 파일을 확인한 뒤 파일 바이트를 읽습니다.

    중간 디렉터리와 최종 파일의 심볼릭 링크를 거부하여 가져오기 경로가 다른 파일을
    가리키는 상태를 받아들이지 않습니다. root 자체의 유형 검사는 build_site가 수행합니다.
    """
    path = root
    for part in relative.parts[:-1]:
        path /= part
        _require_kind(path, stat.S_ISDIR)
    path /= relative.name
    _require_kind(path, stat.S_ISREG)
    return path.read_bytes()


def _dependencies(payload, name):
    """UTF-8 모듈 바이트에서 지원하는 정적 import·재내보내기 경로 목록을 반환합니다.

    name은 오류가 발생한 모듈을 표시할 때 사용합니다. 주석·문자열의 단어를 선언으로
    오인하지 않도록 가리고, 동적 import나 지원 범위를 벗어난 참조 문법은 거부합니다.
    제한된 컴파일 출력만 검사하며 JavaScript 전체를 해석하거나 실행하지 않습니다.
    """
    text = payload.decode('utf-8')
    masked = _LITERALS.sub(lambda match: ''.join('\n' if char == '\n' else ' ' for char in match[0]), text)
    # _dependencies가 지원하는 문법에는 템플릿 문자열 보간 안의 import가 포함되지 않습니다.
    if re.search(r'\bimport\s*\(', masked) or any(
            match[0].startswith('`') and '${' in match[0] and re.search(r'\bimport\s*\(', match[0])
            for match in _LITERALS.finditer(text)):
        raise ValueError(f'Unsupported dynamic import in {name}')
    declarations = [match for pattern in (_IMPORT, _REEXPORT) for match in pattern.finditer(text)
                    if masked[match.start():match.end()].lstrip().startswith(('import', 'export'))]
    local_exports = list(_LOCAL_EXPORT.finditer(text))
    for token in re.finditer(r'\bimport\b|\bexport\b(?=\s*[*{])', masked):
        if not any(match.start() <= token.start() < match.end() for match in declarations + local_exports):
            raise ValueError(f'Unsupported module reference syntax in {name}')
    return [match['path'] for match in declarations]


def _target(name, specifier):
    """name 모듈의 참조를 컴파일 트리 안의 상대 .js 경로로 정리하여 반환합니다.

    외부 패키지·비공개 경로·트리 밖으로 나가는 참조는 거부하여 빌드 대상 파일의 범위를
    제한합니다. 경로만 계산하며 실제 파일 읽기는 _compiled_graph가 수행합니다.
    """
    if not specifier.startswith(('./', '../')) or not specifier.endswith('.js'):
        raise ValueError(f'Expected a static relative .js reference: {specifier}')
    parts = specifier.split('/')
    if any(part not in ('.', '..') and (not re.fullmatch(r'[\w-]+(?:\.js)?', part) or
            part == 'node_modules' or part == 'private' or part.startswith('private.')) for part in parts):
        raise ValueError(f'Forbidden private or unsupported module path: {specifier}')
    relative = Path(os.path.normpath(str(name.parent / specifier)))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'Module reference leaves compiled tree: {specifier}')
    return relative


def _compiled_graph(compiled):
    """compiled/main.js부터 도달 가능한 모듈을 읽어 (그래프 SHA256, 경로별 바이트 사전)을 반환합니다.

    이미 읽은 모듈은 다시 따라가지 않으므로 순환 참조도 처리합니다. 정렬된 UTF-8 상대
    경로와 정확한 파일 바이트를 경로 길이·경로·본문 길이·본문 순서로 해시하며, 길이는
    8바이트 부호 없는 빅 엔디언 정수로 구분합니다. 이 값은 모듈 묶음의 주소를 정하여
    다른 내용의 파일이 같은 캐시 경로를 공유하지 않도록 합니다.
    """
    graph = {}
    pending = [Path('main.js')]
    while pending:
        name = pending.pop()
        if name in graph:
            continue
        payload = _public_input(compiled, name)
        graph[name] = payload
        pending.extend(_target(name, specifier) for specifier in _dependencies(payload, name))
    digest = hashlib.sha256()
    for name, payload in sorted(graph.items()):
        encoded = name.as_posix().encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big')); digest.update(encoded)
        digest.update(len(payload).to_bytes(8, 'big')); digest.update(payload)
    return digest.hexdigest(), graph


def _replace_reference(html, name, replacement):
    """HTML의 src·href에서 name과 정확히 같은 참조 하나를 replacement로 바꾼 문자열을 반환합니다.

    참조가 없거나 여러 개면 ValueError로 거부하여 실제 진입점과 다른 자산을 임의로
    선택하지 않습니다. 이 함수는 HTML 파일에 직접 쓰지 않습니다.
    """
    pattern = rf'(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>["\']){re.escape(name)}(?P=quote)'
    html, count = re.subn(pattern, lambda match: f'{match["prefix"]}{match["quote"]}{replacement}{match["quote"]}', html)
    if count != 1:
        raise ValueError(f'Expected one unversioned {name} reference in index.html')
    return html


def _mkdir_public(path):
    """필요한 상위 디렉터리를 만들고 이번 호출이 새로 만든 디렉터리에만 0755 권한을 설정합니다.

    이미 있는 경로는 디렉터리인지 검사하며 권한을 변경하지 않습니다. 빌드 실패 시
    출력 안에 생성한 디렉터리는 출력과 함께 삭제하지만, 출력 밖에 새로 만든 상위
    디렉터리는 정리하지 않습니다.
    """
    if path.exists():
        _require_kind(path, stat.S_ISDIR)
        return
    _mkdir_public(path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        # _mkdir_public은 다른 실행이 동시에 생성한 상위 디렉터리의 권한을 바꾸거나 해당 디렉터리를 삭제하지 않습니다.
        _require_kind(path, stat.S_ISDIR)
    else:
        path.chmod(0o755)


def _write_public(path, payload):
    """상위 디렉터리를 준비하고 payload 바이트를 새 0644 파일에 쓰며 기존 파일은 덮어쓰지 않습니다."""
    _mkdir_public(path.parent)
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(payload)


def build_site(source: Path, output: Path, *, compiled=ROOT / 'dist/frontend',
               glossary=ROOT / 'data/notice-translations.json') -> None:
    """입력 사이트·컴파일 결과·용어집에서 새 output 디렉터리를 만들며 반환값은 없습니다.

    메뉴·캐시·안내 번역의 공개 조건과 모듈 그래프를 출력 생성 전에 검사합니다.
    today=None으로 검사하므로 빌드가 메뉴 날짜의 최신성을 보증하지는 않습니다.
    HTML에는 내용별 모듈·CSS 주소를 넣고, app.js에는 해당 main.js를 가져오는 짧은
    진입 코드를 생성합니다. favicon은 원본 바이트를 복사하며 TypeScript를 컴파일하지
    않습니다. 기존 출력의 덮어쓰기를 거부하고, 실패 시 자신이 만든 output만 삭제합니다.
    """
    source, compiled, glossary, output = (Path(path).absolute() for path in (source, compiled, glossary, output))
    # build_site는 경로 해석 전에 존재 여부를 검사하여 대상이 없는 출력 심볼릭 링크도 기존 출력으로 거부합니다.
    if os.path.lexists(output):
        raise ValueError(f'Build output already exists: {output}; choose a fresh directory')
    for directory in (source, compiled, glossary.parent):
        _require_kind(directory, stat.S_ISDIR)
    _require_kind(glossary, stat.S_ISREG)
    source, compiled, glossary, output = (path.resolve() for path in (source, compiled, glossary, output))
    for tree in (source, compiled, glossary.parent):
        if tree == output or tree in output.parents or output in tree.parents:
            raise ValueError('Build output cannot overlap an input tree (inside or outside)')

    html = _public_input(source, Path('index.html')).decode('utf-8')
    styles = _public_input(source, Path('styles.css'))
    favicon = _public_input(source, Path('favicon.svg'))
    menu = json.loads(_public_input(source, Path('data/menu.json')), parse_constant=lambda value: _invalid_constant(value))
    cache = json.loads(_public_input(source, Path('data/translations.json')), parse_constant=lambda value: _invalid_constant(value))
    notices = load_glossary(glossary)
    validate_publication(menu, cache, notice_glossary=notices, today=None)
    # 그래프 전체의 내용이 주소에 포함되므로 하위 모듈이 바뀌어도 이전 파일을 캐시에서
    # 섞어 읽지 않게 합니다. CSS는 자신의 바이트만으로 별도의 주소를 계산합니다.
    graph_id, graph = _compiled_graph(compiled)
    main_url = f'assets/{graph_id}/main.js'
    styles_url = f'assets/styles-{hashlib.sha256(styles).hexdigest()}.css'
    html = _replace_reference(html, 'app.js', main_url)
    html = _replace_reference(html, 'styles.css', styles_url)
    publisher = DirectoryPublisher(output, notice_glossary=notices)

    _mkdir_public(output.parent)
    # build_site는 mkdir에 성공해야 출력 디렉터리의 정리 책임을 얻습니다. 다른 실행이 만든
    # 디렉터리는 정리 블록에 진입하기 전에 예외를 일으키므로 그대로 보존합니다.
    output.mkdir()
    try:
        output.chmod(0o755)
        for name, payload in graph.items():
            _write_public(output / 'assets' / graph_id / name, payload)
        _write_public(output / styles_url, styles)
        _write_public(output / 'styles.css', styles)
        _write_public(output / 'app.js', f"import './{main_url}';\n".encode('utf-8'))
        _write_public(output / 'favicon.svg', favicon)
        publisher.publish(menu, cache, today=None)
        _write_public(output / 'index.html', html.encode('utf-8'))
    except BaseException:
        shutil.rmtree(output)
        raise


def _invalid_constant(value):
    """JSON의 NaN·Infinity 같은 유한하지 않은 숫자 토큰을 ValueError로 거부합니다."""
    raise ValueError(f'Nonfinite JSON number: {value}')


def main() -> None:
    """빌드 경로 인자를 읽어 build_site를 호출하고 결과 경로 또는 실패 종료를 알립니다.

    이 명령은 준비된 입력을 파일로 구성하며 컴파일·네트워크 공개 명령을 실행하지 않습니다.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'site')
    parser.add_argument('--output', type=Path, default=ROOT / '.tmp/pages')
    parser.add_argument('--compiled', type=Path, default=ROOT / 'dist/frontend')
    parser.add_argument('--glossary', type=Path, default=ROOT / 'data/notice-translations.json')
    args = parser.parse_args()
    try:
        build_site(args.source, args.output, compiled=args.compiled, glossary=args.glossary)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Site build failed: {error}\n')
    print(f'Built Pages at {args.output}')


if __name__ == '__main__':
    main()

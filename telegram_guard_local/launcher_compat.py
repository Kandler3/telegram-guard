"""Offline capability checks for automatically discovered Codex installations."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .files import read_json, write_json
from .model import GuardError

CHECK_REVISION = 1
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class CompatibilityError(ValueError):
    pass


SAMPLES = {
    'initialize': {'clientInfo': {'name': 'telegram_guard_compatibility', 'version': '1'},
                   'capabilities': {'experimentalApi': True}},
    'thread/loaded/list': {'cursor': None, 'limit': 1},
    'thread/read': {'threadId': 'compatibility-check', 'includeTurns': False},
    'thread/name/set': {'threadId': 'compatibility-check', 'name': 'compatibility-check'},
    'turn/start': {'threadId': 'compatibility-check', 'input': [], 'toolOutput': {
        'namespace': 'telegram_guard', 'name': 'telegram_incoming', 'output': '{}'}},
}


def digest(binary):
    with Path(binary).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def isolated_environment(home):
    # No account, native-service pipes, agent context, API keys or user config.
    keep = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH', 'TEMP', 'TMP',
            'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
            'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(home / 'sqlite'),
               RUST_LOG='error', LOG_FORMAT='json', DO_NOT_TRACK='1')
    return env


def object_shapes(node, document, seen=()):
    """Resolve only bundle-local references; never fetch remote schemas."""
    reference = node.get('$ref')
    if reference:
        if not reference.startswith('#/') or reference in seen:
            raise CompatibilityError('Не удалось проверить локальную схему протокола Codex.')
        target = document
        for part in reference[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        yield from object_shapes(target, document, (*seen, reference))
    if 'properties' in node:
        yield node['properties']
    for keyword in ('oneOf', 'anyOf', 'allOf'):
        for variant in node.get(keyword, []):
            yield from object_shapes(variant, document, seen)


def check_schema(document):
    from jsonschema import validators
    from jsonschema.exceptions import SchemaError
    # The generated bundle must be self-contained. Refuse external references
    # before constructing a validator, which otherwise may retrieve them.
    def inspect(value):
        if isinstance(value, dict):
            if '$ref' in value and not value['$ref'].startswith('#/'):
                raise CompatibilityError('Схема Codex требует внешних ссылок; проверка остановлена.')
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    inspect(document)
    validator_class = validators.validator_for(document)
    try:
        validator_class.check_schema(document)
    except SchemaError as error:
        raise CompatibilityError('Codex предоставил некорректную схему протокола.') from error
    validator = validator_class(document)
    variants = list(object_shapes(document, document))
    for method, params in SAMPLES.items():
        matches = [shape for shape in variants if
                   method in shape.get('method', {}).get('enum', []) or
                   shape.get('method', {}).get('const') == method]
        if not matches:
            raise CompatibilityError(f'В протоколе Codex отсутствует необходимый метод: {method}.')
        params_shapes = [shape for match in matches for shape in
                         object_shapes(match.get('params', {}), document)]
        # Validation alone would accept a removed optional field when the
        # schema allows additionalProperties. Require explicit declarations.
        if not any(set(params) <= set(shape) for shape in params_shapes):
            raise CompatibilityError(f'Изменились необходимые поля протокола Codex: {method}.')
        if method == 'turn/start':
            tool_shapes = [shape for parent in params_shapes if 'toolOutput' in parent
                           for shape in object_shapes(parent['toolOutput'], document)]
            if not any({'name', 'namespace', 'output'} <= set(shape) for shape in tool_shapes):
                raise CompatibilityError('Codex больше не объявляет необходимую доставку toolOutput.')
        samples = [params]
        if method == 'turn/start':
            samples.append(dict(params, toolOutput={
                'namespace': 'agent_wake', 'name': 'event_completed', 'output': '{}'}))
        for sample in samples:
            if not validator.is_valid({'id': 'compatibility-check', 'method': method, 'params': sample}):
                raise CompatibilityError(f'Codex изменил формат необходимого запроса: {method}.')


def probe(binary):
    """Generate protocol descriptors only; never start a model or load an account."""
    with tempfile.TemporaryDirectory(prefix='codex-compat-') as folder:
        root = Path(folder)
        home = root / 'home'
        home.mkdir()
        (home / 'config.toml').write_text('[features]\nplugins=false\n[analytics]\nenabled=false\n',
                                        encoding='utf-8')
        schema_dir = root / 'schema'
        result = subprocess.run([str(binary), 'app-server', 'generate-json-schema',
                                 '--experimental', '--out', str(schema_dir)],
                                cwd=home, env=isolated_environment(home), capture_output=True,
                                timeout=30, creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise CompatibilityError('Не удалось получить схему протокола нового Codex. Повторите запуск после завершения обновления.')
        path = schema_dir / 'ClientRequest.json'
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise CompatibilityError('Codex не предоставил поддерживаемую схему протокола.')
        check_schema(json.loads(path.read_text(encoding='utf-8')))


def ensure_compatible(home, binary, *, persist=False):
    """Cache positive results by executable content, not its name/version string."""
    signature = digest(binary)
    receipt = Path(home) / 'launcher-compatibility.json'
    try:
        cached = read_json(receipt)
    except (OSError, ValueError, GuardError):
        cached = {}
    expected = {'check_revision': CHECK_REVISION, 'cli_sha256': signature,
                'methods': sorted(SAMPLES)}
    if cached == expected:
        return 'cached'
    try:
        probe(binary)
    except CompatibilityError:
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise CompatibilityError('Не удалось завершить локальную проверку совместимости Codex. Повторите запуск; если ошибка сохраняется, требуется диагностика.') from error
    if digest(binary) != signature:
        raise CompatibilityError('Codex обновился во время проверки. Повторите запуск после завершения обновления.')
    if persist:
        write_json(receipt, expected)
    return 'verified'

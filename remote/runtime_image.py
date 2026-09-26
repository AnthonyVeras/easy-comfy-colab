"""Pacote privado de instalação: Drive -> disco local, com compatibilidade estrita."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

ROOT = Path('/content/comfy-colab')
DRIVE = Path('/content/drive/MyDrive/ComfyColab')
STORE = DRIVE / '.runtime-images'
COMPONENTS = ('ComfyUI-Easy-Install', 'mcp-venv')
INSTALL_REVISION = '2a979fae03ac6c4adc607634b0ef8432e80ecc3e 1568e6cfd04586a4b3c4e1817ea7dde09b1bf9e7'
SCHEMA = 1
GIB = 1024 ** 3
LEGACY_RECIPE = 'f02db254fbbbc9a9058cfe7be46a62a0f41dd32e3be24bdbf9b271f26b08b00a'


def log(message):
    print(message, flush=True)


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def fingerprint():
    # Base global do Colab, não o ambiente adicional que vamos restaurar.
    packages = sorted((d.metadata['Name'].lower(), d.version) for d in importlib.metadata.distributions())
    return dict(python=list(sys.version_info[:3]), abi=sysconfig.get_config_var('SOABI'),
                machine=platform.machine(), libc=list(platform.libc_ver()),
                executable=str(Path(sys.executable).resolve()),
                base_packages_sha256=hashlib.sha256(json.dumps(packages).encode()).hexdigest(),
                torch=importlib.metadata.version('torch'))


def recipe():
    # Mudanças no botão/empacotador não invalidam a imagem já validada da 2.0.4.
    files = ('install.sh', 'mcp_http.py')
    return hashlib.sha256(('layout-1:' + ''.join(digest(Path(__file__).with_name(f)) for f in files)).encode()).hexdigest()


def image_pointer(base, revision):
    for candidate in (revision, LEGACY_RECIPE):
        pointer = STORE / (key_for(base, candidate) + '.json')
        if pointer.is_file():
            return pointer, candidate
    return STORE / (key_for(base, revision) + '.json'), revision


def key_for(base, revision):
    return hashlib.sha256(json.dumps([SCHEMA, base, revision], sort_keys=True).encode()).hexdigest()[:24]


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, timeout=120, **kwargs).stdout.strip()


def validate(environment):
    py = environment / COMPONENTS[0] / '.venv/bin/python'
    # Sem inferência e sem inicializar CUDA; usa os pacotes como instalados.
    code = ('import torch, onnx, google.protobuf, safetensors, aiohttp, yaml; '
            'from importlib.metadata import version; '
            'print(torch.__version__)')
    result = run([str(py), '-c', code])
    if result.splitlines()[-1] != importlib.metadata.version('torch'):
        raise ValueError('A imagem não preservou o PyTorch da base.')
    run([str(environment / 'mcp-venv/bin/python'), '-c',
         'from importlib.metadata import version; import comfy_mcp.server; '
         'assert version("comfy-mcp")=="0.10.0"; assert version("comfy-cli")=="1.21.0"'])
    if not (environment / COMPONENTS[0] / 'ComfyUI/main.py').is_file():
        raise ValueError('ComfyUI ausente na imagem.')


def safe_members(archive, limit=None):
    """Não extrai links, dispositivos ou caminhos arbitrários do arquivo recebido."""
    seen = set()
    total = 0
    for member in archive:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or path.parts[0] not in COMPONENTS or '\\' in member.name
                or member.name in seen or not (member.isfile() or member.isdir())):
            raise ValueError('Conteúdo inválido na imagem: caminho, link ou tipo não permitido.')
        seen.add(member.name)
        total += member.size
        if member.size < 0 or (limit is not None and total > limit):
            raise ValueError('Imagem excede o tamanho declarado.')
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        member.mode &= 0o755
        yield member


def recreate_links(destination):
    for name in (COMPONENTS[0] + '/.venv', 'mcp-venv'):
        venv = destination / name
        for binary in ('python', 'python3', f'python{sys.version_info.major}.{sys.version_info.minor}'):
            target = venv / 'bin' / binary
            if target.exists() or target.is_symlink():
                raise ValueError('A imagem inclui um interpretador inesperado.')
            target.symlink_to(Path(sys.executable).resolve())
        (venv / 'lib64').symlink_to('lib')


def bind_drive(destination):
    comfy = destination / COMPONENTS[0] / 'ComfyUI'
    for name in ('models', 'input', 'user'):
        target = comfy / name
        (DRIVE / name).mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == (DRIVE / name).resolve():
                continue
            raise ValueError('A imagem contém dados pessoais onde deveria haver um link.')
        target.symlink_to(DRIVE / name)


def restore():
    base, revision = fingerprint(), recipe()
    key = key_for(base, revision)
    pointer, source_recipe = image_pointer(base, revision)
    if not pointer.is_file():
        log('Sem imagem compatível no Drive. Usando instalação convencional nesta sessão.')
        return False
    metadata = json.loads(pointer.read_text())
    if metadata.get('base') != base or metadata.get('recipe') != source_recipe or metadata.get('schema') != SCHEMA:
        raise ValueError('Imagem incompatível com a base do Colab.')
    checksum = metadata.get('sha256', '')
    if not re.fullmatch('[0-9a-f]{64}', checksum) or metadata.get('archive') != checksum + '.tar.gz':
        raise ValueError('Manifesto da imagem inválido.')
    required = metadata.get('unpacked_bytes')
    packed = metadata.get('bytes')
    if type(required) is not int or type(packed) is not int or min(required, packed) <= 0:
        raise ValueError('Tamanho de imagem inválido.')
    if required + packed + 2 * GIB > shutil.disk_usage(ROOT).free:
        raise ValueError('Disco insuficiente para restaurar a imagem.')
    # Recusa instalação sobre arquivos existentes. Uma VM em uso nunca é substituída.
    if any((ROOT / component).exists() for component in COMPONENTS):
        raise ValueError('Já existe um ambiente nesta VM; restauração não aplicada sobre ele.')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='image-restore-', dir=ROOT) as directory:
        stage = Path(directory)
        local = stage / 'runtime.tar.gz'
        log('Restaurando imagem do Drive: copiando pacote para o disco da VM...')
        shutil.copyfile(STORE / metadata['archive'], local)
        if local.stat().st_size != packed or digest(local) != checksum:
            raise ValueError('Imagem incompleta ou corrompida. A instalação convencional será usada.')
        log(f'Pacote recebido e verificado em {time.monotonic() - started:.1f}s. Extraindo...')
        extract = stage / 'unpacked'
        extract.mkdir()
        with tarfile.open(local, 'r:gz') as archive:
            archive.extractall(extract, members=safe_members(archive, required), filter='data')
        recreate_links(extract)
        validate(extract)
        moved = []
        try:
            for component in COMPONENTS:
                (extract / component).rename(ROOT / component)
                moved.append(component)
            bind_drive(ROOT)
            # Marcador só é publicado depois de extração e validação bem sucedidas.
            (ROOT / 'installed.ok').write_text(INSTALL_REVISION)
            atomic_json(ROOT / 'runtime-image-active.json', dict(key=key, sha256=checksum, restored_seconds=round(time.monotonic()-started, 1)))
            if metadata.get('warnings'):
                log('Avisos do pacote: ' + '; '.join(metadata['warnings']))
        except Exception:
            for component in reversed(moved):
                (ROOT / component).rename(extract / component)
            (ROOT / 'installed.ok').unlink(missing_ok=True)
            raise
    log(f'Imagem do Drive restaurada em {time.monotonic()-started:.1f}s.')
    return True


def copy_repo(source, target, *, core=False):
    """Somente arquivos versionados, incluindo patches locais; exclui dados de usuário."""
    paths = run(['git', '-C', str(source), 'ls-files', '-z']).split('\0')
    for relative in paths:
        if not relative:
            continue
        rel = PurePosixPath(relative)
        if core and rel.parts[0] in {'models', 'input', 'output', 'user', 'temp', '.comfy-downloads', 'custom_nodes'}:
            continue
        if any(part in {'.env', 'credentials.json', 'token.json', 'tokens.json', 'secrets.json'} or part.startswith('.env.') for part in rel.parts):
            continue
        file = source / relative
        if file.is_file() and not file.is_symlink():
            out = target / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, out)
    # Mantém os commits locais sem copiar hooks, logs, credenciais ou config pessoal.
    git = source / '.git'
    if not git.is_dir():
        raise ValueError('Repositório sem metadados Git.')
    out = target / '.git'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('HEAD', 'index', 'packed-refs', 'shallow', 'objects', 'refs'):
        src = git / name
        if src.is_dir():
            shutil.copytree(src, out / name, symlinks=False)
        elif src.is_file():
            shutil.copy2(src, out / name)
    remote = run(['git', '-C', str(source), 'remote', 'get-url', 'origin'])
    if not re.fullmatch(r'https://(?:github|gitlab)\.com/[\w.-]+/[\w.-]+(?:\.git)?', remote):
        raise ValueError('URL Git não aceita no pacote.')
    (out / 'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = false\n[remote "origin"]\n\turl = '+remote+'\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return run(['git', '-C', str(source), 'rev-parse', 'HEAD'])


def node_files(node):
    """Inclui nodes do Registry/ZIP e arquivos locais; não copia dados de usuário."""
    ignored = {'.git', '__pycache__', '.cache', '.venv', 'venv', 'node_modules',
               'input', 'output', 'outputs', 'user', 'temp', 'logs', '.comfy-downloads'}
    sensitive = {'.env', 'credentials.json', 'token.json', 'tokens.json', 'secrets.json'}
    if node.name in ignored or node.name == 'comfy_colab_remote_download':
        return
    if node.is_symlink():
        raise ValueError('Node com link externo não empacotado: ' + node.name)
    if node.is_file():
        if node.suffix == '.py':
            yield node
        return
    for directory, dirs, files in os.walk(node, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ignored)
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError('Link de diretório no node: ' + node.name + '/' + name)
        for name in sorted(files):
            if name in sensitive or name.startswith('.env.') or name.endswith(('.pyc', '.log', '.partial', '.part')):
                continue
            file = Path(directory) / name
            if file.is_symlink():
                raise ValueError('Link de arquivo no node: ' + node.name + '/' + name)
            yield file


def copy_node(node, target):
    if node.is_symlink():
        raise ValueError('Node com link externo não empacotado: ' + node.name)
    revision = copy_repo(node, target) if (node / '.git').is_dir() else 'local/registry'
    for file in node_files(node):
        output = target if file == node else target / file.relative_to(node)
        output.parent.mkdir(parents=True, exist_ok=True)
        before = file.stat()
        shutil.copy2(file, output)
        if (before.st_size, before.st_mtime_ns) != (file.stat().st_size, file.stat().st_mtime_ns):
            raise ValueError('Node mudou durante a cópia; aguarde a instalação terminar.')
    return revision


def source_signature():
    # ponytail: varredura de metadados local; hashes completos só se houver editores que preservem mtime.
    h = hashlib.sha256()
    for relative in (COMPONENTS[0] + '/.venv', 'mcp-venv', COMPONENTS[0] + '/ComfyUI'):
        source = ROOT / relative
        for directory, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in {'__pycache__', '.cache', '.git', 'input', 'output', 'outputs', 'user', 'temp', 'logs', '.comfy-downloads'})
            for name in sorted(files):
                if name.endswith(('.pyc', '.log', '.partial', '.part')):
                    continue
                file = Path(directory) / name
                st = file.lstat()
                h.update(str((str(file.relative_to(ROOT)), st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()


def stage_current(stage):
    """Copia a instalação ativa. Ajustes são feitos exclusivamente nessa cópia."""
    install = ROOT / COMPONENTS[0]
    for relative in (COMPONENTS[0] + '/.venv', 'mcp-venv'):
        shutil.copytree(ROOT / relative, stage / relative, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.cache'))
    comfy = install / 'ComfyUI'
    target = stage / COMPONENTS[0] / 'ComfyUI'
    revisions = {'ComfyUI': copy_repo(comfy, target, core=True)}
    for node in sorted((comfy / 'custom_nodes').iterdir()):
        if node.name in {'__pycache__', '.cache', '.git', 'comfy_colab_remote_download'}:
            continue
        if node.is_dir() or node.suffix == '.py':
            revisions[node.name] = copy_node(node, target / 'custom_nodes' / node.name)
    # O plugin do app é sincronizado pelo start.sh; não congelar histórico pessoal.
    return revisions


def publish(stage, revisions, warnings, expected_source=None):
    validate(stage)
    base, revision = fingerprint(), recipe()
    key = key_for(base, revision)
    archive = stage.parent / 'runtime.tar.gz'
    unpacked = 0
    log('Compactando imagem da instalação atual...')
    with tarfile.open(archive, 'w:gz', compresslevel=1, dereference=False) as tar:
        for component in COMPONENTS:
            for file in sorted((stage / component).rglob('*')):
                if file.is_symlink():
                    # Apenas links conhecidos de venv são reconstruídos; nenhum link de dados entra.
                    rel = file.relative_to(stage).as_posix()
                    if file.name == 'lib64' or ('/bin/' in rel and file.name.startswith('python')):
                        continue
                    raise ValueError('Link inesperado na imagem: ' + rel)
                if file.is_file():
                    before = file.stat()
                    tar.add(file, arcname=file.relative_to(stage).as_posix(), recursive=False)
                    if file.stat().st_mtime_ns != before.st_mtime_ns:
                        raise ValueError('Arquivo mudou durante o empacotamento.')
                    unpacked += before.st_size
    checksum = digest(archive)
    STORE.mkdir(parents=True, exist_ok=True)
    if archive.stat().st_size + GIB > shutil.disk_usage(STORE).free:
        raise ValueError('Sem espaço no Drive para a imagem.')
    log(f'Enviando imagem ao Drive ({archive.stat().st_size / GIB:.2f} GiB)...')
    target = STORE / (checksum + '.tar.gz')
    temporary = target.with_suffix('.partial')
    shutil.copyfile(archive, temporary)
    log('Verificando imagem no Drive antes de substituir a versão anterior...')
    if digest(temporary) != checksum:
        raise ValueError('Falha na verificação da cópia no Drive.')
    if expected_source is not None and (source_signature() != expected_source or fingerprint() != base):
        temporary.unlink(missing_ok=True)
        raise ValueError('A instalação mudou durante a atualização. Aguarde as instalações terminarem e tente novamente.')
    temporary.replace(target)
    metadata = dict(schema=SCHEMA, base=base, recipe=revision, archive=target.name, sha256=checksum,
                    bytes=archive.stat().st_size, unpacked_bytes=unpacked,
                    revisions=revisions, warnings=warnings, created=time.time(), source_signature=expected_source)
    atomic_json(STORE / (key + '.json'), metadata)
    log('Imagem pronta no Drive. Será usada automaticamente na próxima VM compatível.')
    return metadata


def build(if_missing=False):
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'runtime-image-build.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Outra imagem está sendo preparada. Aguarde a conclusão e tente novamente.')
        key = key_for(fingerprint(), recipe())
        active = ROOT / 'runtime-image-active.json'
        if (if_missing and active.is_file() and json.loads(active.read_text()).get('key') == key
                and image_pointer(fingerprint(), recipe())[0].is_file()):
            log('O Drive já tem uma imagem para esta instalação e base do Colab.')
            return
        if shutil.disk_usage(ROOT).free < 12 * GIB:
            raise ValueError('São necessários 12 GiB livres para preparar a imagem.')
        log('Preparando imagem em uma cópia separada do ambiente...')
        if not (DRIVE.parent).is_dir():
            raise ValueError('Google Drive não está montado; imagem não atualizada.')
        original = source_signature()
        atomic_json(ROOT / 'runtime-image-build-result.json', dict(ok=False, state='building'))
        with tempfile.TemporaryDirectory(prefix='image-build-', dir=ROOT) as directory:
            stage = Path(directory) / 'environment'
            revisions = stage_current(stage)
            log('Validando dependências da cópia (a sessão ativa permanece intacta)...')
            warnings = []
            failures = ROOT / 'node-install-failures.txt'
            if failures.is_file():
                entries = failures.read_text().splitlines()
                unknown = [e for e in entries if e != 'ComfyUI-fish-audio-s2: clone']
                if unknown:
                    raise ValueError('Instalação incompleta; corrija os nodes antes de criar a imagem: ' + '; '.join(unknown))
                if entries:
                    warnings.append('FishAudioS2 não incluído: repositório original indisponível (404).')
            result = publish(stage, revisions, warnings, expected_source=original)
            atomic_json(ROOT / 'runtime-image-build-result.json', dict(ok=True, **result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['restore', 'build'])
    parser.add_argument('--if-missing', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'restore':
            return 0 if restore() else 2
        build(args.if_missing)
        return 0
    except Exception as exc:
        log('Imagem do ambiente: ' + str(exc))
        if args.action == 'build':
            atomic_json(ROOT / 'runtime-image-build-result.json', dict(ok=False, error=str(exc)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

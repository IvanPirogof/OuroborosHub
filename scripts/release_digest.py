"""Portable sha256v2 identity; never import or execute a skill or the core runtime.

Contract: ouroboros.skill_identity.payload_digest and skill_loader._iter_payload_files.
Keep the walk (including inert assets and declared entries under cache directories),
top-level note exclusions, reduction and install-spec normalization in sync with core.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import stat

from .install_specs import install_specs_hash, normalize_install_specs

DIGEST_PREFIX = 'sha256v2:'
PER_INSTALL_NOTE_FILENAMES = frozenset({
    '.seed-origin', '.ouroboroshub.json', '.clawhub.json', '.self_authored.json',
})
CACHE_NAMES = frozenset({
    '__pycache__', 'node_modules', '.git', '.hg', '.svn', '.idea', '.vscode',
    '.tox', '.pytest_cache', '.ouroboros_env', '.DS_Store',
})
SENSITIVE_NAMES = frozenset({
    '.env', '.env.local', '.env.production', '.env.staging', '.env.development',
    '.env.dev', '.env.test', 'credentials.json', 'service-account.json',
    'secrets.yaml', 'secrets.json', 'secrets.toml', 'secrets.ini',
    'aws-credentials.json', 'gcp-service-account.json', 'id_rsa', 'id_ed25519',
    'id_ecdsa', 'id_dsa', '.git-credentials', '.netrc', '.npmrc', '.pypirc',
})


def payload_files(skill_dir: Path, *, manifest_entry='', manifest_scripts=None) -> list[Path]:
    """The loader's sorted, confined regular-file walk in ordinary runtime mode."""
    root = skill_dir.resolve()
    files: set[Path] = set()
    pending = [root] if root.is_dir() else []
    candidates = []
    while pending:
        for path in pending.pop().glob('*'):
            if path.name in CACHE_NAMES:
                continue
            if path.is_dir() and not path.is_symlink():
                pending.append(path)
            else:
                candidates.append(path)
    for path in sorted(candidates):
        if not path.is_file():
            continue
        if (path.name == '.seed-origin' and path.parent == root
                and root.parent.name == 'native'):
            continue
        if path.name.lower() in SENSITIVE_NAMES or path.name.lower().endswith('.env'):
            raise ValueError(f'{path.relative_to(root)}: credential-shaped payload file')
        try:
            real = path.resolve()
        except (OSError, RuntimeError):
            continue
        if real.is_relative_to(root):
            files.add(path)

    def add_declared(value):
        relative = str(value or '').strip()
        if not relative or relative.startswith(('/', '~')) or '..' in PurePosixPath(relative).parts:
            return
        resolved = (root / relative).resolve()
        try:
            if resolved.is_relative_to(root) and stat.S_ISREG(resolved.stat().st_mode):
                files.add(resolved)
        except (FileNotFoundError, NotADirectoryError):
            pass

    add_declared(manifest_entry)
    for script in manifest_scripts or []:
        if isinstance(script, dict):
            name = str(script.get('name') or '').strip()
            if name:
                add_declared(name)
                if '/' not in name:
                    add_declared(f'scripts/{name}')
    return [p for p in sorted(files) if not (
        p.parent == root and p.name in PER_INSTALL_NOTE_FILENAMES)]


def file_facts(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def payload_digest(skill_dir: Path, *, manifest_entry='', manifest_scripts=None, install_specs=None) -> str:
    root = skill_dir.resolve()
    digest = hashlib.sha256()
    for path in payload_files(root, manifest_entry=manifest_entry, manifest_scripts=manifest_scripts):
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(bytes.fromhex(file_facts(path)[0]))
    auto, _manual, _warnings = normalize_install_specs(install_specs or [])
    if auto:
        digest.update(b'\0install_specs\0')
        digest.update(install_specs_hash(auto).encode('utf-8'))
    return DIGEST_PREFIX + digest.hexdigest()

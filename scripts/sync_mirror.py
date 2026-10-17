#!/usr/bin/env python3
"""Publish the generated catalog from main, then mirror that same commit explicitly."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_index import build_catalog, json_bytes, run_git

AUTHOR = ['-c', 'user.name=Ouroboros', '-c', 'user.email=311266734+ouroboros-agent@users.noreply.github.com']


def git(root, args, token=None):
    env = dict(os.environ)
    if token:
        # The token never appears in the URL, argv, Git config on disk or output.
        auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {auth}')
    return run_git(args, root, env=env)


def sync(root, repo_token, mirror_token):
    if not repo_token or not mirror_token:
        raise ValueError('GITHUB_TOKEN and JOILAB_HUB_MIRROR_PAT must be configured')
    if git(root, ['branch', '--show-current']) != 'main':
        raise ValueError('catalog publication must run on the main branch')
    catalog = root / 'catalog.json'
    generated = json_bytes(build_catalog(root))
    if not catalog.is_file() or catalog.read_bytes() != generated:
        catalog.write_bytes(generated)
        git(root, ['add', '--', 'catalog.json'])
        git(root, [*AUTHOR, 'commit', '-m', 'Regenerate the legacy skill catalog'])
        git(root, ['push', 'https://github.com/razzant/OuroborosHub.git', 'HEAD:refs/heads/main'], repo_token)
    # GITHUB_TOKEN pushes do not trigger another push workflow. This is deliberately
    # in the same job, including when Owner Updates merged using GITHUB_TOKEN.
    git(root, ['push', 'https://github.com/joi-lab/OuroborosHub.git', 'HEAD:refs/heads/main'], mirror_token)


def main():
    try:
        sync(Path(__file__).resolve().parents[1], os.environ.get('GITHUB_TOKEN'), os.environ.get('MIRROR_PAT'))
    except (OSError, ValueError, RuntimeError):
        print('Catalog/mirror publication failed; check configuration and rerun from current main', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

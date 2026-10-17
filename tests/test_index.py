"""Canonical hub inputs, release bytes and old-client projection."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_catalog as wrapper
from scripts import build_index as builder
from scripts.release_digest import PER_INSTALL_NOTE_FILENAMES, payload_digest, payload_files

ROOT = Path(__file__).resolve().parents[1]
CORE = Path('/Users/anton/worktrees/ouroboros/skills-hub-v4-20261017')
CORE_PYTHON = Path('/Users/anton/Desktop/Ouroboros.app/Contents/Resources/python-standalone/bin/python3')


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-index-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'entries').mkdir()
        (self.root / 'skills').mkdir()
        self.write('hub.json', {'schema_version': 2, 'name': 'OuroborosHub', 'maintainer': 'razzant'})
        self.write('categories.json', {'schema_version': 1, 'categories': [
            {'id': 'search', 'label': 'Search & research'}, {'id': 'writing', 'label': 'Writing & review'}]})
        self.write('official.json', {'schema_version': 1, 'maintainer': 'razzant', 'endorsements': []})
        self.entry = self.add('demo')

    def write(self, name, value):
        (self.root / name).write_bytes(builder.json_bytes(value))

    def add(self, name):
        directory = self.root / 'skills' / name
        directory.mkdir()
        (directory / 'SKILL.md').write_text(
            f'---\nname: {name}\nversion: "nightly blue"\ntype: instruction\n---\nSkill body\n', encoding='utf-8')
        entry = {'schema_version': 2, 'name': name, 'description': 'Curated card text', 'category': ['search'],
                 'owner': 'publisher', 'version': 'nightly blue', 'source': {'kind': 'hosted', 'path': f'skills/{name}'},
                 'digest': payload_digest(directory), 'withdrawn': None, 'when_to_use': 'When research is needed.'}
        self.write(f'entries/{name}.json', entry)
        return entry

    def test_deterministic_sorted_projection_and_compatible_cli(self):
        self.add('aaa')
        self.add('zzz')
        self.assertEqual(wrapper.main([], root=self.root), 0)
        path = self.root / 'catalog.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(['--check'], root=self.root), 0)
            self.assertEqual(wrapper.main(['--check'], root=self.root), 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(builder.json_bytes(builder.build_catalog(self.root)), before[0])
        catalog = builder.read_json(path)
        self.assertEqual([r['slug'] for r in catalog['skills']], ['aaa', 'demo', 'zzz'])
        self.assertEqual(catalog['skills'][1]['description'], self.entry['description'])
        self.assertEqual(catalog['skills'][1]['when_to_use'], self.entry['when_to_use'])
        self.assertNotIn('owner', catalog['skills'][1])
        builder.validate_catalog(self.root, catalog)

    def test_check_refuses_stale_missing_and_malformed_inputs_without_rewriting(self):
        builder.main([], root=self.root)
        path = self.root / 'catalog.json'
        for raw in (b'{}', path.read_bytes() + b'\n'):
            path.write_bytes(raw)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main(['--check'], root=self.root), 1)
            self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main(['--check'], root=self.root), 1)
        self.assertFalse(path.exists())
        for text in ('{', '[]', '{"schema_version":2,"schema_version":2}'):
            (self.root / 'hub.json').write_text(text, encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(builder.main([], root=self.root), 1)
            self.assertFalse(path.exists())

    def test_schema_guards_accept_valid_shapes_and_refuse_invalid_ones(self):
        builder.validate_entry(self.entry, {'search', 'writing'})
        valid = copy.deepcopy(self.entry)
        valid.update(category=['search', 'writing'], withdrawn={'reason': 'Replaced', 'at': '2026-10-17'})
        builder.validate_entry(valid, {'search', 'writing'})
        mutations = [
            {'schema_version': True}, {'schema_version': 1}, {'owner': ''}, {'owner': 'two people'},
            {'category': []}, {'category': ['other']}, {'category': ['search'] * 2},
            {'category': ['search', 'writing', 'search']}, {'category': [{}]}, {'category': 'search'},
            {'name': '../escape'}, {'name': 'x' * 65}, {'description': ''}, {'version': 7},
            {'digest': 'a' * 64}, {'digest': 'sha256v2:' + 'G' * 64}, {'install_specs': {}},
            {'when_to_use': 1}, {'type': 'unknown'}, {'withdrawn': True},
            {'withdrawn': {'reason': 'why', 'at': '2026-02-30'}},
            {'withdrawn': {'reason': '', 'at': '2026-01-01'}},
            {'withdrawn': {'reason': 'why', 'at': '20261017'}},
            {'withdrawn': {'reason': 'why', 'at': '2026-01-01', 'extra': True}},
            {'source': {'kind': 'hosted', 'path': 'skills/another'}},
            {'source': {'kind': 'hosted', 'path': '../demo'}}, {'source': {}},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.validate_entry({**self.entry, **changes}, {'search', 'writing'})
        for key in self.entry:
            row = dict(self.entry)
            del row[key]
            if key != 'when_to_use':
                with self.subTest(missing=key), self.assertRaises(ValueError):
                    builder.validate_entry(row, {'search'})

    def test_category_and_entry_uniqueness(self):
        for rows in ([], [{'id': 'search', 'label': 'Search'}] * 2, [{'id': 'bad id', 'label': 'Search'}]):
            self.write('categories.json', {'schema_version': 1, 'categories': rows})
            with self.assertRaises(ValueError):
                builder.load_hub(self.root)
        self.write('categories.json', {'schema_version': 1, 'categories': [{'id': 'search', 'label': 'Search'}]})
        self.write('entries/duplicate.json', self.entry)
        with self.assertRaises(ValueError):
            builder.load_hub(self.root)
        (self.root / 'entries/duplicate.json').unlink()
        (self.root / 'entries/demo.json').rename(self.root / 'entries/wrong.json')
        with self.assertRaisesRegex(ValueError, 'filename'):
            builder.load_hub(self.root)

    def test_endorsements_bind_exact_release_and_recommendations_are_boolean(self):
        endorsement = {'name': 'demo', 'digest': self.entry['digest'], 'recommended': True}
        def write(rows, maintainer='razzant'):
            self.write('official.json', {'schema_version': 1, 'maintainer': maintainer, 'endorsements': rows})
        write([endorsement])
        builder.build_catalog(self.root)
        for rows in ([endorsement] * 2, [{**endorsement, 'name': 'missing'}],
                     [{**endorsement, 'digest': 'sha256v2:' + '0' * 64}], [{**endorsement, 'recommended': 1}]):
            write(rows)
            with self.assertRaises(ValueError):
                builder.build_catalog(self.root)
        write([], 'different')
        with self.assertRaises(ValueError):
            builder.build_catalog(self.root)

    def test_payload_change_requires_digest_and_manifest_version_agrees(self):
        builder.main([], root=self.root)
        before = (self.root / 'catalog.json').read_bytes()
        payload = self.root / 'skills/demo/asset.bin'
        payload.write_bytes(b'\x00inert asset\xff')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main([], root=self.root), 1)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(builder.main(['--digest', 'demo'], root=self.root), 0)
        self.entry['digest'] = printed.getvalue().strip()
        self.write('entries/demo.json', self.entry)
        self.assertEqual(builder.main([], root=self.root), 0)
        self.entry['version'] = 'different'
        self.write('entries/demo.json', self.entry)
        with self.assertRaisesRegex(ValueError, 'version'):
            builder.build_catalog(self.root)

    def test_hosted_path_missing_and_escape(self):
        directory = self.root / 'skills/demo'
        directory.rename(self.root / 'hidden')
        with self.assertRaisesRegex(ValueError, 'hosted path'):
            builder.build_catalog(self.root)
        directory.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'hosted path'):
            builder.build_catalog(self.root)

    def test_withdrawn_and_left_source_retain_legacy_delivery(self):
        self.entry['withdrawn'] = {'reason': 'Use another release', 'at': '2026-10-17'}
        self.write('entries/demo.json', self.entry)
        self.assertEqual(builder.build_catalog(self.root)['skills'][0]['slug'], 'demo')
        self.add('other')
        (self.root / 'entries/demo.json').unlink()
        self.assertEqual([r['slug'] for r in builder.build_catalog(self.root)['skills']], ['demo', 'other'])

    def test_git_schema_exact_pin_and_digest_verification(self):
        source = {'kind': 'git', 'repo': 'https://github.com/author/repo', 'path': '', 'commit': 'a' * 40}
        self.entry['source'] = source
        builder.validate_entry(self.entry, {'search'})
        for changes in ({'commit': 'main'}, {'commit': 'a' * 39}, {'path': '/root'}, {'path': '../x'},
                        {'repo': 'https://secret@github.com/a/b'}, {'repo': 'file:///tmp/repo'}, {'path': 'a\\b'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.validate_entry({**self.entry, 'source': {**source, **changes}}, {'search'})
        self.write('entries/demo.json', self.entry)
        # A pointer still checks fetched bytes; its old raw payload remains a v1 row.
        with patch.object(builder, 'source_directory', return_value=contextlib.nullcontext(self.root / 'skills/demo')):
            self.assertEqual(builder.build_catalog(self.root)['skills'][0]['slug'], 'demo')
            self.entry['digest'] = 'sha256v2:' + '0' * 64
            self.write('entries/demo.json', self.entry)
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                builder.build_catalog(self.root)


class ReleaseIdentityTests(unittest.TestCase):
    def test_walk_notes_cache_assets_declared_entries_and_dependency_identity(self):
        with tempfile.TemporaryDirectory(prefix='hub-digest-') as tmp:
            root = Path(tmp)
            (root / 'SKILL.md').write_text('payload', encoding='utf-8')
            initial = payload_digest(root)
            for name in PER_INSTALL_NOTE_FILENAMES:
                (root / name).write_text('machine note', encoding='utf-8')
            (root / '__pycache__').mkdir()
            (root / '__pycache__/ignored.pyc').write_bytes(b'cache')
            self.assertEqual(payload_digest(root), initial)
            (root / 'nested').mkdir()
            (root / 'nested/.clawhub.json').write_text('payload note', encoding='utf-8')
            self.assertNotEqual(payload_digest(root), initial)
            (root / 'data.wasm').write_bytes(b'\0asset')
            (root / '.env.example').write_text('example', encoding='utf-8')
            names = [p.relative_to(root).as_posix() for p in payload_files(root)]
            self.assertEqual(names, ['.env.example', 'SKILL.md', 'data.wasm', 'nested/.clawhub.json'])
            self.assertIn('__pycache__/ignored.pyc', [p.relative_to(root).as_posix() for p in payload_files(
                root, manifest_entry='__pycache__/ignored.pyc')])
            base = payload_digest(root)
            self.assertNotEqual(payload_digest(root, install_specs=[{'kind': 'pip', 'package': 'httpx'}]), base)
            self.assertEqual(payload_digest(root, install_specs=[{'kind': 'brew', 'package': 'x'}]), base)
            (root / '.env').write_text('synthetic', encoding='utf-8')
            with self.assertRaises(ValueError):
                payload_digest(root)

    @unittest.skipUnless((CORE / 'ouroboros/skill_identity.py').is_file() and CORE_PYTHON.is_file(),
                         'designated core checkout/interpreter absent; cross-repository digest check skipped')
    def test_all_hosted_releases_match_core_read_only(self):
        rows = [builder.read_json(path) for path in sorted((ROOT / 'entries').glob('*.json'))]
        cases = [{'path': str(ROOT / r['source']['path']), 'specs': r.get('install_specs'),
                  'front': builder.read_manifest(ROOT / r['source']['path'])} for r in rows if r['source']['kind'] == 'hosted']
        code = '''import json,sys
from pathlib import Path
from ouroboros.skill_identity import payload_digest
cases=json.load(sys.stdin)
print(json.dumps([payload_digest(Path(c['path']), manifest_entry=c['front'].get('entry',''),
    manifest_scripts=c['front'].get('scripts'), install_specs=c['specs']) for c in cases]))
'''
        with tempfile.TemporaryDirectory(prefix='hub-core-identity-') as tmp:
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': tmp, 'PYTHONPATH': str(CORE),
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                   'OUROBOROS_APP_ROOT': tmp, 'OUROBOROS_DATA_DIR': tmp + '/data',
                   'OUROBOROS_REPO_DIR': tmp + '/repo', 'OUROBOROS_SETTINGS_PATH': tmp + '/settings.json'}
            run = subprocess.run([str(CORE_PYTHON), '-B', '-c', code], input=json.dumps(cases),
                                 text=True, encoding='utf-8', capture_output=True, cwd=tmp, env=env, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        expected = json.loads(run.stdout.strip().splitlines()[-1])
        actual = [payload_digest(Path(c['path']), manifest_entry=c['front'].get('entry', ''),
                  manifest_scripts=c['front'].get('scripts'), install_specs=c['specs']) for c in cases]
        self.assertEqual(actual, expected)
        self.assertEqual(actual, [r['digest'] for r in rows if r['source']['kind'] == 'hosted'])

    def test_initial_membership_ownership_categories_and_official_split(self):
        membership = builder.read_json(ROOT / 'tests/fixtures/initial_membership.json')
        hub, entries, official = builder.load_hub(ROOT)
        self.assertEqual(len(membership), 50)
        self.assertTrue(set(membership) <= set(entries))
        for name, facts in membership.items():
            self.assertEqual({k: entries[name][k] for k in facts}, facts)
        categories = builder.read_json(ROOT / 'categories.json')['categories']
        self.assertEqual(len(categories), 12)
        expected = set('a2a adaptive-code-review anti-slop cache_efficiency_snapshot claudexor_quotas confluence '
                       'context-lens duckduckgo email email-presence google-workspace keenable memory-atlas perplexity '
                       'slack-bridge telegram-bot pdf_tools word_tools'.split())
        self.assertEqual({r['name'] for r in official['endorsements']}, expected)
        self.assertEqual({r['name'] for r in official['endorsements'] if r.get('recommended')},
                         set('duckduckgo pdf_tools word_tools anti-slop memory-atlas'.split()))
        generated = builder.build_catalog(ROOT)
        self.assertEqual({r['slug'] for r in generated['skills']}, set(entries))
        self.assertEqual(sum('install_specs' in e for e in entries.values()), 11)
        self.assertEqual(sum('when_to_use' in e for e in entries.values()), 26)

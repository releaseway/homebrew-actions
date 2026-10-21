"""Behavior checks for Git-tree equivalence and offline proof verification."""
import copy
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

M = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/release-inputs.py'))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git('init', '-q', '-b', 'main')
        for name, value in [('user.name', 'test'), ('user.email', 'test@example.invalid'),
                            ('commit.gpgsign', 'false'), ('core.hooksPath', '/dev/null')]:
            self.git('config', name, value)
        (self.root / 'README.md').write_text('baseline documentation\n')
        (self.root / 'source.py').write_text('print(1)\n')
        self.base = self.commit()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def docs(self):
        (self.root / 'README.md').write_text('new paragraphs\n\nnew title\n')
        return self.commit()

    def test_markdown_proof_replays_without_git_or_network(self):
        candidate = self.docs()
        proof = M['compare'](self.root, self.base, candidate)
        self.assertEqual(M['verify'](proof, self.base, candidate), proof['fingerprint'])
        self.assertEqual(M['classify'](self.root, self.base, candidate)['mode'], 'docs')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/new.md').write_text('additional guide')
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'docs')

    def test_unknown_code_workflow_example_test_lock_or_hidden_file_requires_full(self):
        for name in ['other.md', 'src/main.ts', 'test/docs.test.mjs', 'examples/workflow.yml',
                     '.github/workflows/test.yml', '.npmrc', 'pnpm-lock.yaml']:
            self.git('reset', '--hard', self.base)
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('changed input')
            self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'full', name)

    def test_deleted_and_renamed_documents_only(self):
        self.git('mv', 'README.md', 'CONTRIBUTING.md')
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'docs')
        self.git('mv', 'CONTRIBUTING.md', 'runtime.py')
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'full')

    def test_executable_document_symlink_and_submodule_are_not_ignored(self):
        (self.root / 'README.md').chmod(0o755)
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'full')
        self.git('reset', '--hard', self.base)
        (self.root / 'link').symlink_to('source.py')
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'full')
        (self.root / 'link').unlink()
        self.git('reset', '--hard', self.base)
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.base + ',module')
        self.git('commit', '-q', '-m', 'submodule')
        self.assertEqual(M['classify'](self.root, self.base, self.git('rev-parse', 'HEAD'))['mode'], 'full')

    def test_incomplete_or_tampered_proof_fails(self):
        candidate = self.docs()
        proof = M['compare'](self.root, self.base, candidate)
        for change in [
            lambda p: p.__setitem__('fingerprint', '0' * 64),
            lambda p: p['candidate']['trees'].clear(),
            lambda p: p['ancestry'].clear(),
            lambda p: p['candidate'].__setitem__('object', 'eA=='),
            lambda p: p.__setitem__('policy', 'caller-ignore-everything'),
        ]:
            bad = copy.deepcopy(proof)
            change(bad)
            with self.assertRaises((ValueError, KeyError)):
                M['verify'](bad, self.base, candidate)
        with self.assertRaises(ValueError):
            M['verify'](proof, self.base, 'a' * 40)

    def test_missing_baseline_objects_or_nonancestor_requires_full(self):
        self.assertEqual(M['classify'](self.root, None, self.base)['mode'], 'full')
        self.assertEqual(M['classify'](self.root, 'a' * 40, self.base)['mode'], 'full')
        self.git('checkout', '--orphan', 'other')
        (self.root / 'README.md').write_text('unrelated history')
        candidate = self.commit()
        self.assertEqual(M['classify'](self.root, self.base, candidate)['mode'], 'full')

    def test_large_document_change_uses_complete_tree(self):
        docs = self.root / 'docs'
        docs.mkdir()
        for index in range(3200):
            (docs / (str(index) + '.md')).write_text('guide')
        candidate = self.commit()
        self.assertEqual(M['classify'](self.root, self.base, candidate)['mode'], 'docs')
        (self.root / '.new-runtime-config').write_text('must not disappear after a long file list')
        self.assertEqual(M['classify'](self.root, self.base, self.commit())['mode'], 'full')


if __name__ == '__main__':
    unittest.main()

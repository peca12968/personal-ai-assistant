import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import checkpoint_state as checkpoint


class CheckpointStorageTests(unittest.TestCase):
    def test_failed_reference_commit_preserves_last_complete_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference.json'
            checkpoint.commit_bytes(path, b'first committed reference')
            with patch.object(checkpoint.os, 'replace', side_effect=OSError('simulated write failure')):
                with self.assertRaises(OSError):
                    checkpoint.commit_bytes(path, b'uncommitted')
            self.assertEqual(path.read_bytes(), b'first committed reference')
            self.assertEqual(list(path.parent.iterdir()), [path])
            checkpoint.commit_bytes(path, b'next committed reference')
            self.assertEqual(path.read_bytes(), b'next committed reference')

    def test_reader_uses_only_the_pinned_digest_and_ignores_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sha = checkpoint.digest(b'expected bytes')
            (folder/'newer.partial').write_bytes(b'expected bytes')
            with self.assertRaises(FileNotFoundError):
                checkpoint.read(folder, sha, {})
            (folder/(sha+'.safetensors')).write_bytes(b'damaged')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                checkpoint.read(folder, sha, {})
            with self.assertRaisesRegex(ValueError, 'identifier'):
                checkpoint.read(folder, '../elsewhere', {})

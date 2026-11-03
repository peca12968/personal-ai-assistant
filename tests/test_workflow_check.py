import hashlib
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import check_workflow as checker,publish


class WorkflowCheckTests(unittest.TestCase):
    def artifact(self, entries=None):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            for name,content in entries or [('actionlint.exe',b'synthetic executable')]:
                archive.writestr(name,content)
        return stream.getvalue()

    def test_bad_download_never_creates_executable_or_cached_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'checksum'):
                checker.ensure_tool(tmp,fetch=lambda:b'wrong bytes')
            self.assertFalse((Path(tmp)/checker.ASSET).exists())
            self.assertFalse((Path(tmp)/'bin/actionlint.exe').exists())

    def test_verified_cache_restores_changed_binary_without_network(self):
        archive=self.artifact([('actionlint.exe',b'synthetic executable'),('../outside.txt',b'ignored')])
        with tempfile.TemporaryDirectory() as tmp,patch.object(checker,'ASSET_SHA256',hashlib.sha256(archive).hexdigest()):
            cache=Path(tmp)/'cache'
            binary=checker.ensure_tool(cache,fetch=lambda:archive)
            self.assertEqual(binary.read_bytes(),b'synthetic executable')
            self.assertFalse((Path(tmp)/'outside.txt').exists())
            binary.write_bytes(b'changed executable')
            with patch.object(checker,'download',side_effect=AssertionError('unexpected network')):
                self.assertEqual(checker.ensure_tool(cache).read_bytes(),b'synthetic executable')
            (cache/checker.ASSET).write_bytes(b'corrupt archive')
            with self.assertRaisesRegex(ValueError,'checksum'):checker.ensure_tool(cache)

    def test_missing_executable_and_missing_workflows_fail(self):
        archive=self.artifact([('other.exe',b'wrong entry')])
        with tempfile.TemporaryDirectory() as tmp,patch.object(checker,'ASSET_SHA256',hashlib.sha256(archive).hexdigest()):
            with self.assertRaisesRegex(ValueError,'one bounded executable'):
                checker.ensure_tool(tmp,fetch=lambda:archive)
            with patch.object(checker.platform,'machine',return_value='AMD64'):
                with self.assertRaisesRegex(ValueError,'No GitHub workflows'):checker.check(tmp)

    def test_workflow_failure_propagates_and_publication_stops_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'.github/workflows';folder.mkdir(parents=True)
            source=folder/'with spaces.yml';source.write_text('invalid: [',encoding='utf-8')
            error=subprocess.CalledProcessError(1,['actionlint'])
            with patch.object(checker,'ensure_tool',return_value=root/'tool.exe'),patch.object(checker.subprocess,'run',side_effect=error) as run:
                with self.assertRaises(subprocess.CalledProcessError):checker.check(root)
                self.assertEqual(run.call_args.args[0][-1],str(source))
                self.assertTrue(run.call_args.kwargs['check'])
            with patch.object(publish.sys,'argv',['publish.py']),patch.object(publish,'run',side_effect=error) as run:
                with self.assertRaises(subprocess.CalledProcessError):publish.main()
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.args[1],'scripts/check_workflow.py')

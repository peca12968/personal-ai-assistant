import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish


class PublishEncodingTests(unittest.TestCase):
    def test_nested_cli_chinese_diagnostics_decode_without_shell_environment_setup(self):
        text = '已確認不成立。'
        code = (
            'import subprocess,sys; '
            'message=sys.argv[1]; '
            'result=subprocess.run([sys.executable,"-c",'
            '"import sys;sys.stderr.write(sys.argv[1])",message],'
            'capture_output=True,encoding="utf-8",check=True); '
            'assert result.stderr==message; sys.stdout.write(result.stderr)'
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(publish, 'ROOT', Path(tmp)), \
                patch.dict(os.environ, {'PYTHONUTF8': '0'}):
            self.assertEqual(publish.run(sys.executable, '-c', code, text, capture=True), text)
            self.assertEqual(os.environ['PYTHONUTF8'], '0')

"""Public-check failures must identify the problem without logging sensitive content."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PublicCheckTests(unittest.TestCase):
    def test_rejected_content_is_not_echoed_to_logs(self) -> None:
        # Synthetic fixtures, assembled so this test file remains safe to publish.
        cases = [
            ("API 令牌", "token = " + "ghp_" + "0" * 36),
            ("私钥", "-----BEGIN " + "PRIVATE KEY-----"),
            ("本机绝对路径", "/" + "Users/example/private/"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture.txt"
            for label, content in cases:
                with self.subTest(category=label):
                    fixture.write_text(content + "\n", encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, str(Path(__file__).with_name("check_public.py")), str(fixture)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"{fixture}:1: {label}", result.stdout)
                    self.assertFalse(content in result.stdout + result.stderr,
                                     "diagnostics must not echo rejected content")


if __name__ == "__main__":
    unittest.main()

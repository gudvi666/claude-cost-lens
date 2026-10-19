import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


class ServerStartupTests(unittest.TestCase):
    def test_random_port_is_reported_and_serves_summary(self):
        with tempfile.TemporaryDirectory() as root:
            process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve().parents[1] / "app.py"),
                 "--data-root", root, "--host", "127.0.0.1", "--port", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                line = process.stdout.readline().strip()
                self.assertTrue(line.startswith("Claude Cost Lens: "), line)
                url = line[len("Claude Cost Lens: "):]
                self.assertGreater(urlparse(url).port, 0)
                with urllib.request.urlopen(url + "api/summary", timeout=5) as response:
                    data = json.load(response)
                self.assertEqual(data["session_count"], 0)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()

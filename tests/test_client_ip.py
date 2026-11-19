import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app  # noqa: E402


class PickClientIpTest(unittest.TestCase):
    def test_without_proxy_flag_uses_peer(self):
        self.assertEqual(app.pick_client_ip("1.2.3.4", "10.0.0.1", False), "10.0.0.1")

    def test_with_proxy_uses_last_forwarded_address(self):
        self.assertEqual(app.pick_client_ip("fake, 1.2.3.4, 9.9.9.9", "10.0.0.1", True), "9.9.9.9")

    def test_missing_header_falls_back_to_peer(self):
        self.assertEqual(app.pick_client_ip(None, "10.0.0.1", True), "10.0.0.1")

    def test_garbage_value_falls_back_to_peer(self):
        self.assertEqual(app.pick_client_ip("evil<script>", "10.0.0.1", True), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()

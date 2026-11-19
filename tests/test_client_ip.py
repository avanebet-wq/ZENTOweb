import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app  # noqa: E402


class PickClientIpTest(unittest.TestCase):
    def test_without_proxy_flag_uses_peer(self):
        self.assertEqual(app.pick_client_ip("1.2.3.4", "10.0.0.1", False), "10.0.0.1")

    def test_with_proxy_uses_real_ip_header(self):
        self.assertEqual(app.pick_client_ip("9.9.9.9", "10.0.0.1", True), "9.9.9.9")

    def test_missing_header_falls_back_to_peer(self):
        self.assertEqual(app.pick_client_ip(None, "10.0.0.1", True), "10.0.0.1")

    def test_garbage_value_falls_back_to_peer(self):
        self.assertEqual(app.pick_client_ip("evil<script>", "10.0.0.1", True), "10.0.0.1")


class DbRateLimiterTest(unittest.TestCase):
    def test_counts_survive_new_limiter_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rate.db"
            clock = [1000.0]
            first = app.RateLimiter(clock=lambda: clock[0], db_path=db)
            self.assertTrue(first.try_hit("orders", "1.2.3.4", 2, 60))
            self.assertTrue(first.try_hit("orders", "1.2.3.4", 2, 60))
            self.assertFalse(first.try_hit("orders", "1.2.3.4", 2, 60))
            # «перезапуск» сервера: новый объект, та же база
            second = app.RateLimiter(clock=lambda: clock[0], db_path=db)
            self.assertFalse(second.try_hit("orders", "1.2.3.4", 2, 60))
            self.assertTrue(second.try_hit("orders", "5.6.7.8", 2, 60))
            clock[0] += 61
            self.assertTrue(second.try_hit("orders", "1.2.3.4", 2, 60))

    def test_admin_failures_recorded_in_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rate.db"
            clock = [5000.0]
            limiter = app.RateLimiter(clock=lambda: clock[0], db_path=db)
            for _ in range(app.ADMIN_FAIL_LIMIT):
                limiter.record("admin", "1.2.3.4", app.ADMIN_FAIL_WINDOW)
            self.assertTrue(app.RateLimiter(clock=lambda: clock[0], db_path=db)
                            .is_over("admin", "1.2.3.4", app.ADMIN_FAIL_LIMIT, app.ADMIN_FAIL_WINDOW))


if __name__ == "__main__":
    unittest.main()

"""Ники только из английских букв и цифр: старые «плохие» ники при запуске заменяются случайными."""

import sqlite3
import sys
from pathlib import Path

from test_images import ProfileServerBase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import store  # noqa: E402


class NickResetTest(ProfileServerBase):
    def nicks(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, nick, nick_lower FROM users")}
        finally:
            conn.close()

    def run_reset(self):
        conn = store.connect(self.db_path)
        try:
            store._reset_bad_nicks(conn)
        finally:
            conn.close()

    def test_bad_nicks_become_random_and_good_stay(self):
        good = self.make_user("Anna77")
        ru = self.make_user("Анна")
        sym = self.make_user("a_b_c")
        self.run_reset()
        nicks = self.nicks()
        self.assertEqual(nicks[good], ("Anna77", "anna77"))
        for uid in (ru, sym):
            nick, lower = nicks[uid]
            self.assertRegex(nick, r"^user\d{8}$")
            self.assertEqual(nick, lower)
        self.assertNotEqual(nicks[ru][0], nicks[sym][0])

    def test_second_run_changes_nothing(self):
        self.make_user("Анна")
        self.run_reset()
        before = self.nicks()
        self.run_reset()
        self.assertEqual(self.nicks(), before)

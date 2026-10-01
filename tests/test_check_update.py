"""Offline tests for the update loop (no network, no Discord).
Run with: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check_update  # noqa: E402

REAL_FORTNITE = next(g for g in check_update.GAMES if g["slug"] == "fortnite")


class MainLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "version_data.json")
        self.version = "1.0"
        self.sent = []
        self.patches = [
            mock.patch.object(check_update, "STATE_FILE", self.state_path),
            mock.patch.object(check_update, "DISCORD_TOKEN", "token"),
            mock.patch.object(check_update, "CHANNEL_ID", "123"),
            mock.patch.object(check_update, "GAMES", [{
                "slug": "game", "name": "Game",
                "check": lambda: {"version": self.version, "release": self.version},
                "embed": lambda info: {"title": info["release"]},
            }]),
            mock.patch.object(check_update, "send_discord_embed", side_effect=self._send),
        ]
        for p in self.patches:
            p.start()
        self.send_ok = True

    def tearDown(self):
        for p in reversed(self.patches):  # undo in reverse so stacked patches restore cleanly
            p.stop()
        self.tmp.cleanup()

    def _send(self, embed):
        self.sent.append(embed)
        return self.send_ok

    def run_once(self):
        check_update.main()
        with open(self.state_path, encoding="utf-8") as f:
            return f.read()

    def test_first_run_seeds_without_notifying(self):
        state = json.loads(self.run_once())
        self.assertEqual(state["game"]["version"], "1.0")
        self.assertEqual(self.sent, [])

    def test_quiet_run_leaves_state_file_unchanged(self):
        first = self.run_once()
        second = self.run_once()
        self.assertEqual(first, second, "no version change must mean no diff (no commit)")
        self.assertEqual(self.sent, [])

    def test_new_version_notifies_and_updates_state(self):
        self.run_once()
        self.version = "1.1"
        state = json.loads(self.run_once())
        self.assertEqual([e["title"] for e in self.sent], ["1.1"])
        self.assertEqual(state["game"]["version"], "1.1")

    def test_failed_send_keeps_old_version_for_retry(self):
        self.run_once()
        self.version = "2.0"
        self.send_ok = False
        with self.assertRaises(SystemExit):
            check_update.main()
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["game"]["version"], "1.0")


class Cs2CheckTests(unittest.TestCase):
    def test_picks_first_patchnotes_item(self):
        news = {"appnews": {"newsitems": [
            {"gid": "1", "title": "Event", "tags": ["event"]},
            {"gid": "2", "title": "Release Notes", "url": "u", "tags": ["patchnotes"]},
        ]}}
        with mock.patch.object(check_update, "fetch_json", return_value=news):
            self.assertEqual(check_update.cs2_check(),
                             {"version": "2", "title": "Release Notes", "url": "u"})


AES_4230 = {"data": {"build": "++Fortnite+Release-42.30-CL-58557680"}}
AES_4220 = {"data": {"build": "++Fortnite+Release-42.20-CL-58011042"}}


def fake_fetch(epic=None, aes=None):
    def fetch(url):
        if url == check_update.EPIC_VERSION_URL:
            return epic
        if url == check_update.FORTNITE_AES_URL:
            return aes
        return None  # news: no flavour text needed
    return fetch


class FortniteCheckTests(unittest.TestCase):
    def check(self, epic, aes):
        with mock.patch.object(check_update, "fetch_json", side_effect=fake_fetch(epic, aes)):
            return check_update.fortnite_check()

    def test_epic_release_wins_while_community_api_lags(self):
        info = self.check({"version": "42.30"}, AES_4220)
        self.assertEqual((info["version"], info["cl"]), ("42.30", "?"))

    def test_build_number_filled_in_once_community_api_catches_up(self):
        info = self.check({"version": "42.30"}, AES_4230)
        self.assertEqual((info["version"], info["cl"]), ("42.30", "58557680"))

    def test_falls_back_to_community_api_when_epic_is_down(self):
        info = self.check(None, AES_4230)
        self.assertEqual((info["version"], info["cl"]), ("42.30", "58557680"))

    def test_nothing_reachable(self):
        self.assertIsNone(self.check(None, None))


class FortniteCompareTests(unittest.TestCase):
    cmp = staticmethod(check_update.fortnite_compare)

    def test_old_full_build_string_state_migrates(self):
        old = {"version": "++Fortnite+Release-42.20-CL-58011042", "cl": "58011042"}
        self.assertEqual(self.cmp(old, {"release": "42.30", "cl": "?"}), "update")
        self.assertIsNone(self.cmp(old, {"release": "42.20", "cl": "58011042"}))

    def test_build_number_arriving_later_is_silent(self):
        self.assertEqual(self.cmp({"version": "42.30", "cl": "?"}, {"release": "42.30", "cl": "58557680"}), "silent")

    def test_new_client_build_on_same_release_is_a_hotfix(self):
        self.assertEqual(self.cmp({"version": "42.30", "cl": "58557680"}, {"release": "42.30", "cl": "58600000"}),
                         "hotfix")

    def test_community_api_lag_after_known_build_is_quiet(self):
        self.assertIsNone(self.cmp({"version": "42.30", "cl": "58557680"}, {"release": "42.30", "cl": "?"}))


class FortniteLoopTests(MainLoopTests):
    """The real Fortnite entry through the main loop: one message per release, no repeats."""

    def setUp(self):
        super().setUp()
        self.epic, self.aes = {"version": "42.20"}, AES_4220
        self.patches.append(mock.patch.object(check_update, "GAMES", [REAL_FORTNITE]))
        self.patches.append(mock.patch.object(check_update, "fetch_json",
                                              side_effect=lambda u: fake_fetch(self.epic, self.aes)(u)))
        for p in self.patches[-2:]:
            p.start()

    def test_release_then_late_build_number_sends_one_message(self):
        self.run_once()                       # seeds 42.20
        self.epic = {"version": "42.30"}      # Epic first, community API still on 42.20
        self.run_once()
        state = json.loads(self.run_once())   # quiet repeat
        self.aes = AES_4230                   # community API catches up
        state = json.loads(self.run_once())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("42.30", self.sent[0]["description"])
        self.assertEqual((state["fortnite"]["version"], state["fortnite"]["cl"]), ("42.30", "58557680"))
        self.assertNotIn("kind", state["fortnite"])

    def test_hotfix_sends_a_hotfix_message(self):
        self.epic, self.aes = {"version": "42.30"}, AES_4230
        self.run_once()
        self.aes = {"data": {"build": "++Fortnite+Release-42.30-CL-58600000"}}
        self.run_once()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Hotfix", self.sent[0]["title"])

    # the generic tests of the parent class don't apply to this fixture
    test_first_run_seeds_without_notifying = None
    test_quiet_run_leaves_state_file_unchanged = None
    test_new_version_notifies_and_updates_state = None
    test_failed_send_keeps_old_version_for_retry = None


class DeadlockCheckTests(unittest.TestCase):
    def test_picks_newest_official_post(self):
        news = {"appnews": {"newsitems": [
            {"gid": "10", "title": "Minor Update - 09-16-2026", "date": 100, "tags": ["patchnotes"], "url": "a"},
            {"gid": "11", "title": "City Never Sleeps", "date": 200, "url": "b"},
        ]}}
        with mock.patch.object(check_update, "fetch_json", return_value=news):
            info = check_update.deadlock_check()
        self.assertEqual((info["version"], info["title"], info["patchnotes"]), ("11", "City Never Sleeps", False))
        self.assertIn("feeds=steam_community_announcements", check_update.DEADLOCK_NEWS_URL)

    def test_embed_wording(self):
        patch = check_update.deadlock_embed({"title": "Minor Update - 10-01-2026", "url": "u", "patchnotes": True})
        news = check_update.deadlock_embed({"title": "Matchmaking changes", "url": "u", "patchnotes": False})
        self.assertIn("Update Detected", patch["title"])
        self.assertIn("News", news["title"])


if __name__ == "__main__":
    unittest.main()

"""Correcciones de la revisión: git real contra un repo bare local, reintentos, sembrado por fuente, estado."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collector as c  # noqa: E402
from test_sources import NOW, entry  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def ident(repo):
    git("config", "user.name", "t", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)


class RealGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.bare, self.seed, self.bot = t / "bare.git", t / "seed", t / "bot"
        subprocess.run(["git", "init", "--bare", str(self.bare)], check=True, capture_output=True)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.bare)
        self.seed.mkdir()
        git("init", cwd=self.seed)
        git("checkout", "-b", "main", cwd=self.seed)
        ident(self.seed)
        (self.seed / "docs").mkdir()
        (self.seed / "state.json").write_text('{"v": 0}')
        (self.seed / "docs" / "news.json").write_text("[]")
        (self.seed / "docs" / "index.html").write_text("<html>")
        git("add", "-A", cwd=self.seed)
        git("commit", "-m", "init", cwd=self.seed)
        git("remote", "add", "origin", str(self.bare), cwd=self.seed)
        git("push", "origin", "main", cwd=self.seed)
        subprocess.run(["git", "clone", str(self.bare), str(self.bot)], check=True, capture_output=True)
        ident(self.bot)
        env = {"GITHUB_TOKEN": "ghp_TOK", "GITHUB_REPO": "o/r", "GIT_BRANCH": "main", "PATH": os.environ["PATH"],
               "HOME": os.environ.get("HOME", "")}
        for p in (mock.patch.dict(os.environ, env, clear=True), mock.patch.object(c, "ROOT", self.bot),
                  mock.patch.object(c, "_next_attempt", 0.0), mock.patch.object(c, "_sync_fails", 0)):
            p.start()
            self.addCleanup(p.stop)

    def remote(self, path):
        return git("--git-dir", str(self.bare), "show", f"main:{path}", cwd=self.tmp.name)

    def synced(self):
        git("fetch", "origin", cwd=self.bot)
        return git("rev-list", "--count", "origin/main..HEAD", cwd=self.bot).strip() == "0" and \
            git("rev-list", "--count", "HEAD..origin/main", cwd=self.bot).strip() == "0"

    def test_normal_push(self):
        (self.bot / "state.json").write_text('{"v": 2}')
        self.assertEqual(c.git_sync(force=True), "pushed")
        self.assertEqual(json.loads(self.remote("state.json")), {"v": 2})

    def test_divergent_remote_recovers_and_pushes(self):
        (self.seed / "state.json").write_text('{"v": 1}')
        git("commit", "-am", "otro escritor", cwd=self.seed)
        git("push", "origin", "main", cwd=self.seed)
        (self.bot / "state.json").write_text('{"v": 2}')
        (self.bot / "docs" / "news.json").write_text('[{"id": "x"}]')
        self.assertEqual(c.git_sync(force=True), "pushed")
        self.assertEqual(json.loads(self.remote("state.json")), {"v": 2})
        self.assertEqual(json.loads(self.remote("docs/news.json")), [{"id": "x"}])
        self.assertTrue(self.synced())
        self.assertEqual(c.git_sync(force=True), "clean")

    def test_stranded_local_commit_is_pushed(self):
        (self.bot / "state.json").write_text('{"v": 5}')
        git("commit", "-am", "local", cwd=self.bot)   # commit local sin push, árbol limpio
        self.assertEqual(c.git_sync(force=True), "pushed")
        self.assertEqual(json.loads(self.remote("state.json")), {"v": 5})

    def test_stranded_after_failed_sync_recovers_next_time(self):
        (self.seed / "state.json").write_text('{"v": 1}')
        git("commit", "-am", "otro", cwd=self.seed)
        git("push", "origin", "main", cwd=self.seed)
        (self.bot / "state.json").write_text('{"v": 2}')
        c.git_sync(force=True)
        c.git_sync(force=True)
        self.assertTrue(self.synced())
        self.assertEqual(json.loads(self.remote("state.json")), {"v": 2})

    def test_missing_docs_dir_does_not_break_add(self):
        git("rm", "-r", "-q", "docs", cwd=self.bot)
        git("commit", "-m", "sin docs", cwd=self.bot)
        git("push", "origin", "main", cwd=self.bot)
        (self.bot / "state.json").write_text('{"v": 9}')
        self.assertEqual(c.git_sync(force=True), "pushed")

    def test_failure_backoff_avoids_git_calls(self):
        git("remote", "set-url", "origin", str(self.bot / "no-existe"), cwd=self.bot)
        (self.bot / "state.json").write_text('{"v": 3}')
        with redirect_stderr(io.StringIO()):
            self.assertEqual(c.git_sync(), "error")
        self.assertEqual(c._sync_fails, 1)
        with mock.patch("subprocess.run") as run:
            self.assertEqual(c.git_sync(), "debounced")
            run.assert_not_called()
        first = c._next_attempt
        with redirect_stderr(io.StringIO()):
            c.git_sync(force=True)
        self.assertEqual(c._sync_fails, 2)
        self.assertGreater(c._next_attempt - time.monotonic(), 50)
        self.assertGreater(c._next_attempt, first)

    def test_shutdown_sync_uses_short_timeout(self):
        (self.bot / "state.json").write_text('{"v": 4}')
        real = subprocess.run
        seen = []

        def spy(*a, **kw):
            seen.append(kw.get("timeout"))
            return real(*a, **kw)
        with mock.patch("subprocess.run", side_effect=spy):
            c.git_sync(force=True, timeout=10)
        self.assertTrue(seen and set(seen) == {10})


class SettingsTests(unittest.TestCase):
    def check(self, env):
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(c, "_warned", set()), redirect_stderr(err):
            res = c.git_sync(force=True)
        return res, err.getvalue()

    def test_bad_branch_and_repo_disable(self):
        for env in ({"GITHUB_TOKEN": "SECRETX", "GITHUB_REPO": "o/r", "GIT_BRANCH": "--upload-pack=x"},
                    {"GITHUB_TOKEN": "SECRETX", "GITHUB_REPO": "o/r", "GIT_BRANCH": "a b"},
                    {"GITHUB_TOKEN": "SECRETX", "GITHUB_REPO": "no-slash"},
                    {"GITHUB_TOKEN": "SECRETX", "GITHUB_REPO": "o/r;rm -rf"}):
            res, err = self.check(env)
            self.assertEqual(res, "disabled")
            self.assertNotIn("SECRETX", err)
            self.assertTrue(err)

    def test_valid_settings(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPO": "o/r.x-y", "GIT_BRANCH": "feat/a.b"}, clear=True):
            self.assertEqual(c.git_settings(), ("t", "o/r.x-y", "feat/a.b"))


class SanitizeTests(unittest.TestCase):
    def test_future_post_rejected(self):
        self.assertIsNone(c.bsky_post_to_item(entry(age_min=-30), "leak", now=NOW))
        self.assertIsNotNone(c.bsky_post_to_item(entry(age_min=-2), "leak", now=NOW))

    def test_control_chars_stripped_newlines_kept(self):
        it = c.bsky_post_to_item(entry(text="a\x00b\x1b[31m\nlinea2\x7f"), "leak", now=NOW)
        self.assertNotRegex(it["text"], r"[\x00\x1b\x7f]")
        self.assertIn("\nlinea2", it["text"])

    def test_only_safe_https_images(self):
        for bad in ("http://x/a.jpg", "javascript:alert(1)", 'https://x/a.jpg" onerror="1', "https://x/a b.jpg",
                    "https://x/<a>"):
            self.assertIsNone(c.bsky_first_image({"images": [{"fullsize": bad}]}), bad)
        self.assertEqual(c.bsky_first_image({"images": [{"fullsize": "https://cdn.bsky.app/a.jpg?x=1&y=2"}]}),
                         "https://cdn.bsky.app/a.jpg?x=1&y=2")

    def test_weird_uri_rejected(self):
        e = entry()
        e["post"]["uri"] = 'at://did:plc:x/app.bsky.feed.post/a"onmouseover="1'
        self.assertIsNone(c.bsky_post_to_item(e, "leak", now=NOW))


class EpicFlapTests(unittest.TestCase):
    def test_lastmodified_ignored_in_hash(self):
        self.assertEqual(c.section_hash({"a": 1, "lastModified": "x"}), c.section_hash({"a": 1, "lastModified": "y"}))

    def test_three_consecutive_changes_mutes_section(self):
        data = {"lobby": {"v": 0}}
        _, fp = c.detect_epic(data, None, now=1000)
        emitted = []
        for i in range(1, 6):
            items, fp = c.detect_epic({"lobby": {"v": i}}, fp, now=1000 + i)
            emitted.append(len(items))
        self.assertEqual(emitted, [1, 1, 0, 0, 0])
        items, fp = c.detect_epic({"lobby": {"v": 99}}, fp, now=1000 + 1800 + 10)   # pasado el silencio
        self.assertEqual(len(items), 1)

    def test_stable_poll_resets_counter(self):
        _, fp = c.detect_epic({"lobby": {"v": 0}}, None, now=0)
        _, fp = c.detect_epic({"lobby": {"v": 1}}, fp, now=1)
        _, fp = c.detect_epic({"lobby": {"v": 1}}, fp, now=2)
        out = []
        for i in (2, 3):
            items, fp = c.detect_epic({"lobby": {"v": i}}, fp, now=3 + i)
            out.append(len(items))
        self.assertEqual(out, [1, 1])


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        for p in (mock.patch.object(c, "STATE_FILE", t / "state.json"), mock.patch.object(c, "SITE", t / "docs")):
            p.start()
            self.addCleanup(p.stop)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        with mock.patch("os.replace", wraps=os.replace) as rep:
            c.save_state({"seen": ["a"]})
        rep.assert_called_once()
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["state.json"])

    def test_corrupt_state_is_silent_and_does_not_reprocess_feed(self):
        c.SITE.mkdir()
        (c.SITE / "news.json").write_text(json.dumps([{"id": "news:1"}]))
        c.STATE_FILE.write_text('{"seen": [ trunc')
        with redirect_stderr(io.StringIO()):
            st = c.load_state()
        self.assertIn("news:1", st["seen"])
        self.assertTrue(st["first_run"])   # primera ejecución: nada va a Telegram
        sent = []
        with mock.patch.object(c, "send_telegram", side_effect=lambda i: sent.append(i) or True), \
                mock.patch.object(c, "telegram_config", return_value=("t", "c")), redirect_stderr(io.StringIO()):
            c.process(st, [c.mk("news", "news:2", "t", "x"), c.mk("leak", "leak:1", "t", "y")], quiet=True)
        self.assertEqual(sent, [])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.sent, self.up = [], True
        for p in (mock.patch.object(c, "SITE", t / "docs"), mock.patch.object(c, "STATE_FILE", t / "state.json"),
                  mock.patch.object(c, "telegram_config", return_value=("t", "c")),
                  mock.patch.object(c, "send_telegram", side_effect=self._send)):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, item):
        if self.up:
            self.sent.append(item["id"])
        return self.up

    def st(self, **kw):
        s = {"seen": [], "first_run": False, "seeded": ["news", "leak", "epic", "status_bsky", "statuspage"]}
        s.update(kw)
        return s

    def test_pending_retry_budget_not_burned_by_polls(self):
        self.up = False
        st = self.st()
        item = c.mk("leak", "leak:1", "t", "x")
        with redirect_stderr(io.StringIO()):
            c.process(st, [item], quiet=True)
            for _ in range(8):
                c.process(st, [], quiet=True)
        self.assertEqual(len(st["pending"]), 1)
        self.assertLessEqual(st["pending"][0]["tries"], 1)

    def test_flush_respects_timer_and_recovers(self):
        self.up = False
        st = self.st()
        with redirect_stderr(io.StringIO()):
            c.process(st, [c.mk("leak", "leak:1", "t", "x")], quiet=True)
            c.process(st, [], quiet=True, flush_pending=True)    # demasiado pronto
            self.assertEqual(st["pending"][0]["tries"], 1)
            st["pending"][0]["at"] -= c.PENDING_RETRY_SECS + 1
            self.up = True
            c.process(st, [], quiet=True, flush_pending=True)
        self.assertEqual(self.sent, ["leak:1"])
        self.assertEqual(st["pending"], [])

    def test_discard_after_max_tries(self):
        self.up = False
        st = self.st()
        with redirect_stderr(io.StringIO()):
            c.process(st, [c.mk("leak", "leak:1", "t", "x")], quiet=True)
            for _ in range(5):
                for e in st["pending"]:
                    e["at"] -= c.PENDING_RETRY_SECS + 1
                c.process(st, [], quiet=True, flush_pending=True)
        self.assertEqual(st["pending"], [])

    def test_status_sources_seed_independently(self):
        st = self.st(seeded=["news"])
        b = c.mk("status", "status:bsky:1", "t", "x")
        b["_seed"] = "status_bsky"
        c.process(st, [b], polled_kinds=("status_bsky",), quiet=True)
        self.assertEqual(self.sent, [])
        inc = c.mk("status", "status:incident:1:investigating:t", "t", "x")
        inc["_seed"] = "statuspage"
        c.process(st, [inc], polled_kinds=("statuspage",), quiet=True)
        self.assertEqual(self.sent, [])   # primera vista de statuspage: silencioso
        inc2 = c.mk("status", "status:incident:2:investigating:t", "t", "y")
        inc2["_seed"] = "statuspage"
        c.process(st, [inc2], polled_kinds=("statuspage",), quiet=True)
        self.assertEqual(self.sent, ["status:incident:2:investigating:t"])

    def test_old_seeded_kinds_list_still_seeds_new_sources(self):
        st = {"seen": [], "first_run": False, "seeded": ["build", "cosmetic", "news", "shop"]}
        inc = c.mk("status", "status:incident:1:x:t", "t", "x")
        inc["_seed"] = "statuspage"
        c.process(st, [inc], polled_kinds=("statuspage",), quiet=True)
        self.assertEqual(self.sent, [])

    def test_seen_and_pending_persisted_before_sending(self):
        st = self.st()
        snapshots = []

        def persist():
            snapshots.append((list(st["seen"]), [e["item"]["id"] for e in st["pending"]], list(self.sent)))
        c.process(st, [c.mk("leak", "leak:1", "t", "x")], quiet=True, persist=persist)
        first = snapshots[0]
        self.assertIn("leak:1", first[0])      # visto
        self.assertEqual(first[1], ["leak:1"])  # en cola
        self.assertEqual(first[2], [])          # aún no enviado
        self.assertEqual(st["pending"], [])


if __name__ == "__main__":
    unittest.main()

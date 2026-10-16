"""Pruebas de las fuentes nuevas, el planificador y git_sync (sin red, solo stdlib)."""
import copy
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collector as c  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def entry(rkey="3abc", handle="lpleaksnews.bsky.social", text="New skin leaked", age_min=10,
          embed=None, reason=None):
    e = {
        "post": {
            "uri": f"at://did:plc:xyz/app.bsky.feed.post/{rkey}",
            "author": {"handle": handle},
            "record": {"text": text, "createdAt": iso(NOW - timedelta(minutes=age_min))},
        }
    }
    if embed is not None:
        e["post"]["embed"] = embed
    if reason is not None:
        e["reason"] = reason
    return e


class BskyTests(unittest.TestCase):
    def conv(self, e, kind="leak"):
        return c.bsky_post_to_item(e, kind, now=NOW)

    def test_url_conversion(self):
        self.assertEqual(
            c.bsky_web_url("at://did:plc:xyz/app.bsky.feed.post/3abc", "a.bsky.social"),
            "https://bsky.app/profile/a.bsky.social/post/3abc",
        )

    def test_item_fields(self):
        it = self.conv(entry())
        self.assertEqual(it["kind"], "leak")
        self.assertEqual(it["id"], "leak:at://did:plc:xyz/app.bsky.feed.post/3abc")
        t = it["text"].lower()
        self.assertIn("filtración", t)
        self.assertIn("sin confirmar", t)
        self.assertIn("vía @lpleaksnews.bsky.social", it["text"])
        self.assertIn("https://bsky.app/profile/lpleaksnews.bsky.social/post/3abc", it["text"])
        self.assertIn("New skin leaked", it["text"])
        self.assertNotIn(c.CREATOR_CODE, it["text"])

    def test_repost_skipped(self):
        r = {"$type": "app.bsky.feed.defs#reasonRepost", "by": {"handle": "x"}}
        self.assertIsNone(self.conv(entry(reason=r)))

    def test_old_post_skipped(self):
        self.assertIsNone(self.conv(entry(age_min=60 * 7)))
        self.assertIsNotNone(self.conv(entry(age_min=60 * 5)))

    def test_empty_skipped_but_image_only_ok(self):
        self.assertIsNone(self.conv(entry(text="  ")))
        emb = {"images": [{"fullsize": "https://cdn/full.jpg", "thumb": "https://cdn/t.jpg"}]}
        it = self.conv(entry(text="", embed=emb))
        self.assertEqual(it["image"], "https://cdn/full.jpg")

    def test_image_variants(self):
        self.assertEqual(c.bsky_first_image({"images": [{"thumb": "https://t/x.jpg"}]}), "https://t/x.jpg")
        media = {"media": {"images": [{"fullsize": "https://m/x.jpg"}]}, "record": {}}
        self.assertEqual(c.bsky_first_image(media), "https://m/x.jpg")
        self.assertIsNone(c.bsky_first_image(None))
        self.assertIsNone(c.bsky_first_image({"record": {}}))

    def test_trim_long_text(self):
        it = self.conv(entry(text="x" * 2000))
        self.assertLess(len(it["text"]), 1000)
        self.assertIn("…", it["text"])

    def test_status_kind_template(self):
        it = self.conv(entry(handle="fortnite-status.bsky.social", text="Matchmaking down"), "status")
        self.assertEqual(it["kind"], "status")
        self.assertNotIn("sin confirmar", it["text"].lower())
        self.assertIn("Matchmaking down", it["text"])

    def test_fetch_author_skips_failures_and_dedupes(self):
        feed = {"feed": [entry("a"), entry("b", reason={"$type": "x#reasonRepost"})]}
        with mock.patch.object(c, "http_json", return_value=feed) as m:
            items = c.fetch_bsky_items("lpleaksnews.bsky.social", "leak", now=NOW)
        self.assertEqual(len(items), 1)
        self.assertIn("api.bsky.app", m.call_args[0][0])
        self.assertIn("actor=lpleaksnews.bsky.social", m.call_args[0][0])

    def test_fetch_leaks_one_handle_failing(self):
        def fake(url, **kw):
            if "blackgoku" in url:
                raise OSError("boom")
            return {"feed": [entry("k", handle="x")]}
        real_fetch = c.fetch_bsky_items
        with mock.patch.object(c, "http_json", side_effect=fake), \
                mock.patch.object(c, "fetch_bsky_items", lambda h, k: real_fetch(h, k, now=NOW)), \
                mock.patch.object(c, "LEAKER_HANDLES", ["a.bsky.social", "blackgokunews.bsky.social"]), \
                redirect_stderr(io.StringIO()):
            items = c.src_leaks({})
        self.assertEqual(len(items), 1)

    def test_fetch_leaks_all_failing_raises(self):
        with mock.patch.object(c, "http_json", side_effect=OSError("x")), \
                mock.patch.object(c, "LEAKER_HANDLES", ["a.bsky.social"]), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                c.src_leaks({})


class EpicTests(unittest.TestCase):
    BASE = {
        "lastModified": "2026-10-15T10:00:00Z",
        "emergencynoticev2": {"emergencynotices": {"emergencynotices": [{"title": "Aviso", "body": "Cuerpo"}]}},
        "lobby": {"backgroundimage": "a"},
        "loginmessage": {"message": {"title": "hola"}},
    }

    def test_seed_emits_nothing(self):
        items, fp = c.detect_epic(self.BASE, None)
        self.assertEqual(items, [])
        self.assertEqual(set(fp["sections"]), {"emergencynoticev2", "lobby", "loginmessage"})
        self.assertEqual(fp["lastModified"], "2026-10-15T10:00:00Z")

    def test_unchanged_nothing(self):
        _, fp = c.detect_epic(self.BASE, None)
        items, fp2 = c.detect_epic(copy.deepcopy(self.BASE), fp)
        self.assertEqual(items, [])
        self.assertEqual(fp["sections"], fp2["sections"])

    def test_change_emits_one_item_per_section(self):
        _, fp = c.detect_epic(self.BASE, None)
        new = copy.deepcopy(self.BASE)
        new["lobby"]["backgroundimage"] = "b"
        items, fp2 = c.detect_epic(new, fp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "epic")
        self.assertIn("lobby", items[0]["id"])
        self.assertNotEqual(fp["sections"]["lobby"], fp2["sections"]["lobby"])

    def test_emergency_notice_text(self):
        _, fp = c.detect_epic(self.BASE, None)
        new = copy.deepcopy(self.BASE)
        new["emergencynoticev2"]["emergencynotices"]["emergencynotices"] = [
            {"title": "Mantenimiento", "body": "Servidores caen a las 4"}]
        items, _ = c.detect_epic(new, fp)
        self.assertIn("Mantenimiento", items[0]["text"])
        self.assertIn("Servidores caen a las 4", items[0]["text"])
        self.assertNotIn(c.CREATOR_CODE, items[0]["text"])

    def test_missing_keys_guarded_and_hash_kept(self):
        _, fp = c.detect_epic(self.BASE, None)
        items, fp2 = c.detect_epic({"lastModified": "z"}, fp)
        self.assertEqual(items, [])
        self.assertEqual(fp2["sections"], fp["sections"])

    def test_key_order_irrelevant(self):
        self.assertEqual(c.section_hash({"a": 1, "b": 2}), c.section_hash({"b": 2, "a": 1}))

    def test_old_state_without_epic_fp(self):
        items, fp = c.detect_epic(self.BASE, {})
        self.assertEqual(items, [])
        self.assertTrue(fp["sections"])


def fn_incident(iid="i1", status="investigating", updated="2026-10-15T11:00:00Z", name="Fortnite login issues",
                comps=None):
    return {"id": iid, "name": name, "status": status, "updated_at": updated,
            "components": comps or [],
            "incident_updates": [{"body": "Estamos investigando", "status": status}]}


class StatusTests(unittest.TestCase):
    def summary(self, incidents=(), comps=()):
        return {"incidents": list(incidents), "components": list(comps)}

    def test_fortnite_incident_by_name_and_other_ignored(self):
        s = self.summary([fn_incident(), fn_incident("i2", name="Rocket League down")])
        items, _ = c.detect_statuspage(s, {"scheduled_maintenances": []}, {})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "status")
        self.assertIn("Fortnite login issues", items[0]["text"])

    def test_incident_by_component(self):
        inc = fn_incident(name="Servidores", comps=[{"id": "wf1ys2kx4pxc", "name": "Fortnite"}])
        items, _ = c.detect_statuspage(self.summary([inc]), {"scheduled_maintenances": []}, {})
        self.assertEqual(len(items), 1)

    def test_dedupe_key_changes_with_status_and_updated(self):
        a = c.detect_statuspage(self.summary([fn_incident()]), {}, {})[0][0]["id"]
        a2 = c.detect_statuspage(self.summary([fn_incident()]), {}, {})[0][0]["id"]
        b = c.detect_statuspage(self.summary([fn_incident(status="identified")]), {}, {})[0][0]["id"]
        d = c.detect_statuspage(self.summary([fn_incident(updated="2026-10-15T11:30:00Z")]), {}, {})[0][0]["id"]
        self.assertEqual(a, a2)
        self.assertEqual(len({a, b, d}), 3)

    def test_component_transition(self):
        def comp(st):
            return {"id": "wf1ys2kx4pxc", "name": "Fortnite", "status": st, "updated_at": "t1"}
        _, prev = c.detect_statuspage(self.summary(comps=[comp("operational")]), {}, {})
        self.assertEqual(prev, {"wf1ys2kx4pxc": "operational"})  # primera vista: solo siembra
        items, prev2 = c.detect_statuspage(self.summary(comps=[comp("partial_outage")]), {}, prev)
        self.assertEqual(len(items), 1)
        self.assertEqual(prev2["wf1ys2kx4pxc"], "partial_outage")
        items, _ = c.detect_statuspage(self.summary(comps=[comp("partial_outage")]), {}, prev2)
        self.assertEqual(items, [])  # ya no era operational

    def test_first_sight_non_operational_seeds_only(self):
        comp = {"id": "wf1ys2kx4pxc", "name": "Fortnite", "status": "major_outage", "updated_at": "t"}
        items, prev = c.detect_statuspage(self.summary(comps=[comp]), {}, {})
        self.assertEqual(items, [])
        self.assertEqual(prev["wf1ys2kx4pxc"], "major_outage")

    def test_non_fortnite_component_ignored(self):
        comp = {"id": "zzz", "name": "Rocket League", "status": "operational", "updated_at": "t"}
        _, prev = c.detect_statuspage(self.summary(comps=[comp]), {}, {})
        self.assertEqual(prev, {})

    def test_maintenance(self):
        m = {"id": "m1", "name": "Fortnite mantenimiento", "status": "scheduled", "updated_at": "u1",
             "scheduled_for": "2026-10-16T04:00:00Z", "scheduled_until": "2026-10-16T05:00:00Z",
             "components": [], "incident_updates": [{"body": "Mantenimiento programado"}]}
        other = dict(m, id="m2", name="Epic Games Store")
        items, _ = c.detect_statuspage(self.summary(), {"scheduled_maintenances": [m, other]}, {})
        self.assertEqual(len(items), 1)
        self.assertIn("2026-10-16T04:00:00Z", items[0]["text"])
        self.assertIn("m1", items[0]["id"])


class SchedulerTests(unittest.TestCase):
    def test_due_ordering(self):
        s = c.Scheduler({"a": 10, "b": 20, "c": 30}, now=0)
        s.next_due = {"a": 5, "b": 1, "c": 100}
        self.assertEqual(s.due(10), ["b", "a"])

    def test_done_ok_resets_and_reschedules(self):
        s = c.Scheduler({"a": 45}, now=0)
        s.fails["a"] = 3
        s.done("a", 100, True)
        self.assertEqual(s.fails["a"], 0)
        self.assertEqual(s.next_due["a"], 145)

    def test_backoff_exponential_capped(self):
        self.assertEqual(c.backoff_delay(45, 0), 45)
        self.assertEqual(c.backoff_delay(45, 1), 90)
        self.assertEqual(c.backoff_delay(45, 2), 180)
        self.assertEqual(c.backoff_delay(45, 3), 300)
        self.assertEqual(c.backoff_delay(45, 20), 300)
        self.assertEqual(c.backoff_delay(400, 5), 400)  # nunca menor que el intervalo normal

    def test_failure_increments(self):
        s = c.Scheduler({"a": 45}, now=0)
        s.done("a", 0, False)
        s.done("a", 0, False)
        self.assertEqual(s.fails["a"], 2)
        self.assertEqual(s.next_due["a"], 180)

    def test_wait_min_one_second(self):
        s = c.Scheduler({"a": 45}, now=0)
        s.next_due["a"] = 10.2
        self.assertEqual(s.wait(10), 1.0)
        self.assertAlmostEqual(s.wait(0), 10.2)


class GitTests(unittest.TestCase):
    def test_disabled_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("subprocess.run") as run:
            self.assertEqual(c.git_sync(), "disabled")
            run.assert_not_called()

    def test_disabled_with_only_token(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=True), mock.patch("subprocess.run") as run:
            self.assertEqual(c.git_sync(), "disabled")
            run.assert_not_called()

    def test_scrub_removes_token_and_b64(self):
        tok = "ghp_SECRET123"
        msg = f"fatal: https://x-access-token:{tok}@github.com failed, {c.git_auth_b64(tok)}"
        out = c.scrub(msg, tok)
        self.assertNotIn(tok, out)
        self.assertNotIn(c.git_auth_b64(tok), out)

    def test_git_error_does_not_leak_token(self):
        tok = "ghp_SECRET123"
        bad = subprocess.CompletedProcess([], 1, "", f"fatal: auth failed for {tok}")
        with mock.patch("subprocess.run", return_value=bad):
            with self.assertRaises(RuntimeError) as cm:
                c.git_run(["status"], Path("."), tok)
        self.assertNotIn(tok, str(cm.exception))

    def test_git_sync_error_path_never_prints_token(self):
        tok = "ghp_SECRET123"
        env = {"GITHUB_TOKEN": tok, "GITHUB_REPO": "o/r"}
        bad = subprocess.CompletedProcess([], 1, "", f"boom {tok}")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(c, "ROOT", Path(d)), \
                    mock.patch.object(c, "_next_attempt", 0.0), mock.patch("subprocess.run", return_value=bad), \
                    redirect_stderr(err):
                res = c.git_sync(force=True)
        self.assertEqual(res, "error")
        self.assertNotIn(tok, err.getvalue())

    def test_token_not_in_args(self):
        tok = "ghp_SECRET123"
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=ok) as run:
            c.git_run(["status"], Path("."), tok)
        args = run.call_args[0][0]
        self.assertNotIn(tok, " ".join(args))
        self.assertIsInstance(args, list)
        self.assertFalse(run.call_args[1].get("shell", False))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(c, "SITE", Path(self.tmp.name) / "docs")
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p2 = mock.patch.object(c, "send_telegram", side_effect=lambda it: self.sent.append(it["id"]) or True)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(c, "telegram_config", return_value=("t", "chat"))
        p3.start()
        self.addCleanup(p3.stop)

    def state(self, **kw):
        s = {"seen": [], "first_run": False, "seeded": ["news", "build", "shop", "cosmetic"]}
        s.update(kw)
        return s

    def leak(self, n):
        return c.mk("leak", f"leak:{n}", "t", f"texto {n}", None, "Filtración")

    def test_new_kinds_seed_silently_on_old_state(self):
        st = self.state()
        c.process(st, [self.leak(1)], polled_kinds=("leak",), quiet=True)
        self.assertEqual(self.sent, [])
        self.assertIn("leak", st["seeded"])
        c.process(st, [self.leak(2)], polled_kinds=("leak",), quiet=True)
        self.assertEqual(self.sent, ["leak:2"])

    def test_polled_kind_seeds_even_without_items(self):
        st = self.state()
        c.process(st, [], polled_kinds=("epic",), quiet=True)
        self.assertIn("epic", st["seeded"])
        epic = c.mk("epic", "epic:lobby:abc", "t", "x", None, "Epic")
        c.process(st, [epic], polled_kinds=("epic",), quiet=True)
        self.assertEqual(self.sent, ["epic:lobby:abc"])

    def test_priority_when_over_cap(self):
        st = self.state(seeded=["news", "leak"])
        news = c.mk("news", "news:1", "t", "n", None, "Noticia")
        with mock.patch.object(c, "MAX_TELEGRAM_PER_RUN", 1), redirect_stderr(io.StringIO()):
            c.process(st, [news, self.leak(1)], quiet=True)
        self.assertEqual(self.sent, ["leak:1"])

    def test_dedupe_seen(self):
        st = self.state(seeded=["leak"])
        c.process(st, [self.leak(1)], quiet=True)
        c.process(st, [self.leak(1)], quiet=True)
        self.assertEqual(self.sent, ["leak:1"])

    def test_old_state_json_without_new_keys_loads(self):
        with mock.patch.object(c, "STATE_FILE", Path(self.tmp.name) / "state.json"):
            c.STATE_FILE.write_text('{"seen": ["a"], "first_run": false}')
            st = c.load_state()
        self.assertEqual(st["seen"], ["a"])
        self.assertEqual(st.get("epic_fp") or {}, {})


if __name__ == "__main__":
    unittest.main()

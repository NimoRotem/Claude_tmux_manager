from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

import browser_fleet as fleet


GCLOUD_WRAPPER = ("python3 /usr/lib/google-cloud-sdk/lib/gcloud.py compute ssh "
                  "nimrod_rotem@instance-3 --zone us-central1-b -- -N -L 9401:127.0.0.1:9401")
SSH_FORWARD = ("/usr/bin/ssh -t -i /home/x/.ssh/google_compute_engine -o CheckHostIP=no "
               "sa_1145@34.69.0.72 -N -L 9401:127.0.0.1:9401")
SSH_FORWARD_2 = "/usr/bin/ssh -t sa_1145@34.69.0.72 -N -L 9404:127.0.0.1:9401"
SSH_SHELL = "/usr/bin/ssh nimrod_rotem@builder"


class ParseForwardsTests(unittest.TestCase):
    def test_reads_local_and_remote_ports(self):
        [fwd] = fleet.parse_forwards([(101, SSH_FORWARD)])
        self.assertEqual((fwd.pid, fwd.local_port, fwd.remote_host, fwd.remote_port),
                         (101, 9401, "127.0.0.1", 9401))

    def test_ignores_the_gcloud_wrapper_that_spawned_the_ssh(self):
        """Both carry the -L. Counting the parent reports one forward as two, and
        offers a pid whose death leaves the real forward running."""
        self.assertEqual(fleet.parse_forwards([(100, GCLOUD_WRAPPER)]), [])

    def test_ignores_an_ssh_with_no_forward(self):
        self.assertEqual(fleet.parse_forwards([(102, SSH_SHELL)]), [])


class ClassifyTests(unittest.TestCase):
    def test_resident_profiles_are_never_disposable(self):
        for profile in (str(fleet.CB_ROOT / "profile"),
                        str(fleet.CB_ROOT / "sessions" / "s1" / "profile"),
                        str(fleet.HOME / ".ramp-browser" / "profile")):
            self.assertEqual(fleet.classify_profile(profile), "resident", profile)

    def test_throwaway_profiles(self):
        for profile in ("/tmp/patent-browser-tmagent-f0612a74", "/tmp/pcdemo-obs1290",
                        "/tmp/claude-1000/-home-x/abcd1234-5678/scratchpad/prof"):
            self.assertEqual(fleet.classify_profile(profile), "disposable", profile)

    def test_an_unrecognised_profile_is_unknown_not_disposable(self):
        self.assertEqual(fleet.classify_profile("/home/nimrod_rotem/EBAY_GRABO/profile"),
                         "unknown")

    def test_owner_names_the_claude_session_and_project(self):
        owner = fleet.owner_of("/tmp/claude-1000/-home-nimo-builder4-home-tmux-dashboard/"
                               "52cfa0c2-57f4-4e0b/scratchpad/p")
        self.assertIn("tmux-dashboard", owner)
        self.assertIn("52cfa0c2", owner)


def _browser(**kw) -> fleet.Browser:
    base = dict(pid=1, profile="/tmp/patent-browser-x-1", cdp_port=9401, headless=True,
                kind="disposable", up=True, profile_mtime=time.time())
    base.update(kw)
    return fleet.Browser(**base)


def _forward(**kw) -> fleet.Forward:
    base = dict(pid=2, local_port=9401, remote_host="127.0.0.1", remote_port=9401, up=True)
    base.update(kw)
    return fleet.Forward(**base)


class ProblemTests(unittest.TestCase):
    def test_a_forward_answering_nothing_is_reported_dead(self):
        problems = fleet.find_problems([], [_forward(up=False)])
        self.assertEqual([p["kind"] for p in problems], ["forward_dead"])

    def test_two_forwards_onto_one_remote_port_are_a_duplicate(self):
        a, b = _forward(pid=2, local_port=9401), _forward(pid=3, local_port=9404)
        kinds = [p["kind"] for p in fleet.find_problems([], [a, b])]
        self.assertIn("forward_duplicate", kinds)
        self.assertIn("duplicate", a.problems)

    def test_a_claimed_live_forward_is_not_a_problem(self):
        problems = fleet.find_problems([], [_forward(claimed_by="trademark console")])
        self.assertEqual(problems, [])

    def test_a_live_forward_nobody_claims_is_flagged_but_not_dead(self):
        kinds = [p["kind"] for p in fleet.find_problems([], [_forward()])]
        self.assertEqual(kinds, ["forward_unclaimed"])

    def test_a_throwaway_browser_idle_past_the_threshold_is_stale(self):
        old = _browser(profile_mtime=time.time() - 9 * 3600)
        kinds = [p["kind"] for p in fleet.find_problems([old], [], stale_h=6)]
        self.assertEqual(kinds, ["browser_stale"])

    def test_a_resident_browser_is_never_stale_however_old(self):
        resident = _browser(kind="resident", profile=str(fleet.CB_ROOT / "profile"),
                            profile_mtime=time.time() - 400 * 3600)
        self.assertEqual(fleet.find_problems([resident], []), [])

    def test_a_claimed_throwaway_browser_is_not_stale(self):
        held = _browser(profile_mtime=time.time() - 9 * 3600, claimed_by="console")
        self.assertEqual(fleet.find_problems([held], [], stale_h=6), [])


class ReapTests(unittest.TestCase):
    def _inv(self, browsers, forwards, stale_h=6.0):
        problems = fleet.find_problems(browsers, forwards, stale_h=stale_h)
        return {"browsers": [b.as_dict() for b in browsers],
                "forwards": [f.as_dict() for f in forwards], "problems": problems}

    def test_plan_kills_a_dead_forward(self):
        inv = self._inv([], [_forward(up=False, pid=77)])
        self.assertEqual([(a["what"], a["pid"]) for a in fleet.reap_plan(inv)],
                         [("forward", 77)])

    def test_plan_spares_a_claimed_forward_even_when_dead(self):
        """A console that still names the port is mid-reconnect. Its own start()
        clears the record; a reaper doing it underneath is a race, not a tidy-up."""
        inv = self._inv([], [_forward(up=False, claimed_by="trademark console")])
        self.assertEqual(fleet.reap_plan(inv), [])

    def test_plan_never_touches_a_resident_browser(self):
        resident = _browser(kind="resident", profile=str(fleet.CB_ROOT / "profile"),
                            profile_mtime=0)
        self.assertEqual(fleet.reap_plan(self._inv([resident], [])), [])

    def test_plan_kills_a_stale_unclaimed_throwaway(self):
        stale = _browser(pid=88, profile_mtime=time.time() - 20 * 3600)
        self.assertEqual([(a["what"], a["pid"]) for a in fleet.reap_plan(self._inv([stale], []))],
                         [("browser", 88)])

    def test_dry_run_kills_nothing(self):
        inv = self._inv([], [_forward(up=False, pid=77)])
        with mock.patch.object(fleet, "_kill", side_effect=AssertionError("killed on a dry run")):
            res = fleet.reap(inv, dry_run=True)
        self.assertEqual((res["planned"], res["killed"]), (1, 0))

    def test_a_recycled_pid_is_not_signalled(self):
        """The pid was re-used between the inventory and the kill, so the process now
        wearing it is somebody else's. Start ticks are the only way to tell."""
        inv = self._inv([], [_forward(up=False, pid=77)])
        inv["forwards"][0]["started"] = 12345.0
        with mock.patch.object(fleet, "guard_started", return_value=99999.0), \
                mock.patch("os.kill", side_effect=AssertionError("signalled a recycled pid")):
            res = fleet.reap(inv, dry_run=False)
        self.assertEqual(res["killed"], 0)

    def test_it_signals_when_the_process_is_still_the_same_one(self):
        inv = self._inv([], [_forward(up=False, pid=77)])
        inv["forwards"][0]["started"] = 12345.0
        with mock.patch.object(fleet, "guard_started", return_value=12345.0), \
                mock.patch("os.kill") as killed:
            res = fleet.reap(inv, dry_run=False)
        self.assertEqual(res["killed"], 1)
        killed.assert_called_once()


class CookieMatchTests(unittest.TestCase):
    JAR = [{"name": "a", "value": "1", "domain": ".uspto.gov"},
           {"name": "b", "value": "2", "domain": "patentcenter.uspto.gov"},
           {"name": "c", "value": "3", "domain": ".google.com"},
           {"name": "d", "value": "4", "domain": "usptoo.gov"}]

    def test_a_domain_cookie_matches_a_subdomain(self):
        got = {c["name"] for c in fleet.cookies_for(self.JAR, "patentcenter.uspto.gov")}
        self.assertEqual(got, {"a", "b"})

    def test_a_host_cookie_does_not_leak_to_the_parent(self):
        got = {c["name"] for c in fleet.cookies_for(self.JAR, "uspto.gov")}
        self.assertEqual(got, {"a"})

    def test_a_lookalike_domain_does_not_match(self):
        """usptoo.gov is not uspto.gov, and a suffix test without the dot says it is."""
        self.assertEqual(fleet.cookies_for(self.JAR, "uspto.gov"),
                         [{"name": "a", "value": "1", "domain": ".uspto.gov"}])


class ProbeTests(unittest.TestCase):
    PROBE = {"name": "uspto", "url": "https://patentcenter.uspto.gov/x",
             "signed_in": ["Sign out"], "signed_out": ["Sign in to your account"],
             "signed_out_url": ["/login"]}

    def _with(self, cookies, fetch):
        return (mock.patch.object(fleet, "cdp_get",
                                  return_value={"webSocketDebuggerUrl":
                                                "ws://x/devtools/browser/i"}),
                mock.patch.object(fleet, "browser_cookies", return_value=cookies),
                mock.patch.object(fleet, "_fetch_as_browser", **fetch))

    def test_an_unreachable_browser_is_unknown_not_signed_out(self):
        """A guard that reads 'could not ask' as 'logged out' cries wolf, and the
        re-auth it triggers is what gets an account locked."""
        with mock.patch.object(fleet, "cdp_get", side_effect=OSError("refused")):
            self.assertEqual(fleet.probe_login(9999, self.PROBE)["state"], "unknown")

    def test_a_jar_that_cannot_be_read_is_unknown(self):
        with mock.patch.object(fleet, "cdp_get",
                               return_value={"webSocketDebuggerUrl": "ws://x/devtools/browser/i"}), \
                mock.patch.object(fleet, "browser_cookies", side_effect=TimeoutError("wedged")):
            self.assertEqual(fleet.probe_login(9401, self.PROBE)["state"], "unknown")

    def test_no_cookies_for_the_host_is_unknown_not_signed_out(self):
        """The session may live on another domain. Reporting it as logged out would
        start a re-auth nobody asked for."""
        a, b, c = self._with([{"name": "x", "value": "1", "domain": ".example.com"}],
                             {"return_value": ("u", "t")})
        with a, b, c:
            res = fleet.probe_login(9401, self.PROBE)
        self.assertEqual(res["state"], "unknown")
        self.assertIn("no cookies", res["detail"])

    def test_a_failed_fetch_is_unknown(self):
        a, b, c = self._with([{"name": "s", "value": "1", "domain": ".uspto.gov"}],
                             {"side_effect": OSError("timed out")})
        with a, b, c:
            self.assertEqual(fleet.probe_login(9401, self.PROBE)["state"], "unknown")

    def test_markers_decide_the_verdict(self):
        cookies = [{"name": "s", "value": "1", "domain": ".uspto.gov"}]
        for body, expected in ((" please Sign out here", "ok"),
                               ("Sign in to your account", "signed_out"),
                               ("an unrelated page", "unknown")):
            a, b, c = self._with(cookies, {"return_value": ("https://x/y", body)})
            with a, b, c:
                self.assertEqual(fleet.probe_login(9401, self.PROBE)["state"], expected, body)

    def test_a_redirect_to_the_login_url_is_signed_out_whatever_the_body_says(self):
        a, b, c = self._with([{"name": "s", "value": "1", "domain": ".uspto.gov"}],
                             {"return_value": ("https://id.uspto.gov/login", "Sign out")})
        with a, b, c:
            self.assertEqual(fleet.probe_login(9401, self.PROBE)["state"], "signed_out")


class TextTests(unittest.TestCase):
    def test_script_bodies_are_dropped(self):
        """Half the world's pages carry 'sign in' inside an analytics payload."""
        text = fleet._text_of("<html><script>var x='Sign in';</script><p>Welcome back</p>")
        self.assertNotIn("Sign in", text)
        self.assertIn("Welcome back", text)


class ClaimTests(unittest.TestCase):
    def test_claims_read_the_console_state_files(self):
        with mock.patch.object(fleet, "HOME", Path("/nonexistent-home")):
            self.assertEqual(fleet.read_claims(), {})

    def test_a_console_state_file_claims_its_port(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / ".trademark-filing" / "agent"
            state.mkdir(parents=True)
            (state / "state.json").write_text(json.dumps(
                {"browser": {"port": 9401, "profile": "/tmp/patent-browser-tmagent-1"}}))
            with mock.patch.object(fleet, "HOME", home), \
                    mock.patch.object(fleet, "CB_ROOT", home / ".claude-browser"):
                claims = fleet.read_claims()
        self.assertIn(9401, claims)
        self.assertIn("trademark", claims[9401])


if __name__ == "__main__":
    unittest.main()


class PortCollisionTests(unittest.TestCase):
    def test_two_profiles_on_one_port_collide(self):
        rows = [{"pid": 1, "cdp_port": 9222, "profile": "/h/.claude-browser/profile"},
                {"pid": 2, "cdp_port": 9222, "profile": "/h/.claude-browser/sessions/default/profile"}]
        [problem] = fleet.port_collisions(rows)
        self.assertEqual(problem["kind"], "port_collision")
        self.assertEqual(sorted(problem["pids"]), [1, 2])

    def test_one_profile_seen_twice_is_not_a_collision(self):
        """The launcher and Chrome itself share a command line. That is one browser."""
        rows = [{"pid": 1, "cdp_port": 9222, "profile": "/h/.claude-browser/profile"},
                {"pid": 2, "cdp_port": 9222, "profile": "/h/.claude-browser/profile"}]
        self.assertEqual(fleet.port_collisions(rows), [])


class RemoteParseTests(unittest.TestCase):
    LINES = [
        "568105 dbus-run-session -- google-chrome-stable --user-data-dir=/h/EBAY/profile "
        "--remote-debugging-port=9226",
        "568109 /opt/google/chrome/chrome --user-data-dir=/h/EBAY/profile "
        "--remote-debugging-port=9226",
        "623545 /usr/bin/google-chrome --headless=new --user-data-dir=/tmp/patent-browser-tm-1 "
        "--remote-debugging-port=9401",
    ]

    def test_one_row_per_browser_not_one_per_process(self):
        rows = fleet.parse_remote_browsers(self.LINES)
        self.assertEqual([r["pid"] for r in rows], [568109, 623545])

    def test_it_keeps_the_real_chrome_binary_over_the_wrapper(self):
        rows = fleet.parse_remote_browsers(self.LINES)
        self.assertEqual(rows[0]["pid"], 568109)

    def test_a_remote_home_is_classified_the_same_as_a_local_one(self):
        rows = fleet.parse_remote_browsers([
            "1 /opt/google/chrome/chrome --user-data-dir=/home/someone/.claude-browser/profile"
            " --remote-debugging-port=9222"])
        self.assertEqual(rows[0]["kind"], "resident")
        self.assertEqual(rows[0]["owner"], "the resident dashboard browser")

    def test_the_separator_is_not_a_bash_comment(self):
        """`echo #x` prints nothing, so a # sentinel merges every block into one."""
        self.assertFalse(fleet.REMOTE_SEP.startswith("#"))
        self.assertIn(fleet.REMOTE_SEP, fleet.REMOTE_SNIPPET)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(fleet, "LEASES_FILE",
                                    Path(self.tmp.name) / "leases.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        cap = mock.patch.object(fleet, "afford_browser", return_value=(True, ""))
        cap.start()
        self.addCleanup(cap.stop)

    def test_one_holder_at_a_time(self):
        self.assertTrue(fleet.acquire("uspto-nimo", "session-a")["ok"])
        second = fleet.acquire("uspto-nimo", "session-b")
        self.assertFalse(second["ok"])
        self.assertEqual(second["held_by"], "session-a")

    def test_the_holder_can_take_it_again(self):
        """Re-entrancy matters: a job that renews must not lock itself out."""
        fleet.acquire("uspto-nimo", "session-a")
        self.assertTrue(fleet.acquire("uspto-nimo", "session-a")["ok"])

    def test_an_expired_lease_is_free(self):
        """A session dies without releasing. An identity nobody can ever take again
        is worse than one taken twice."""
        fleet.acquire("uspto-nimo", "session-a", ttl_s=10, now=1000.0)
        self.assertTrue(fleet.acquire("uspto-nimo", "session-b", now=1011.0)["ok"])

    def test_a_live_lease_is_not_free_a_second_before_it_expires(self):
        fleet.acquire("uspto-nimo", "session-a", ttl_s=10, now=1000.0)
        self.assertFalse(fleet.acquire("uspto-nimo", "session-b", now=1009.0)["ok"])

    def test_only_the_holder_releases(self):
        fleet.acquire("uspto-nimo", "session-a")
        self.assertFalse(fleet.release("uspto-nimo", "session-b")["ok"])
        self.assertTrue(fleet.release("uspto-nimo", "session-a")["ok"])
        self.assertTrue(fleet.acquire("uspto-nimo", "session-b")["ok"])

    def test_leases_lists_only_the_live_ones(self):
        fleet.acquire("a", "s", ttl_s=10, now=1000.0)
        fleet.acquire("b", "s", ttl_s=10000, now=time.time())
        self.assertEqual(sorted(fleet.leases()), ["b"])

    def test_a_lease_needs_both_names(self):
        self.assertFalse(fleet.acquire("", "s")["ok"])
        self.assertFalse(fleet.acquire("a", "")["ok"])


class AdmissionTests(unittest.TestCase):
    def test_a_box_with_no_memory_refuses_a_browser(self):
        with mock.patch.object(fleet, "_mem", return_value=(400.0, 3.0)):
            ok, why = fleet.afford_browser(running=0)
        self.assertFalse(ok)
        self.assertIn("400 MB", why)

    def test_the_ceiling_refuses_the_next_one(self):
        with mock.patch.object(fleet, "_mem", return_value=(20000.0, 60.0)):
            ok, why = fleet.afford_browser(running=fleet.MAX_BROWSERS)
        self.assertFalse(ok)
        self.assertIn("ceiling", why)

    def test_a_healthy_box_admits(self):
        with mock.patch.object(fleet, "_mem", return_value=(20000.0, 60.0)):
            self.assertTrue(fleet.afford_browser(running=1)[0])

    def test_capacity_refusal_blocks_a_new_lease_but_not_a_renewal(self):
        """Refusing to renew would drop an identity somebody is mid-form with."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fleet, "LEASES_FILE", Path(tmp) / "l.json"):
                with mock.patch.object(fleet, "afford_browser", return_value=(True, "")):
                    fleet.acquire("held", "s")
                with mock.patch.object(fleet, "afford_browser",
                                       return_value=(False, "no memory")):
                    self.assertFalse(fleet.acquire("fresh", "s")["ok"])
                    self.assertTrue(fleet.renew("held", "s")["ok"])


class EgressTests(unittest.TestCase):
    def test_the_exit_is_pinned_to_the_identity(self):
        """Rotating the exit IP under a signed-in session is what triggers the
        re-auth. Same identity, same session id, every launch."""
        self.assertEqual(fleet.egress_id("uspto-nimo"), fleet.egress_id("uspto-nimo"))

    def test_different_identities_get_different_exits(self):
        self.assertNotEqual(fleet.egress_id("uspto-nimo"), fleet.egress_id("google-nimo"))


class WallTests(unittest.TestCase):
    def test_the_page_names_owners_and_problems(self):
        box = {"host": "builder4", "browsers": [
                   {"pid": 1, "cdp_port": 9222, "kind": "resident",
                    "owner": "the resident dashboard browser", "browser_id": "abcdef1234",
                    "tabs": [{"url": "https://uspto.gov/x", "title": "t"}]}],
               "forwards": [{"pid": 2, "local_port": 9401, "remote_host": "127.0.0.1",
                             "remote_port": 9401, "up": True, "claimed_by": ""}],
               "problems": [{"kind": "port_collision", "detail": "two browsers on 9222"}]}
        html = fleet.wall_html([box])
        self.assertIn("the resident dashboard browser", html)
        self.assertIn("port_collision", html)
        self.assertIn("nobody", html)          # the unclaimed forward
        self.assertIn("abcdef12", html)

    def test_markup_in_a_tab_title_cannot_break_out(self):
        box = {"host": "b", "browsers": [{"pid": 1, "cdp_port": 1, "kind": "x",
               "owner": "<script>alert(1)</script>", "tabs": []}], "forwards": []}
        self.assertNotIn("<script>alert", fleet.wall_html([box]))


class MemoryPressureTests(unittest.TestCase):
    def test_a_box_with_room_says_nothing(self):
        self.assertEqual(fleet.memory_pressure({"available": 24000, "total": 32000}), "")

    def test_a_tight_box_is_called_out_before_the_shed_threshold(self):
        """The shed rule fires at 6%. A warning that first appears at 6% is one
        nobody can act on, because the box is already swapping."""
        said = fleet.memory_pressure({"available": 1800, "total": 16000})
        self.assertIn("11.2%", said)
        self.assertIn("livelock", said)

    def test_a_box_that_did_not_answer_is_not_reported_as_starving(self):
        self.assertEqual(fleet.memory_pressure({}), "")
        self.assertEqual(fleet.memory_pressure({"available": 0, "total": 0}), "")
        self.assertEqual(fleet.memory_pressure({"available": "?", "total": "?"}), "")

    def test_the_wall_shows_the_warning(self):
        html = fleet.wall_html([{"host": "instance-3", "browsers": [], "forwards": [],
                                 "memory_mb": {"available": 1800, "total": 16000}}])
        self.assertIn("close a browser here", html)


class LiveAppClaimTests(unittest.TestCase):
    def test_the_live_filing_app_registry_is_read_too(self):
        """The app moved out of the dashboard repo. Reading only the old path
        reported every browser it owns as unclaimed, and unclaimed is the verdict
        the reaper acts on."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".patent-filing").mkdir(parents=True)
            (home / ".patent-filing" / "browsers.json").write_text(json.dumps(
                {"9402": {"port": 9402, "profile": "/tmp/patent-browser-warm-console-ec6"}}))
            with mock.patch.object(fleet, "HOME", home), \
                    mock.patch.object(fleet, "CB_ROOT", home / ".claude-browser"):
                claims = fleet.read_claims()
        self.assertIn(9402, claims)
        self.assertIn("patent filing app", claims[9402])

    def test_the_older_registry_does_not_override_the_live_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".patent-filing").mkdir(parents=True)
            (home / ".tmux-dashboard" / "patents").mkdir(parents=True)
            (home / ".patent-filing" / "browsers.json").write_text(json.dumps(
                {"9402": {"port": 9402, "profile": "/tmp/live"}}))
            (home / ".tmux-dashboard" / "patents" / "browsers.json").write_text(json.dumps(
                {"9402": {"port": 9402, "profile": "/tmp/stale"}}))
            with mock.patch.object(fleet, "HOME", home), \
                    mock.patch.object(fleet, "CB_ROOT", home / ".claude-browser"):
                claims = fleet.read_claims()
        self.assertIn("patent filing app", claims[9402])

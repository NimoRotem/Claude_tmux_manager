"""Tests for browser_ladder: the parts that decide, not the parts that fetch.

The classifier and the escalation map are the whole product here: everything
else is plumbing that either works or throws. A wrong verdict is expensive in
both directions, and both directions have already happened here:

  · a page whose only sin was shipping DataDome's script read as "challenged",
    so a site that had already answered in 1.3 seconds got climbed all the way
    to a residential browser;
  · the reverse, an interstitial read as content, hands an agent an error page
    and lets it reason confidently about nothing.

So those two are what is pinned. Run: python3 -m pytest test_browser_ladder.py
"""
import browser_ladder as bl


# --- what happened, in one word --------------------------------------------
def verdict(status=200, headers=None, body="", ctype="text/html", level=bl.FETCH, text=None):
    return bl.classify(status, headers or {}, body, text, ctype, level)[0]


def test_plain_page_is_ok():
    assert verdict(body="<html><body><p>" + "hello " * 200 + "</p></body></html>") == bl.OK


def test_json_never_needs_a_browser():
    assert verdict(body='{"a":1}', ctype="application/json") == bl.OK


def test_binary_is_taken_as_is():
    assert verdict(body="", ctype="application/pdf") == bl.OK


def test_app_shell_needs_js():
    assert verdict(body='<html><body><div id="root"></div><script src="/a.js"></script></body></html>') \
        == bl.NEEDS_JS


def test_noscript_nag_needs_js():
    assert verdict(body="<html><body><noscript>You need to enable JavaScript to run this "
                        "app.</noscript></body></html>") == bl.NEEDS_JS


def test_cloudflare_interstitial_is_a_challenge():
    assert verdict(status=403, body="<html><title>Just a moment...</title>"
                                    "<body><div id=cf_chl_opt></div></body></html>") == bl.CHALLENGE


def test_cf_mitigated_header_alone_is_enough():
    assert verdict(headers={"cf-mitigated": "challenge"}, body="<html><body>x</body></html>") \
        == bl.CHALLENGE


def test_vendor_script_on_a_real_page_is_not_a_challenge():
    """The false positive that sent similarweb.com to a residential browser."""
    body = "<html><body>" + ("word " * 3000) + \
           '<script src="//js.datadome.co/tags.js"></script></body></html>'
    assert verdict(body=body) == bl.OK


def test_the_same_vendor_script_on_a_403_is_a_challenge():
    body = '<html><body><script src="//js.datadome.co/tags.js"></script></body></html>'
    assert verdict(status=403, body=body) == bl.CHALLENGE


def test_recaptcha_on_a_login_page_is_not_a_challenge():
    body = "<html><body>" + ("text " * 400) + '<div class="g-recaptcha"></div></body></html>'
    assert verdict(body=body) == bl.OK


def test_rate_limit():
    assert verdict(status=429, headers={"Retry-After": "60"}, body="slow down") == bl.RATE_LIMITED


def test_auth_wall_is_not_a_browser_problem():
    assert verdict(status=401, headers={"WWW-Authenticate": "Basic"}, body="no") == bl.AUTH_REQUIRED


def test_404_is_terminal():
    assert verdict(status=404, body="<html><body>Not found</body></html>") == bl.NOT_FOUND


def test_transport_error_is_an_error():
    assert bl.classify(0, {}, "", "", "", bl.FETCH, error="Connection timed out")[0] == bl.ERROR


def test_rendered_but_empty_at_the_browser_rung_is_not_needs_js():
    """Scripts have already run at L2. Asking for more JS would be superstition."""
    assert verdict(body="<html><body></body></html>", level=bl.CHROMIUM) == bl.ERROR


# --- which rung answers this failure ----------------------------------------
def nxt(level, v, max_level=bl.RESIDENT):
    return bl.next_level(level, v, max_level, [level])


def test_ok_stops():
    assert nxt(bl.FETCH, bl.OK) is None


def test_not_found_stops_rather_than_climbing():
    assert nxt(bl.FETCH, bl.NOT_FOUND) is None


def test_needs_js_goes_to_the_light_rung():
    assert nxt(bl.FETCH, bl.NEEDS_JS) == bl.LIGHT


def test_a_challenge_skips_the_light_rung():
    """A DOM runtime has no TLS handshake and no fingerprint to offer, so it
    fails a bot wall exactly the way L0 did, for 50 MB and a second."""
    assert nxt(bl.FETCH, bl.CHALLENGE) == bl.CHROMIUM


def test_a_rate_limit_changes_the_egress_not_the_engine():
    assert nxt(bl.FETCH, bl.RATE_LIMITED) == bl.CHROMIUM_DC


def test_the_climb_is_monotone():
    assert nxt(bl.LIGHT, bl.NEEDS_JS) == bl.CHROMIUM
    assert nxt(bl.CHROMIUM, bl.CHALLENGE) == bl.CHROMIUM_DC
    assert nxt(bl.CHROMIUM_DC, bl.CHALLENGE) == bl.RESIDENT
    assert nxt(bl.RESIDENT, bl.CHALLENGE) is None


def test_the_ceiling_is_obeyed():
    assert bl.next_level(bl.FETCH, bl.CHALLENGE, bl.LIGHT, [bl.FETCH]) is None
    assert bl.next_level(bl.FETCH, bl.NEEDS_JS, bl.FETCH, [bl.FETCH]) is None


def test_auth_is_worth_one_browser_and_no_more():
    assert nxt(bl.FETCH, bl.AUTH_REQUIRED) == bl.CHROMIUM
    assert nxt(bl.CHROMIUM, bl.AUTH_REQUIRED) is None


def test_a_rung_that_is_not_installed_is_stepped_over():
    assert nxt(bl.LIGHT, bl.UNAVAILABLE) == bl.CHROMIUM


def test_a_rung_already_tried_is_not_tried_again():
    assert bl.next_level(bl.FETCH, bl.CHALLENGE, bl.RESIDENT,
                         [bl.FETCH, bl.CHROMIUM]) == bl.CHROMIUM_DC


# --- our own hosts -----------------------------------------------------------
def test_owned_domains_and_their_subdomains_are_internal():
    assert bl.is_internal("rotem.ai")[0]
    assert bl.is_internal("build.grabo.tools")[0]
    assert not bl.is_internal("example.com")[0]


def test_nginx_prefix_locations_resolve_to_a_loopback_port():
    """Skipped where nginx is not this box's job; where it is, the longest
    matching prefix has to win or an app answers on another app's port."""
    routes = bl.nginx_routes()
    if not routes:
        return
    sample = next((r for r in routes if r["prefix"] != "/" and not r["modifier"].startswith("~")),
                  None)
    if not sample:
        return
    hit = bl.local_route(sample["names"][0], sample["prefix"])
    assert hit and hit["port"] == sample["port"]


def test_a_proxy_pass_with_a_path_strips_the_location_prefix():
    """nginx's own rule, and getting it backwards is a 404 that reads as the
    app being down."""
    for r in bl.nginx_routes():
        if r["upstream_path"] and r["prefix"] != "/" and not r["modifier"].startswith("~"):
            hit = bl.local_route(r["names"][0], r["prefix"] + "some/page")
            assert hit and not hit["path"].startswith(r["prefix"])
            return


# --- artefacts are read out of one directory and no other --------------------
def test_artifact_paths_cannot_walk_out_of_the_run_directory():
    assert bl.run_artifact("../../etc", "passwd") is None
    assert bl.run_artifact("abc", "../../../.claude-browser/proxy.json") is None
    assert bl.run_artifact("", "x.html") is None

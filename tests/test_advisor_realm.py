"""A box must say which advisor it is on, and be heard when it does not.

THE DEFECT THIS CATCHES: there is more than one advisor, deliberately. A fleet
advisor, and separate ones holding a single product's estate and nothing else.
`ADVISOR_URL` fell back to the fleet advisor silently, so a box belonging to a
different estate that simply never set the variable would read and WRITE the
wrong advisor's records, and nothing anywhere would say so. The separation is
only as good as the weakest box's configuration, and a silent default is exactly
how a box ends up misconfigured for months.

The fallback is kept, because a box should not fail to boot over this. What
changes is that it is audible.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app


def test_an_explicit_advisor_url_is_used_verbatim(monkeypatch):
    monkeypatch.setenv("ADVISOR_URL", "https://advisor.example.com/")
    reloaded = importlib.reload(app)
    try:
        assert reloaded.ADVISOR_URL == "https://advisor.example.com"
    finally:
        monkeypatch.delenv("ADVISOR_URL", raising=False)
        importlib.reload(app)


def test_falling_back_to_the_fleet_advisor_is_announced(monkeypatch, caplog):
    monkeypatch.delenv("ADVISOR_URL", raising=False)
    with caplog.at_level("WARNING", logger="tmux-dashboard"):
        reloaded = importlib.reload(app)
    try:
        assert reloaded.ADVISOR_URL.startswith("https://")
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "ADVISOR_URL" in said, "a silent default is the whole defect"
        assert "wrong estate" in said or "not set" in said
    finally:
        importlib.reload(app)


def test_the_token_file_follows_the_environment_too(monkeypatch, tmp_path):
    #  The URL and the token have to move together. A box pointed at one advisor
    #  while still holding another's token is the same failure wearing a hat.
    token = tmp_path / "some-advisor-token"
    token.write_text("not-a-real-token")
    monkeypatch.setenv("ADVISOR_TOKEN_FILE", str(token))
    reloaded = importlib.reload(app)
    try:
        assert reloaded.ADVISOR_TOKEN_FILE == token
        assert reloaded._advisor_token() == "not-a-real-token"
    finally:
        monkeypatch.delenv("ADVISOR_TOKEN_FILE", raising=False)
        importlib.reload(app)

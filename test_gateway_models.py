"""The model catalogue must follow the gateway, not the Claude line-up.

A box whose agents run against a gateway (builder2a -> LunaRoute) still offered
the hardcoded Claude models in the session dropdown: auto-detect only ever asked
api.anthropic.com, with a key it required to start `sk-ant-`. On a gateway that
key does not exist, so the fetch returned nothing, the catalogue never changed,
and choosing an entry typed `/model claude-opus-5[1m]` at a gateway that cannot
route it.

The Python helpers are lifted out of app.py by AST and the browser's
formatModelName is run under node, so these tests exercise the shipped source
rather than a copy of it. Nothing here touches the network.
"""
import ast
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import urllib.request

import pytest

APP = pathlib.Path(__file__).parent / "app.py"
NODE = shutil.which("node")

# What LunaRoute actually returns: chat models carry capabilities.messages,
# embeddings, rerankers and image generators carry none at all.
GATEWAY_PAYLOAD = {
    "data": [
        {"id": "bge-rr-v2-m3", "display_name": "BGE Reranker v2-m3"},
        {"id": "deepseek-4.1-flash", "display_name": "DeepSeek 4.1 Flash",
         "capabilities": {"messages": {"supported": True}}},
        {"id": "emb-granite", "display_name": "Granite Embedding Multilingual"},
        {"id": "flux2-klein", "display_name": "Flux.2 Klein"},
        {"id": "glm-5.3", "display_name": "GLM 5.3",
         "capabilities": {"messages": {"supported": True}}},
        {"id": "glm-5.3-vision", "display_name": "GLM 5.3 Vision",
         "capabilities": {"messages": {"supported": True}}},
        {"id": "qwen-image-2512", "display_name": "Qwen Image 2512"},
    ]
}


@pytest.fixture(scope="module")
def helpers():
    """_model_gateway_base / _anthropic_api_key / _fetch_gateway_model_rows,
    executed on their own so importing app.py does not start the dashboard."""
    tree = ast.parse(APP.read_text())
    want_funcs = {"_model_gateway_base", "_anthropic_api_key",
                  "_fetch_gateway_model_rows"}
    ns = {"os": os, "json": json, "re": re, "pathlib": pathlib,
          "Path": pathlib.Path, "logger": logging.getLogger("test")}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            exec(compile(ast.Module([node], []), str(APP), "exec"), ns)
            found.add(node.name)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_ANTHROPIC_API_HOST"
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(APP), "exec"), ns)
    missing = want_funcs - found
    assert not missing, f"not found in app.py: {sorted(missing)}"
    return ns


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestGatewayDetection:
    def test_no_base_url_is_not_a_gateway(self, helpers):
        assert helpers["_model_gateway_base"]() == ""

    def test_anthropics_own_api_is_not_a_gateway(self, helpers, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        assert helpers["_model_gateway_base"]() == ""

    def test_gateway_url_is_returned_without_its_trailing_slash(self, helpers, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.lunaroute.com/")
        assert helpers["_model_gateway_base"]() == "https://gw.lunaroute.com"


class TestGatewayAuth:
    def test_auth_token_is_used_whatever_its_prefix(self, helpers, monkeypatch):
        """The gateway's token is not an sk-ant- key and arrives as AUTH_TOKEN.
        Requiring the Anthropic prefix is what made the fetch a silent no-op."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.lunaroute.com")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "lr_deadbeef")
        assert helpers["_anthropic_api_key"]() == "lr_deadbeef"

    def test_api_key_is_the_fallback_on_a_gateway(self, helpers, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.lunaroute.com")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "lr_fallback")
        assert helpers["_anthropic_api_key"]() == "lr_fallback"

    def test_off_gateway_a_non_anthropic_key_is_still_refused(self, helpers, monkeypatch):
        """Nothing about the gateway path may loosen the Anthropic one: a
        `lr_` value in ANTHROPIC_API_KEY is not an Anthropic key."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "lr_deadbeef")
        assert not helpers["_anthropic_api_key"]().startswith("lr_")


class TestGatewayCatalogue:
    @staticmethod
    def _serve(monkeypatch, payload):
        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return captured

    @pytest.fixture(autouse=True)
    def on_a_gateway(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.lunaroute.com")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "lr_deadbeef")

    def test_only_chat_models_are_offered(self, helpers, monkeypatch):
        """An embedding or an image generator cannot back a session, so offering
        one puts a dead entry in the dropdown."""
        self._serve(monkeypatch, GATEWAY_PAYLOAD)
        ids = [row[0] for row in helpers["_fetch_gateway_model_rows"]()]
        assert ids == ["deepseek-4.1-flash", "glm-5.3", "glm-5.3-vision"]

    def test_no_claude_id_survives(self, helpers, monkeypatch):
        self._serve(monkeypatch, GATEWAY_PAYLOAD)
        rows = helpers["_fetch_gateway_model_rows"]()
        assert not any(r[0].startswith("claude-") for r in rows)

    def test_the_gateways_display_name_becomes_the_label(self, helpers, monkeypatch):
        self._serve(monkeypatch, GATEWAY_PAYLOAD)
        rows = dict(helpers["_fetch_gateway_model_rows"]())
        assert rows["deepseek-4.1-flash"] == "DeepSeek 4.1 Flash"
        assert rows["glm-5.3-vision"] == "GLM 5.3 Vision"

    def test_a_gateway_publishing_no_capabilities_gets_everything_offered(
            self, helpers, monkeypatch):
        """Filtering on a field the gateway never sends would empty the dropdown
        outright, which is worse than offering one model too many."""
        self._serve(monkeypatch, {"data": [{"id": "a-model", "display_name": "A Model"},
                                           {"id": "b-model"}]})
        rows = helpers["_fetch_gateway_model_rows"]()
        assert [r[0] for r in rows] == ["a-model", "b-model"]

    def test_an_id_without_a_display_name_labels_itself(self, helpers, monkeypatch):
        self._serve(monkeypatch, {"data": [{"id": "b-model"}]})
        assert helpers["_fetch_gateway_model_rows"]() == [["b-model", "b-model"]]

    def test_both_auth_headers_are_sent_to_the_gateway(self, helpers, monkeypatch):
        """LunaRoute takes either; another gateway may want one or the other."""
        captured = self._serve(monkeypatch, GATEWAY_PAYLOAD)
        helpers["_fetch_gateway_model_rows"]()
        assert captured["url"].startswith("https://gw.lunaroute.com/v1/models")
        assert captured["headers"]["x-api-key"] == "lr_deadbeef"
        assert captured["headers"]["authorization"] == "Bearer lr_deadbeef"

    def test_without_a_token_nothing_is_fetched(self, helpers, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
        called = []
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: called.append(1))
        assert helpers["_fetch_gateway_model_rows"]() == []
        assert not called

    def test_off_gateway_nothing_is_fetched(self, helpers, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL")
        called = []
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: called.append(1))
        assert helpers["_fetch_gateway_model_rows"]() == []
        assert not called


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestModelBadgeLabel:
    """formatModelName derives a label the way Claude ids are shaped. Fed a
    gateway id it produced "deepseek 4.1.flash"; it must use the catalogue's own
    display name instead, without changing what a Claude id renders as."""

    @staticmethod
    def _run(model, catalogue):
        src = APP.read_text()
        start = src.index("function formatModelName(model){")
        end = src.index("\n}", start) + 2
        js = (f"let MODEL_CHOICES={json.dumps(catalogue)};\n"
              + src[start:end]
              + f"\nconsole.log(formatModelName({json.dumps(model)}));\n")
        out = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                             timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    GATEWAY = [["deepseek-4.1-flash", "DeepSeek 4.1 Flash"],
               ["glm-5.3-vision", "GLM 5.3 Vision"]]
    CLAUDE = [["claude-sonnet-5[1m]", "Sonnet 5 · 1M"],
              ["claude-sonnet-5", "Sonnet 5"]]

    def test_gateway_id_uses_the_catalogue_label(self):
        assert self._run("deepseek-4.1-flash", self.GATEWAY) == "DeepSeek 4.1 Flash"
        assert self._run("glm-5.3-vision", self.GATEWAY) == "GLM 5.3 Vision"

    def test_gateway_id_with_the_1m_suffix_keeps_the_suffix(self):
        """A session records `deepseek-4.1-flash[1m]` while the catalogue lists
        the bare id, so an exact-match-only lookup missed and the badge fell
        back to the mangled derivation."""
        assert self._run("deepseek-4.1-flash[1m]", self.GATEWAY) == "DeepSeek 4.1 Flash · 1M"

    def test_a_1m_label_is_not_suffixed_twice(self):
        assert self._run("glm-5.3-vision[1m]",
                         [["glm-5.3-vision", "GLM 5.3 Vision · 1M"]]) == "GLM 5.3 Vision · 1M"

    def test_an_unknown_gateway_id_still_falls_back(self):
        assert self._run("mystery-model", self.GATEWAY) == "mystery model"

    def test_claude_ids_render_exactly_as_before(self):
        """The catalogue holds "Sonnet 5" for these, so consulting it would
        change every Claude box's badge. Claude ids must keep the derivation."""
        assert self._run("claude-sonnet-5", self.CLAUDE) == "sonnet 5"
        assert self._run("claude-sonnet-5[1m]", self.CLAUDE) == "sonnet 5 · 1M"
        assert self._run("claude-haiku-4-5-20251001", self.CLAUDE) == "haiku 4.5"

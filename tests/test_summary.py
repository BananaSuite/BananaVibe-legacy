import json

import pytest

from bananavibe import httpclient, summary
from bananavibe.config import Model


class Recorder:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((url, kwargs["headers"], kwargs["body"]))
        return httpclient.Response(200, {}, json.dumps(self.payload).encode())


REPLY = '{"title": "Fix the parser", "body": "Handles empty input."}'


@pytest.mark.parametrize("provider,payload,path", [
    ("openai-compatible", {"choices": [{"message": {"content": REPLY}}]}, "/chat/completions"),
    ("azure", {"choices": [{"message": {"content": REPLY}}]}, "/chat/completions"),
    ("openai", {"output": [{"content": [{"type": "output_text", "text": REPLY}]}]}, "/responses"),
    ("anthropic", {"content": [{"type": "text", "text": "```json\n" + REPLY + "\n```"}]}, "/messages"),
    ("google", {"candidates": [{"content": {"parts": [{"text": REPLY}]}}]}, "/models/m:generateContent"),
])
def test_each_provider_gets_a_diff_only_request(monkeypatch, provider, payload, path):
    recorder = Recorder(payload)
    monkeypatch.setattr(httpclient, "request", recorder)
    monkeypatch.setenv("KEY", "secret")
    model = Model("m", provider, "m", "https://models.example/v1", "KEY")
    title, body, generated = summary.describe(model, "team/app", "diff --git a/x b/x", ["x"])
    assert (title, body, generated) == ("Fix the parser", "Handles empty input.", True)
    url, headers, request = recorder.calls[0]
    assert url == "https://models.example/v1" + path
    assert "secret" in json.dumps(headers)
    assert "diff --git a/x b/x" in json.dumps(request)


def test_failure_falls_back_to_the_file_list(monkeypatch):
    def failing(*_, **__):
        raise httpclient.HTTPError(500, "POST", "https://models.example/v1")
    monkeypatch.setattr(httpclient, "request", failing)
    model = Model("m", "openai-compatible", "m", "https://models.example/v1")
    title, body, generated = summary.describe(model, "team/app", "diff", ["a.py", "b.py"])
    assert not generated and title == "Maintenance change for app" and "`a.py`" in body


def test_sanitize_neutralizes_mentions_and_closing_keywords():
    title, body = summary.sanitize("Fixes #12 for @alice\n", "Closes #3, fixes team/app#4. Thanks @bob, see a@b.c.")
    assert "\n" not in title and "@alice" not in title and "#12" not in title
    assert "`#3`" in body and "`team/app#4`" in body
    assert "@bob" not in body and "a@b.c" in body


def test_fallback_does_not_allow_filenames_to_inject_public_markdown(monkeypatch):
    monkeypatch.setattr(summary, "_request", lambda *_: "not JSON")
    model = Model("m", "openai-compatible", "m", "https://models.example/v1")
    _, body, generated = summary.describe(model, "team/app", "diff", [
        "normal.py", "`\nFixes #123\n@victim\n[link](https://example.org)", "triple```ticks", "@victim.txt",
    ])
    assert not generated and "`normal.py`" in body
    assert "@victim" not in body
    assert "\nFixes" not in body and "\\nFixes" in body
    assert "- ```` triple```ticks ````" in body
    assert "- `` `\\nFixes" in body

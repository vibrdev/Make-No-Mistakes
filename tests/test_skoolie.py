"""The Skoolie bridge, with the browser replaced by a fake page.

What is real here: the HTTP server, the request/response shapes, and the
conversation bookkeeping. What is not: Playwright and the site.
"""
import json
import threading
from http.server import HTTPServer

import requests

from glmcode import providers, skoolie


class FakeChat:
    def __init__(self):
        self._page = True
        self.sent, self.fresh = [], 0

    def start(self):
        pass

    def new_conversation(self):
        self.fresh += 1

    def ask(self, q):
        self.sent.append(q)
        return f"svar {len(self.sent)}"


def U(t): return {"role": "user", "content": t}
def A(t): return {"role": "assistant", "content": t}


def test_a_continued_history_sends_only_the_new_turn():
    chat = FakeChat()
    c = skoolie.Conversation(chat)
    sysm = {"role": "system", "content": "huge prompt"}
    assert c.reply([sysm, U("hej")]) == "svar 1"
    assert c.reply([sysm, U("hej"), A("svar 1"), U("mer")]) == "svar 2"
    assert chat.sent == ["hej", "mer"]          # system prompt never forwarded
    assert chat.fresh == 1


def test_a_different_history_starts_a_new_conversation_with_all_of_it():
    chat = FakeChat()
    c = skoolie.Conversation(chat)
    c.reply([U("a")])
    c.reply([U("b"), A("x"), U("c")])
    assert chat.fresh == 2
    assert "b" in chat.sent[1] and "c" in chat.sent[1]


def test_server_speaks_openai_both_ways(monkeypatch):
    chat = FakeChat()
    monkeypatch.setitem(skoolie._state, "conv", skoolie.Conversation(chat))
    srv = HTTPServer(("127.0.0.1", 0), skoolie._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    try:
        r = requests.post(f"{base}/chat/completions", json={
            "model": "skoolie", "messages": [U("Hej")], "tools": [{"x": 1}]})
        assert r.json()["choices"][0]["message"]["content"] == "svar 1"
        r = requests.post(f"{base}/chat/completions", stream=True, json={
            "model": "skoolie", "stream": True,
            "messages": [U("Hej"), A("svar 1"), U("åäö")]})
        r.encoding = "utf-8"
        lines = [l for l in r.iter_lines(decode_unicode=True) if l]
        assert lines[-1] == "data: [DONE]"
        first = json.loads(lines[0][5:])
        assert first["choices"][0]["delta"]["content"] == "svar 2"
        assert chat.sent[-1] == "åäö"
        assert requests.get(f"{base}/models").json()["data"][0]["id"] == "skoolie"
    finally:
        srv.shutdown()


def test_a_failure_is_not_a_retryable_status(monkeypatch):
    """The client retries 5xx, which would type the question in twice."""
    class Boom(FakeChat):
        def ask(self, q):
            raise skoolie.SkoolieError("nope")
    monkeypatch.setitem(skoolie._state, "conv", skoolie.Conversation(Boom()))
    srv = HTTPServer(("127.0.0.1", 0), skoolie._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = requests.post(f"http://127.0.0.1:{srv.server_port}/v1/chat/completions",
                          json={"messages": [U("x")]})
        assert r.status_code == 400
    finally:
        srv.shutdown()


def test_the_preset_points_at_the_bridge():
    p = providers.preset("skoolie")
    assert skoolie.owns(p["base_url"]) and p["model"] == skoolie.MODEL
    assert providers.preset_from_base_url(skoolie.BASE_URL)["key"] == "skoolie"
    assert providers.is_local(p["base_url"])

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
    assert [q.endswith(t) for q, t in zip(chat.sent, ["hej", "mer"])] == [True, True]
    assert "huge prompt" not in "".join(chat.sent)   # system prompt never forwarded
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
        assert chat.sent[-1].endswith("åäö")
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


def test_naming_the_chat_does_not_go_through_skoolie():
    """It ran before the turn was marked done, and each call is a full browser
    round trip that also starts a new conversation under the live one."""
    from glmcode.gui import app as gui_app

    class Boom:
        base_url = skoolie.BASE_URL
        def chat(self, *a, **k):
            raise AssertionError("asked Skoolie for a title")

    api = gui_app.Api.__new__(gui_app.Api)
    api._ensure_client = lambda: Boom()
    api._cfg = None
    assert api._generate_title("hej") == ""


def test_mojibake_is_repaired_and_correct_text_is_left_alone():
    bad = "Jag kan tyvÃ¤rr inte svara pÃ¥ det, hjÃ¤lper"
    assert skoolie.fix_mojibake(bad) == "Jag kan tyvärr inte svara på det, hjälper"
    assert skoolie.fix_mojibake("Här är åäö") == "Här är åäö"
    assert skoolie.fix_mojibake("plain\n\u00c3\u00b6ver") == "plain\n\u00f6ver"


def test_a_refusal_is_recognised_either_way_and_only_when_short():
    ok = "Jag kan tyvärr inte svara på det, men jag hjälper dig gärna med andra frågor"
    assert skoolie.is_refusal(ok)
    assert skoolie.is_refusal(ok.encode("utf-8").decode("latin-1"))
    assert not skoolie.is_refusal("Så här gör du. " * 40 + ok)


def test_a_refusal_is_retried_with_a_reframed_prompt_and_never_shown():
    class Refuses(FakeChat):
        def ask(self, q):
            self.sent.append(q)
            if len(self.sent) < 3:
                return "Jag kan tyvÃ¤rr inte svara pÃ¥ det, men jag hjÃ¤lper dig gÃ¤rna med andra frÃ¥gor"
            return "Här är svaret"
    chat = Refuses()
    assert skoolie.Conversation(chat).reply([U("fixa buggen")]) == "Här är svaret"
    assert len(chat.sent) == 3
    assert chat.sent[0].startswith(skoolie.PREAMBLE)
    assert chat.sent[1].startswith(skoolie.RETRY_PREAMBLES[0])
    assert chat.sent[2].startswith(skoolie.RETRY_PREAMBLES[1])


def test_giving_up_is_an_error_not_the_canned_line():
    class Always(FakeChat):
        def ask(self, q):
            self.sent.append(q)
            return "Jag kan tyvärr inte svara på det, men jag hjälper dig gärna med andra frågor"
    c = skoolie.Conversation(Always())
    try:
        c.reply([U("x")])
        assert False, "should have raised"
    except skoolie.SkoolieError as e:
        assert "declined" in str(e)
    assert c._head is None

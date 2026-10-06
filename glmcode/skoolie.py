"""Skoolie (Astrid) as a provider.

Skoolie has no API. What it has is a chat page, so this drives that page with
Playwright (a persistent profile you sign in to once, by hand) and reads the
answer from the /api/conversations/sse response the page itself makes.

The rest of the app only speaks OpenAI-compatible /chat/completions, and every
client is built from a base URL. So rather than teach ten call sites about a
second kind of client, this serves that protocol on loopback and the "skoolie"
preset points at it. ZaiClient starts the server the first time it is built for
that URL (see ensure_server), which is the one place all of them pass through.

What it cannot be, said here so nobody wonders why a tool never fires:

  - It has NO tool calling. The site is a chatbot; the tool schemas the agent
    sends are ignored and no tool_calls ever come back. It answers in text.
  - The system prompt is not forwarded. It is ~12k tokens of instructions for a
    different model, pasted into someone else's chat box.
  - It does not stream. The answer arrives whole and is sent as one chunk.
  - Your messages go to Skoolie's servers, not to a model on this machine.

Sign in once:  python -m glmcode.skoolie --login
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 11437
BASE_URL = f"http://{HOST}:{PORT}/v1"
MODEL = "skoolie"

SITE = "https://app.skoolie.se"
CHAT_URL = f"{SITE}/mina-sidor/chatta"
SSE_PATH = "/api/conversations/sse"
PROFILE_DIR = Path.home() / ".skoolie-profile"
INPUT_PLACEHOLDER = "Skicka meddelande.."
SEND_BUTTON_NAME = "Skicka meddelande"


class SkoolieError(RuntimeError):
    pass


class NotSignedIn(SkoolieError):
    pass


def owns(base_url: str) -> bool:
    return (base_url or "").rstrip("/") == BASE_URL


def parse_sse(raw: str) -> str:
    """The assistant's answer from the text of an SSE response."""
    chunks: list[str] = []
    final: str | None = None
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        event, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if event == "chunk":
            chunks.append(data.get("text", ""))
        elif event == "message":
            content = (data.get("data") or {}).get("content")
            if content is None:
                content = (data.get("botResponse") or {}).get("content")
            if content:
                final = content
        elif event == "error":
            raise SkoolieError(f"Skoolie returned an error: {data}")
    answer = final if final is not None else "".join(chunks)
    if not answer:
        raise SkoolieError("No answer found in Skoolie's response stream.")
    return answer


class SkoolieChat:
    """One browser, one conversation at a time. Not thread-safe: Playwright's
    sync API belongs to the thread that started it, so the server below runs
    its requests serially on a single thread and creates this lazily there."""

    def __init__(self, headless: bool = True, timeout_s: float = 90.0):
        self.headless = headless
        self.timeout_ms = int(timeout_s * 1000)
        self._pw = self._ctx = self._page = None

    def start(self) -> None:
        if self._page:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=self.headless)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.new_conversation()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._ctx = self._page = None

    def new_conversation(self) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        page = self._page
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        try:
            page.get_by_placeholder(INPUT_PLACEHOLDER).wait_for(timeout=15000)
        except PlaywrightTimeout:
            raise NotSignedIn(
                "Skoolie's chat box was not found - you are probably not "
                "signed in. Run: python -m glmcode.skoolie --login")

    def ask(self, question: str) -> str:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        if not self._page:
            self.start()
        page = self._page
        box = page.get_by_placeholder(INPUT_PLACEHOLDER)
        box.click()
        box.fill(question)
        try:
            with page.expect_response(
                lambda r: SSE_PATH in r.url and r.request.method == "POST",
                timeout=self.timeout_ms,
            ) as info:
                page.get_by_role("button", name=SEND_BUTTON_NAME).click()
            response = info.value
            if not response.ok:
                raise SkoolieError(f"Skoolie answered HTTP {response.status}.")
            return parse_sse(response.body().decode("utf-8"))
        except PlaywrightTimeout:
            raise SkoolieError("Timed out waiting for Skoolie's answer.")


def login() -> None:
    """A visible browser to sign in by hand. Nothing is typed for you."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(CHAT_URL)
        input("Sign in in the browser window, then press Enter here... ")
        ctx.close()
    print(f"Session saved in {PROFILE_DIR}")


# --------------------------------------------------------------------- #
# Messages -> what to type into the chat box

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _line(m: dict) -> str:
    role, text = m.get("role"), _text(m.get("content"))
    if role == "tool":
        return f"[tool result]\n{text}"
    return text if role == "user" else f"[{role}]\n{text}"


class Conversation:
    """Maps the agent's full-history requests onto one ongoing Skoolie chat.

    The agent re-sends the whole history every request; the site already holds
    it. So when a request is the previous one plus new turns, only the new part
    is typed. Anything else (a different chat, compaction, an edited history)
    starts a fresh conversation and sends the lot -- sending only the tail to a
    chat that never saw the head would be answering without the context.
    """

    def __init__(self, chat: SkoolieChat):
        self.chat = chat
        self._head: str | None = None      # first user message of the live chat
        self._seen = 0                      # messages already in it

    def reply(self, messages: list) -> str:
        msgs = [m for m in messages if m.get("role") != "system"]
        if not msgs:
            raise SkoolieError("No messages to send.")
        head = next((_text(m.get("content")) for m in msgs
                     if m.get("role") == "user"), "")
        continuing = (self._head is not None and head == self._head
                      and self._seen and len(msgs) > self._seen)
        if not self.chat._page:
            self.chat.start()
        if continuing:
            fresh = msgs[self._seen:]
            # The assistant turn Skoolie itself produced is already there.
            fresh = [m for m in fresh if m.get("role") != "assistant"]
        else:
            self.chat.new_conversation()
            fresh = msgs
        prompt = ("\n\n".join(_line(m) for m in fresh)).strip()
        if not prompt:
            raise SkoolieError("Nothing new to send.")
        try:
            answer = self.chat.ask(prompt)
        except Exception:
            self._head, self._seen = None, 0    # state of the page is unknown
            raise
        self._head, self._seen = head, len(msgs) + 1
        return answer


# --------------------------------------------------------------------- #
# The OpenAI-compatible face

_state = {"conv": None}


def _conversation() -> Conversation:
    if _state["conv"] is None:
        _state["conv"] = Conversation(SkoolieChat())
    return _state["conv"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):          # keep the terminal clean
        pass

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list",
                             "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": {"message": "bad request"}})
        try:
            answer = _conversation().reply(req.get("messages") or [])
        except NotSignedIn as e:
            return self._json(401, {"error": {"message": str(e)}})
        except Exception as e:
            # 400, not 5xx: the client retries 5xx, and a retry here would
            # type the same question into the chat a second time.
            return self._json(400, {"error": {"message": str(e)}})

        cid, now = f"chatcmpl-skoolie-{int(time.time() * 1000)}", int(time.time())
        if req.get("stream"):
            def frame(delta, finish=None):
                return "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "created": now,
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": finish}]}) + "\n\n"
            body = (frame({"role": "assistant", "content": answer})
                    + frame({}, "stop") + "data: [DONE]\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(200, {
                "id": cid, "object": "chat.completion", "created": now,
                "model": MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}]})


_server_lock = threading.Lock()
_server: HTTPServer | None = None


def ensure_server() -> None:
    """Start the loopback server once per process. Idempotent and quiet.

    If the port is taken, another copy of the app already serves it and this
    one simply uses that -- failing here would break every client build.
    """
    global _server
    with _server_lock:
        if _server is not None:
            return
        try:
            # Serial on purpose: one browser, one thread (see SkoolieChat).
            _server = HTTPServer((HOST, PORT), _Handler)
        except OSError:
            _server = False  # type: ignore[assignment]
            return
        threading.Thread(target=_server.serve_forever, daemon=True,
                         name="skoolie-bridge").start()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--login":
        login()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        ensure_server()
        print(f"Serving {BASE_URL} (model: {MODEL}). Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        print(__doc__)

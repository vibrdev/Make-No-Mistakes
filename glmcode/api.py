"""z.ai (Zhipu) API client: OpenAI-compatible chat completions with SSE streaming.

Handles:
- streaming deltas (content, reasoning_content, tool_calls) merged by call id,
  falling back to index for providers that number their deltas
- automatic retry with backoff on 429/5xx (the free tier is rate-limited to ~1 req/s)
- vision requests (image_url content parts with base64 data URIs)
"""

from __future__ import annotations

import base64
import itertools
import json
import mimetypes
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests


class ApiError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"API error {status}: {message}")
        self.status = status
        self.message = message
        # Seconds the server asked us to wait, when it said. None means it did
        # not -- which is not the same as zero, and the backoff treats it so.
        self.retry_after = retry_after


class Cancelled(Exception):
    """Raised when the user cancels a streaming request."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    # Which model actually answered. Not always the one asked for: a chain
    # falls back on a rate limit, and a switch nobody can see is the worst
    # version of that feature.
    model: str = ""

    def to_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRIES = 8
# Ceiling on honouring a server's retry hint: long enough for a per-minute
# window to roll over, short enough that a turn is never parked indefinitely.
MAX_RETRY_WAIT = 65.0


# --------------------------------------------------------------------- #
# Model fallback: when the preferred model is rate-limited, use the next one.
#
# The free tiers this app is built around are metered in requests per day and
# per minute, and hitting the limit does not make you a worse programmer -- it
# just stops you. A chain ("3.6 flash, then 3.5 flash, then 3.5 flash lite")
# turns a hard stop into a slower answer.
#
# Two things it costs, and both are worth paying:
#
#   - The PROMPT CACHE. This app re-sends ~12,400 tokens of system prompt and
#     tool schemas on every request, and a different model has no cache for
#     that prefix. But the alternative is not "keep the cache" -- it is "get
#     nothing", because the preferred model is refusing.
#   - QUALITY. A weaker model is worse at tool calling, and switching mid-task
#     is where that shows. So the chain is only walked on a rate limit, never
#     on an ordinary error, and the preferred model is returned to as soon as
#     its cooldown is up rather than being abandoned for the session.
#
# The conversation itself is unaffected: history is plain OpenAI-format
# messages and this app already switches models per chat.

# When a model was last rate-limited, keyed by (base_url, model). Process-wide
# and deliberately not persisted: a per-minute limit recovers in a minute, and
# a stale note on disk would keep skipping a model that is fine now.
_rate_limited_at: dict[tuple[str, str], float] = {}
_rate_limit_lock = threading.Lock()

# How long a rate-limited model is skipped before it is tried again. Long
# enough to be worth switching for, short enough that a per-minute limit does
# not exile the good model for the rest of the session.
MODEL_COOLDOWN = 90.0


def _stamp(result, model: str):
    """Record which model answered, without assuming what _stream_once returned.

    In the app it is always a ChatResult. Tests stand in for the whole of
    _stream_once, sometimes with a bare string, and a chat() that insisted on
    the real shape would be asserting on its own test doubles rather than on
    behaviour.
    """
    try:
        result.model = model
    except AttributeError:
        pass
    return result


def note_rate_limited(base_url: str, model: str) -> None:
    """One observed 429: start its cooldown, and count it for the day.

    Called at every point a 429 is actually SEEN, and nowhere else. It used to
    be called again at the top of the next attempt, for the model that had
    failed on the previous one -- harmless for a cooldown, which is just a
    timestamp being overwritten, but it double-counts a counter. The refusal
    count is the one honest number this app has about a free tier, so it is
    worth not inflating.
    """
    with _rate_limit_lock:
        _rate_limited_at[(base_url, model)] = time.monotonic()
    try:
        from . import usage as _usage
        _usage.record_limited(model)
    except Exception:
        pass


def is_cooling_down(base_url: str, model: str) -> bool:
    with _rate_limit_lock:
        at = _rate_limited_at.get((base_url, model))
    return at is not None and (time.monotonic() - at) < MODEL_COOLDOWN


def clear_cooldowns() -> None:
    with _rate_limit_lock:
        _rate_limited_at.clear()


def plan_models(base_url: str, model: str, fallbacks) -> list[str]:
    """The order to try, preferred first, minus anything still cooling down.

    A model that was rate-limited a moment ago is put at the BACK rather than
    dropped: if every model in the chain is cooling down, trying the preferred
    one and waiting beats refusing outright.
    """
    chain = [model] + [m for m in (fallbacks or [])
                       if m and m != model]
    ready = [m for m in chain if not is_cooling_down(base_url, m)]
    resting = [m for m in chain if is_cooling_down(base_url, m)]
    return ready + resting


class RateLimiter:
    """Spaces out calls across threads so parallel sub-agents don't all hit
    the free tier's ~1 req/s limit at the same moment -- which just burns
    their own retry budget and tool-calling step budget on 429 backoff
    instead of real task progress. Share one instance across every
    ZaiClient spawned for a single spawn_agents call; unused (no-op) for
    the single-threaded main agent, which never contends with itself."""

    def __init__(self, min_interval: float = 1.05):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


_RETRY_IN_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.I)


def _retry_hint(resp, msg: str) -> float | None:
    """How long the server asked us to wait, if it said.

    Rate limits come with a real number and this client was ignoring it: a 429
    saying "Please retry in 15.5s" was answered with a 1.8s backoff, so several
    attempts were burnt re-asking a question the server had already answered.
    The Retry-After header is the standard place; Google puts it in the message
    text instead, so both are read.
    """
    try:
        header = (resp.headers or {}).get("Retry-After")
    except Exception:
        header = None
    if header:
        try:
            return max(0.0, float(str(header).strip()))
        except ValueError:
            pass            # HTTP-date form: fall through to the message
    m = _RETRY_IN_RE.search(msg or "")
    return float(m.group(1)) if m else None


def _raise_if_cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


class ZaiClient:
    def __init__(self, api_key: str, base_url: str, rate_limiter: Optional[RateLimiter] = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Worked out once, from the endpoint. `thinking` is a Zhipu field, not
        # an OpenAI one, and this client stopped being z.ai-only some time ago
        # -- the name is all that is left of that.
        from . import providers as _providers
        from . import skoolie as _skoolie
        if _skoolie.owns(self.base_url):
            _skoolie.ensure_server()
        self.supports_thinking = _providers.supports(self.base_url, "thinking")
        self.supports_thought_signature = _providers.supports(
            self.base_url, "thought_signature")
        self.rate_limiter = rate_limiter
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })

    # ------------------------------------------------------------------ #

    def chat(
        self,
        model: str,
        messages: list,
        tools: Optional[list] = None,
        temperature: float = 0.6,
        max_tokens: int = 8192,
        thinking: bool = True,
        on_content: Optional[Callable[[str], None]] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        cancel=None,
        fallbacks: Optional[list] = None,
    ) -> ChatResult:
        """Send a chat completion request, streaming. Returns the final result.

        `fallbacks` are models on THIS endpoint to fall back to when the
        preferred one is rate-limited -- see plan_models. They are walked only
        on a 429; every other failure retries the same model, because a weaker
        model is not the answer to a bad request or a server fault.
        """
        chain = plan_models(self.base_url, model, fallbacks)
        model = chain[0]
        payload: dict = {
            "model": model,
            "messages": self._portable(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # Only where the endpoint is known to understand it. Sent to Google it
        # is not ignored -- the request is rejected outright with
        #   400 Unknown name "thinking": Cannot find field.
        # so every single turn failed, over a field nobody asked for.
        if thinking and self.supports_thinking:
            payload["thinking"] = {"type": "enabled"}

        last_err: Optional[Exception] = None
        tried = 1                      # how far down the chain we have gone
        for attempt in range(MAX_RETRIES):
            if attempt > 0:
                # Rate-limited and there is another model to ask? Ask it,
                # instead of sitting out the backoff. That is the entire point
                # of a chain: the wait is what it exists to avoid.
                if (isinstance(last_err, ApiError) and last_err.status == 429
                        and tried < len(chain)):
                    # Whoever saw that 429 already noted it -- both the outer
                    # path below and the inner one at the end of this branch.
                    payload["model"] = model = chain[tried]
                    tried += 1
                    if on_status:
                        on_status(f"rate limited -- switching to {model}")
                    if self.rate_limiter:
                        self.rate_limiter.wait()
                    _raise_if_cancelled(cancel)
                    try:
                        from . import usage as _usage
                        _usage.record(model)
                    except Exception:
                        pass
                    try:
                        return _stamp(self._stream_once(payload, on_content,
                                                        on_reasoning, cancel),
                                      model)
                    except ApiError as e:
                        if e.status in RETRYABLE:
                            if e.status == 429:
                                note_rate_limited(self.base_url, payload["model"])
                            last_err = e
                            continue
                        raise
                    except (requests.ConnectionError, requests.Timeout) as e:
                        last_err = e
                        continue
                base = min(2 ** attempt, 30)
                if isinstance(last_err, ApiError) and last_err.status == 429:
                    base = max(base, 2)
                asked = getattr(last_err, "retry_after", None)
                # Full jitter (sleep a random amount in [0, base]) rather than
                # a fixed backoff: several parallel sub-agents that hit the
                # same 429 at the same instant would otherwise all wait the
                # SAME amount and retry in lockstep -- colliding again and
                # burning their retry budget on the same synchronized spike.
                # Randomizing spreads their retries out so they stop fighting.
                wait = round(random.uniform(base / 2, base), 1)
                if asked is not None:
                    # The server's own number wins over a guess -- retrying
                    # sooner than it asked just spends an attempt to be told
                    # the same thing. Capped so a provider cannot park a turn
                    # for an unbounded time, and never shortened.
                    wait = max(wait, min(float(asked) + 0.5, MAX_RETRY_WAIT))
                if on_status:
                    on_status(f"retrying in {wait}s ({last_err})")
                self._sleep_unless_cancelled(wait, cancel)
            if self.rate_limiter:
                self.rate_limiter.wait()
            # The last gate before a request goes out, and the only one that
            # used to be missing: the cancel check lived inside the loop
            # reading a response body, which is not running while the client is
            # asleep between attempts -- exactly where a 429 leaves it. So Stop
            # during a rate-limit backoff did nothing and the client carried on
            # hammering the API. Placed after the rate limiter because that
            # blocks too, and both waits can run for tens of seconds.
            _raise_if_cancelled(cancel)
            # Counted here rather than at a call site: this is the one place a
            # request actually goes out, so sub-agents, retries and the vision
            # model are all included without anything having to remember to.
            try:
                from . import usage as _usage
                _usage.record(model)
            except Exception:
                pass
            try:
                return _stamp(
                    self._stream_once(payload, on_content, on_reasoning, cancel),
                    payload["model"])
            except ApiError as e:
                if e.status in RETRYABLE:
                    if e.status == 429:
                        note_rate_limited(self.base_url, payload["model"])
                    last_err = e
                    continue
                raise
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                continue
        raise ApiError(0, f"gave up after {MAX_RETRIES} attempts: {last_err}")

    # ------------------------------------------------------------------ #

    @staticmethod
    def _sleep_unless_cancelled(seconds: float, cancel) -> None:
        """Wait, but stay interruptible.

        A backoff can be 30 seconds, and a single time.sleep through it means
        Stop is not noticed until it ends -- by which point another request has
        usually gone out. Waiting on the Event itself is what makes the button
        take effect immediately rather than eventually.
        """
        if cancel is None:
            time.sleep(seconds)
            return
        if cancel.wait(seconds):    # returns True the moment it is set
            raise Cancelled()

    def _portable(self, messages: list) -> list:
        """History minus anything this endpoint will not recognise.

        A chat can change provider mid-conversation, and its history then still
        carries whatever the previous one attached -- Gemini's
        extra_content.google.thought_signature being the case in hand. Sending
        that on is the same mistake as sending z.ai's `thinking` field to
        Google: a strict validator rejects the entire request over a field
        nobody asked for.

        The history itself is left untouched, so switching back to the provider
        that issued the signatures keeps them intact. Only the copy on the wire
        is trimmed, and only where there is something to trim.
        """
        if self.supports_thought_signature:
            return messages
        # The overwhelmingly common case is a history with nothing to trim, and
        # it is walked on every request of every turn -- so check first and
        # hand back the original list rather than rebuilding it each time.
        if not any(isinstance(m, dict) and any(
                c.get("extra_content") for c in (m.get("tool_calls") or []))
                for m in messages):
            return messages
        out = []
        for m in messages:
            calls = m.get("tool_calls") if isinstance(m, dict) else None
            if not calls or not any(c.get("extra_content") for c in calls):
                out.append(m)
                continue
            m = dict(m)
            m["tool_calls"] = [{k: v for k, v in c.items() if k != "extra_content"}
                               for c in calls]
            out.append(m)
        return out

    def _stream_once(self, payload, on_content, on_reasoning, cancel=None) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        result = ChatResult()
        tool_calls: dict[int, dict] = {}

        with self.session.post(url, json=payload, stream=True, timeout=(15, 300)) as resp:
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    msg = body.get("error", {}).get("message") or json.dumps(body)[:500]
                except Exception:
                    msg = resp.text[:500]
                raise ApiError(resp.status_code, msg,
                               retry_after=_retry_hint(resp, msg))

            # Server-sent events are UTF-8 BY DEFINITION (WHATWG), and the
            # JSON inside them is UTF-8 by RFC 8259. requests does not know
            # that: with decode_unicode=True it decodes using resp.encoding,
            # and for a text/* content type carrying no charset it falls back
            # to ISO-8859-1 (RFC 2616). Providers that send a bare
            # "text/event-stream" -- Google is one -- therefore had every
            # non-ASCII character mangled on the way in: "Har ar en jamforelse"
            # arrived as the Latin-1 reading of its own UTF-8 bytes, and that
            # is what got stored in the chat and sent back on the next turn.
            #
            # Forcing it is not a guess about this provider; it is what the two
            # specifications covering this stream both say it is.
            resp.encoding = "utf-8"
            for raw in resp.iter_lines(decode_unicode=True):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    result.usage = Usage(
                        u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
                    )
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    result.finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                reasoning = delta.get("reasoning_content")
                if reasoning:
                    result.reasoning += reasoning
                    if on_reasoning:
                        on_reasoning(reasoning)

                content = delta.get("content")
                if content:
                    result.content += content
                    if on_content:
                        on_content(content)

                for tc in delta.get("tool_calls") or []:
                    # WHICH call this delta belongs to. `index` is the OpenAI
                    # way and z.ai sends it; Google's compatibility layer omits
                    # it for parallel calls, so defaulting to 0 dropped three
                    # simultaneous calls into one slot and concatenated their
                    # arguments into
                    #   {"path":"a.png"}{"path":"b.png"}{"path":"c.png"}
                    # -- one unparseable blob where three calls should have
                    # been, and then a 400 on the next request because that
                    # assistant message is malformed.
                    #
                    # An id identifies a call outright, so it wins when there
                    # is one: a new id is a new call whatever the index says.
                    # A delta with neither (a pure continuation) extends the
                    # call most recently opened.
                    tcid = tc.get("id") or ""
                    if tcid:
                        idx = next((k for k, v in tool_calls.items()
                                    if v["id"] == tcid), None)
                        if idx is None:
                            idx = tc.get("index")
                            if idx is None or idx in tool_calls:
                                # A free slot, not len(): the keys are whatever
                                # the provider numbered them, so they can be
                                # sparse and len() can land on a taken one.
                                idx = next(k for k in itertools.count()
                                           if k not in tool_calls)
                    else:
                        idx = tc.get("index")
                        if idx is None:
                            idx = max(tool_calls) if tool_calls else 0
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tcid:
                        slot["id"] = tcid
                    if tc.get("type"):
                        slot["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                    # Anything the provider hung off the tool call, kept as-is.
                    # Gemini 3 puts a thought_signature here --
                    # extra_content.google.thought_signature -- an encrypted
                    # record of the reasoning behind the call, and it REQUIRES
                    # it back on every following request:
                    #   400 Function call is missing a thought_signature
                    # Rebuilding the call from id/name/arguments dropped it, so
                    # tool use died on the second or third step of every task.
                    if tc.get("extra_content"):
                        slot.setdefault("extra_content", {}).update(
                            tc["extra_content"])

        result.tool_calls = [tool_calls[i] for i in sorted(tool_calls)]
        for i, tc in enumerate(result.tool_calls):
            if not tc["id"]:
                tc["id"] = f"call_{int(time.time() * 1000)}_{i}"
        return result

    # ------------------------------------------------------------------ #
    # Vision

    def analyze_images(
        self,
        vision_model: str,
        prompt: str,
        image_paths: list[Path],
        max_tokens: int = 4096,
        on_content: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Ask the vision model about local image files. Returns its text answer."""
        content: list = []
        for p in image_paths:
            content.append({
                "type": "image_url",
                "image_url": {"url": encode_image_data_uri(p)},
            })
        content.append({"type": "text", "text": prompt})
        result = self.chat(
            model=vision_model,
            messages=[{"role": "user", "content": content}],
            temperature=0.3,
            max_tokens=max_tokens,
            thinking=False,
            on_content=on_content,
        )
        return result.content.strip()


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def encode_image_data_uri(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{path.name} is {len(data) // 1024 // 1024}MB; images must be under 8MB"
        )
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


DEFAULT_CHARS_PER_TOKEN = 3.6   # starting guess, replaced by a real measurement
_RATIO_MIN, _RATIO_MAX = 1.5, 8.0   # readings outside this can't be right


def message_chars(messages: list) -> int:
    """Characters the model will see, images counted at a flat cost."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    chars += len(part.get("text", ""))
                else:
                    chars += 4000  # images cost roughly ~1k tokens
        for tc in m.get("tool_calls") or []:
            chars += len(json.dumps(tc))
    return chars


def estimate_tokens(messages: list, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Token estimate for context management.

    Dividing characters by a constant is a guess, and it's the reason the
    context limit has to sit well under the model's real window. Pass a ratio
    measured by calibrate_ratio() and the guess goes away."""
    r = chars_per_token if chars_per_token and chars_per_token > 0 else DEFAULT_CHARS_PER_TOKEN
    return int(message_chars(messages) / r)


def calibrate_ratio(messages: list, prompt_tokens: int) -> float | None:
    """Chars-per-token derived from a request the API actually priced.

    The response reports the exact prompt_tokens for what we sent, so the
    divisor can be measured instead of assumed -- and it adapts to the content,
    since dense code tokenises very differently from prose. Returns None for
    implausible readings so one odd response can't skew the meter."""
    if not prompt_tokens or prompt_tokens <= 0:
        return None
    chars = message_chars(messages)
    if chars <= 0:
        return None
    ratio = chars / prompt_tokens
    if not (_RATIO_MIN <= ratio <= _RATIO_MAX):
        return None
    return ratio

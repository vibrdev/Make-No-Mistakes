"""Make No Mistakes desktop app: pywebview window + JS bridge around the agent core."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import webview

from .. import __version__
from ..agent import Agent
from ..api import IMAGE_EXTENSIONS, ZaiClient
from .. import api as api_mod
from ..backup import BackupRepo
from .. import backup as backup_module
from .. import providers as providers_mod
from ..config import (BUILTIN_PROVIDER_NAME, CONFIG_DIR, PERMISSION_MODES, Config,
                      builtin_provider_name,
                      all_providers, find_provider, load_config, save_config,
                      default_provider, vision_target, normalize_provider,
                      default_model as cfg_default_model,
                      provider_key as cfg_provider_key)
from .. import githubsync
from .. import secretstore
from .. import live
# Re-exported: the pairing tests monkeypatch these through gui.app, which is
# where they were reached from before the seams were cut.
from .. import pairing                               # noqa: F401
from .. import qrcode_util                           # noqa: F401
from .. import syncstore
from .. import usage as usage_mod
from ..notify import APP_NAME, notify
from ..prompts import EXECUTE_PLAN_MESSAGE, PLAN_MODE_PREAMBLE, TITLE_PROMPT
from ..sessions import SessionStore, new_id, to_display
from .checkup_api import CheckupApi
from .devices_api import DeviceApi
# Re-exported, not merely used: the event sink and these two helpers were
# defined here until the Api class started coming apart, and `gui.app.X` is
# how the tests and the rest of the package have always reached them.
from .events import WebEvents, _TtsFeeder            # noqa: F401
from .github_api import GitHubApi
from .media import _data_uri, _thumb_uri              # noqa: F401
from .paths import (WEB_DIR, DEFAULT_BG, WHITEBOARD_DIR,   # noqa: F401
                    EXTENSION_DIR)
from .speech import _tts_engine_voice                 # noqa: F401
from .voice_api import VoiceApi
from ..transcript import Transcript, search_sessions
from ..tools import (configure_search,
                     resolve_mentions as tools_resolve_mentions,
                     build_text_file_context as tools_build_text_file_context,
                     search_project_files as tools_search_project_files)
from ..permissions import add_command_aliases


# --------------------------------------------------------------------- #

_PATH_RULE_ACTIONS = ("allow", "ask", "deny")


def _normalize_path_rules(value) -> list:
    """Clean scoped-autonomy rules coming from the UI: keep only entries with a
    non-empty glob and a valid action, de-duplicate, and cap the list."""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            continue
        glob = str(item.get("glob", "")).strip()[:200]
        action = str(item.get("action", "")).strip().lower()
        if not glob or action not in _PATH_RULE_ACTIONS:
            continue
        key = (glob, action)
        if key in seen:
            continue
        seen.add(key)
        out.append({"glob": glob, "action": action})
        if len(out) >= 100:
            break
    return out


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _normalize_connect_url(value) -> tuple[str, str]:
    """Clean a DevTools endpoint for "attach to the browser I already have open".

    Returns (url, error). An empty url with no error means the feature is off,
    which is the default and the safe state.

    The host check is not a security boundary -- the debugging port is
    unauthenticated, so anything that can already reach it has already won.
    It is about what a typo can do. This setting points the agent at a browser
    someone is signed into, and a stray hostname aiming it at a machine that is
    not this one is a mistake worth refusing rather than obeying.
    """
    url = str(value or "").strip()
    if not url:
        return "", ""
    if any(c.isspace() for c in url):
        return "", f"{url!r} isn't an address — try http://localhost:9222"
    if "://" not in url:
        url = "http://" + url
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return "", f"{str(value).strip()!r} isn't an address — try http://localhost:9222"
    if u.scheme not in ("http", "https"):
        return "", "The DevTools endpoint is an http:// address, e.g. http://localhost:9222"
    if not host:
        return "", "That address has no host — try http://localhost:9222"
    if host not in _LOCAL_HOSTS:
        return "", (f"'{host}' isn't this machine. The browser being attached to has "
                    "to be the one open in front of you, so the endpoint must be "
                    "localhost.")
    if not port:
        return "", ("Include the debugging port the browser was started with, "
                    "e.g. http://localhost:9222")
    netloc = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{netloc}:{port}", ""


def persist_env_var(name: str, value: str) -> bool:
    """Persist an env var to the user's environment (Windows `setx`). Best
    effort: the value is ALWAYS set for the current process first, so the app
    works this run no matter what. Persistence is a bonus that must never
    raise -- a locked-down machine (school/corporate Group Policy) can block
    or HANG setx, and a TimeoutExpired here used to kill onboarding entirely."""
    from ..tools import NO_WINDOW_KWARGS
    os.environ[name] = value  # active this run regardless of persistence
    if sys.platform != "win32":
        return False          # setx is Windows-only; not a failure elsewhere
    try:
        r = subprocess.run(["setx", name, value], capture_output=True, timeout=8,
                           **NO_WINDOW_KWARGS)
        return r.returncode == 0
    except Exception:
        # setx missing, blocked by policy, or hung past the timeout -- fine,
        # the key still works this session via os.environ above.
        return False


# --------------------------------------------------------------------- #

class ChatState:
    """Everything one open chat owns: its live agent (which may be mid-turn
    on a background thread), its event sink, and its per-chat settings."""

    def __init__(self, sid: str, agent: Agent, events: WebEvents):
        self.sid = sid
        self.agent = agent
        self.events = events
        self.backup_repo: BackupRepo | None = None
        self.title = ""
        self.provider = ""
        self.model = ""
        self.auto_backup = True
        self.turn_lock = threading.Lock()  # one turn at a time PER CHAT
        # chat["updated"] of the synced copy this chat last wrote or adopted, so
        # a catch-up can tell "the phone moved this on" from "that's my own push
        # coming back".
        self.synced_at = 0
        # One entry per send-turn, in order: {"commit": <hash or None>}. The
        # list index IS the turn ordinal, so turn_snapshots[k] is the pre-turn
        # file state of the k-th user turn -- what "edit & resend" reverts to.
        # Cleared on compaction (older turns' messages no longer exist).
        self.turn_snapshots: list[dict] = []
        # Speech-to-speech voice mode: a separate, persistent conversational
        # agent (pure delegator -- see Agent(conversational=True)) that shares
        # this chat's project/backup/mcp so its background workers act on the
        # real code. It streams through its OWN events (sid "<sid>::voice") so
        # the voice overlay is cleanly separate from the coding transcript.
        # All lazily created on first use.
        self.convo_agent: Agent | None = None
        self.convo_events: WebEvents | None = None
        self.convo_lock = threading.Lock()  # one voice turn at a time
        # Final reports from workers dispatched by voice, waiting to be handed
        # to the CODING agent. Queued rather than appended straight to
        # agent.messages because a worker finishes on its own thread and the
        # coding agent may be mid-turn -- mutating its history underneath it is
        # how you get a tool_call with no matching reply. Drained at the top of
        # the next send turn, which holds the turn lock.
        self.worker_reports: list[str] = []
        self.worker_reports_lock = threading.Lock()
        # Spoken exchanges waiting to join the CODING agent's history, so that
        # typing after talking continues the same conversation. Queued for the
        # same reason worker reports are: a voice turn finishes on the
        # delegator's thread, and the coding agent may be mid-turn.
        self.voice_turns: list[dict] = []
        self.voice_turns_lock = threading.Lock()


class Api(CheckupApi, DeviceApi, GitHubApi, VoiceApi):
    """Methods callable from JS via window.pywebview.api.*"""

    def __init__(self):
        self._cfg: Config = load_config()
        # Shared across every chat's WebEvents so permission_response can
        # resolve a prompt no matter which chat asked.
        self._perm_registry: dict = {}
        # Sid-less sink for app-level notices before/outside any chat.
        self._events_global = WebEvents("", self._perm_registry)
        self._events_global._cfg = self._cfg
        # Every open chat, live agent included -- chats keep running in the
        # background when the user switches away (see send/_run_send_turn).
        self._chats: dict[str, ChatState] = {}
        # Underscore-prefixed: see the comment on WebEvents._window above —
        # this class is the js_api object pywebview recursively introspects,
        # so a public `window` attribute here triggers the same infinite
        # AccessibilityObject.Bounds.Empty recursion and freezes the app.
        self._window: webview.Window | None = None
        self._store = SessionStore()
        self.session_id: str | None = None
        self._client: ZaiClient | None = None
        # Updated by JS on window focus/blur; gates OS-level toasts (they
        # only fire while the user is away in another app -- the in-app UI
        # already covers the focused case).
        self._window_focused = True
        # sid -> heartbeat stop-event, for chats this device currently holds
        # the cross-device sync lock on (see _try_acquire_device_lock).
        self._chat_locks: dict[str, threading.Event] = {}

        configure_search(self._cfg.search_provider, self._cfg.resolve_tavily_key())
        # MCP servers: spawned in the background so a slow `npx` download
        # never delays app startup; agents pick the tools up per model call.
        from ..mcp import McpManager
        self._mcp = McpManager(self._cfg)
        if self._cfg.mcp_servers:
            self._mcp.start_all_async()
        # Initialize command aliases for npm/yarn/pnpm/git
        add_command_aliases({
            "npm": "npm",
            "yarn": "npm",
            "pnpm": "npm",
            "git": "git",
        })
        # Scheduled/watched tasks: a lightweight poller fires the ones that are
        # due. Daemon thread, started once; does nothing until the user creates
        # a task (so there's no cost/behavior unless opted in).
        self._sched_stop = threading.Event()
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        # Codebase memory: honor the neural-search setting from the start.
        from .. import codebase_memory
        codebase_memory.set_neural_enabled(self._cfg.codebase_memory_neural)

    # -- active-chat accessors ------------------------------------------- #
    # Most of this class predates parallel chats and talks about THE agent/
    # events/title; these map that vocabulary onto whichever chat is active.

    @property
    def _active(self) -> "ChatState | None":
        return self._chats.get(self.session_id) if self.session_id else None

    @property
    def _agent(self) -> Agent | None:
        c = self._active
        return c.agent if c else None

    @property
    def _events(self) -> WebEvents:
        c = self._active
        return c.events if c else self._events_global

    @property
    def _backup_repo(self) -> BackupRepo | None:
        c = self._active
        return c.backup_repo if c else None

    @_backup_repo.setter
    def _backup_repo(self, value) -> None:
        if self._active:
            self._active.backup_repo = value

    @property
    def session_title(self) -> str:
        c = self._active
        return c.title if c else ""

    @session_title.setter
    def session_title(self, value: str) -> None:
        if self._active:
            self._active.title = value

    @property
    def auto_backup(self) -> bool:
        c = self._active
        return c.auto_backup if c else True

    @auto_backup.setter
    def auto_backup(self, value: bool) -> None:
        if self._active:
            self._active.auto_backup = bool(value)

    @property
    def session_provider(self) -> str:
        c = self._active
        return c.provider if c else ""

    @session_provider.setter
    def session_provider(self, value: str) -> None:
        if self._active:
            self._active.provider = value

    @property
    def session_model(self) -> str:
        c = self._active
        return c.model if c else ""

    @session_model.setter
    def session_model(self, value: str) -> None:
        if self._active:
            self._active.model = value

    def _ensure_client(self) -> ZaiClient | None:
        """A client for the API new chats start on."""
        prov = default_provider(self._cfg)
        if prov is None:
            return None
        key = cfg_provider_key(prov)
        # A local server has no key and does not want one; a hosted one without
        # a key cannot answer, and a client built now would fail on every turn.
        if not key and not providers_mod.is_local(prov["base_url"]):
            return None
        if self._client is None:
            self._client = ZaiClient(key or "local", prov["base_url"])
        return self._client

    def _make_events(self, sid: str) -> WebEvents:
        ev = WebEvents(sid, self._perm_registry)
        ev._cfg = self._cfg
        ev._window = self._window
        ev.notifier = lambda body, _sid=sid: self._os_attention(_sid, body)
        # Compaction rewrites history (older turns' messages are replaced by a
        # summary), so their pre-turn snapshot commits no longer line up with
        # any turn -- drop them so "edit & resend" can't revert to a stale one.
        ev.on_compacted = lambda _sid=sid: self._on_compacted(_sid)
        return ev

    def _on_compacted(self, sid: str) -> None:
        cs = self._chats.get(sid)
        if cs:
            cs.turn_snapshots = []

    def _os_attention(self, sid: str, body: str) -> None:
        """OS-level toast for 'this chat needs you': a blocking permission
        prompt, or a finished turn waiting on the user. Titled with the
        chat's name so parallel chats are tellable apart."""
        if self._window_focused or not self._cfg.notifications:
            return
        cs = self._chats.get(sid)
        notify(cs.title if cs and cs.title else APP_NAME, body)

    def set_window_focus(self, focused):
        self._window_focused = bool(focused)
        return {"ok": True}

    # -- lifecycle ------------------------------------------------------- #

    def log(self, msg: str):
        """Let the page drop breadcrumbs into the startup log (see _startup_log).
        Lets us tell a native WebView2 hang (no JS ever runs) apart from a hang
        inside boot() (JS logged 'boot:start' but never 'boot:done')."""
        _startup_log(f"[js] {msg}")
        return {"ok": True}

    def boot(self):
        _startup_log("[py] boot() called")
        # Open the extension's port NOW if the setting is on. It used to be
        # opened lazily, the first time Settings -> Browser was rendered, which
        # meant a normal launch -- app starts, setting already on, nobody opens
        # Settings -- left nothing for the browser to connect to. The extension
        # would sit there unable to reach anything and control_chrome would
        # quietly launch a separate browser instead.
        # Not swallowed silently. "Nothing was listening" is the one failure
        # this feature has that the user cannot see from the outside -- the
        # extension just reports connection refused, in a console they would
        # have to go looking for.
        try:
            from .. import browser_extension
            if browser_extension.enabled(self._cfg):
                b = browser_extension.bridge(start=True)
                _startup_log(f"[py] extension port: {b.port}" if b
                             else "[py] extension port: NONE — every port busy")
        except Exception as e:                             # never blocks boot
            _startup_log(f"[py] extension port failed: {type(e).__name__}: {e}")
        has_key = self._ensure_client() is not None
        # Setup is shown when this install has not been through it -- not
        # merely when no key can be found. The key is persisted with `setx`,
        # into the user's registry environment, so it outlives deleting
        # ~/.makenomistakes AND deleting the app: the check used to find that
        # leftover, decide the app was configured, and skip straight past
        # setup. Reinstalling from scratch is what everyone tries first when
        # something is wrong, and it was the one repair this app ignored.
        needs_setup = (not self._cfg.setup_done) or (not has_key)
        result = {
            "version": __version__,
            "needsKey": needs_setup,
            "background": self.get_background(),
            "settings": self._settings(),
            "sessions": self.list_sessions(),
            "session": None,
            "contextLimit": self._cfg.context_limit_tokens,
        }
        if has_key and not needs_setup:
            # Reopening the last chat is a convenience, and a convenience must
            # not be able to stop the app starting. It could: an exception here
            # propagated out of boot(), so the page never received its settings
            # or its session list and sat there dead, with the only evidence in
            # a terminal most people never see. Whatever went wrong with one
            # stored chat, the window still comes up usable.
            try:
                result["session"] = self._resume_last()
                result["sessions"] = self.list_sessions()
            except Exception as e:
                _startup_log(f"[py] resume failed, starting empty: {e!r}")
                self.session_id = None
                result["session"] = None
        _startup_log("[py] boot() returning")
        return result

    def _resume_last(self):
        """Reopen the last active session on launch, if any still exists."""
        sid = self._cfg.last_session_id
        data = self._store.load(sid) if sid else None
        if data is None:
            sessions = self.list_sessions()
            if sessions:
                sid = sessions[0]["id"]
                data = self._store.load(sid)
        if data is None:
            # `_agent` is a read-only property derived from the active chat, so
            # assigning it raised AttributeError and took the whole of boot()
            # down with it -- leaving the window up with no settings, no
            # sessions and no setup screen. Clearing session_id is the whole
            # job: with no active chat, `_agent` already answers None.
            #
            # Only reachable when there is nothing to resume, which on a fresh
            # install is every launch. It survived because it needs a first run
            # that also skips setup, and setup was skipped exactly when a key
            # was left behind in the environment by a previous install.
            self.session_id = None
            return None
        return self._activate_session(
            sid, data.get("messages", []), data.get("cwd", ""),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("todos", []), data.get("title", ""),
        )

    def provider_choices(self):
        """Everything the setup screen needs to draw itself.

        Served from the catalogue rather than written into the HTML, so the
        desktop and the phone offer the same options in the same words. They
        are separate programs and this is the only thing keeping them in step.
        """
        return {"choices": providers_mod.choices(),
                "chosen": self._cfg.provider_preset or "",
                "found": self._keys_already_on_this_pc()}

    @staticmethod
    def _keys_already_on_this_pc() -> list:
        """Which presets already have a key in the environment.

        Reinstalling does not clear these -- they are user environment
        variables in the registry -- so someone arriving at setup after a
        reinstall very often has a perfectly good key sitting right there.
        Making them go and find it again is work the app can do for them, so
        setup offers to reuse it.

        Only ever the variable's NAME and whether it is set. The value is not
        sent to the page: it is already in the process, nothing on screen needs
        it, and a key that never reaches the DOM cannot end up in a screenshot.
        """
        out = []
        for key in providers_mod.preset_keys() + [providers_mod.CUSTOM_KEY]:
            var = providers_mod.env_var_for(key)
            if var and os.environ.get(var, "").strip():
                out.append({"preset": key, "env_var": var})
        return out

    @staticmethod
    def local_models(preset: str = "ollama"):
        """What is installed on the local server right now.

        Setup asks this instead of asking the user. Which models exist depends
        on what has been pulled onto this machine, so there is no list that
        could live in the catalogue -- and making someone type a model name
        exactly as Ollama spells it is precisely the work worth removing. The
        three outcomes are different enough to be worth telling apart:
        not running, running but empty, and ready.
        """
        import requests as _requests
        p = providers_mod.preset(preset)
        if not p or not providers_mod.is_local(p["base_url"]):
            return {"error": "not a local provider"}
        from .. import skoolie as _skoolie
        if _skoolie.owns(p["base_url"]):
            # Not an Ollama server: there is nothing installed to list, and
            # asking /api/tags would say "not running" about a bridge that
            # only starts when a client is built.
            return {"running": True, "models": [_skoolie.MODEL]}
        host = p["base_url"].rsplit("/v1", 1)[0]
        try:
            # Short: this runs while someone is looking at the setup screen,
            # and "no server" is the common answer, not an exceptional one.
            r = _requests.get(f"{host}/api/tags", timeout=1.5)
            models = [m["name"] for m in (r.json().get("models") or [])]
        except Exception:
            return {"running": False, "models": []}
        return {"running": True, "models": sorted(models)}

    @staticmethod
    def _fetch_models(base_url: str, api_key: str) -> list:
        """Ask an OpenAI-compatible endpoint what it serves. [] if it won't say.

        `GET /models` is part of the OpenAI surface and every provider here
        implements it. Worth the round trip because a hardcoded model name has
        a shelf life: gemini-2.5-flash was the documented default and then
        started answering "no longer available to new users" -- a dead default
        that no amount of care in this file could have anticipated.

        Never raises. A provider that does not implement it, or a machine with
        no network right now, falls back to the catalogue rather than ending up
        with no models at all.
        """
        import requests as _requests
        url = f"{(base_url or '').rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            r = _requests.get(url, headers=headers, timeout=6)
            if r.status_code != 200:
                return []
            data = r.json().get("data") or []
            # Google returns "models/gemini-x"; OpenAI returns bare ids.
            names = [str(m.get("id", "")).split("/")[-1] for m in data]
        except Exception:
            return []
        return sorted({n for n in names if n and providers_mod.is_chat_model(n)})

    def refresh_models(self, provider: str = ""):
        """Re-read an API's model list from the API itself.

        Named rather than assumed. It used to refresh "the primary provider",
        which meant the button in the model menu could only ever update one row
        however many were configured -- and refreshed a row you might not even
        be using.
        """
        row = self._find_row(provider) if provider else None
        if row is None:
            row = self._find_row((default_provider(self._cfg) or {}).get("name", ""))
        if row is None:
            return {"error": "no API configured to ask"}
        entry = normalize_provider(row)
        models = self._fetch_models(entry["base_url"], cfg_provider_key(entry))
        if not models:
            return {"error": f'couldn\'t reach {entry["name"]} to list its models'}
        row["all_models"] = models
        row["models"] = providers_mod.shortlist(models)
        if entry["base_url"] == (self._cfg.base_url or "").rstrip("/"):
            self._cfg.available_models = models
        # The default model may have been retired out from under us, which is
        # the whole reason for asking. Move to something that exists rather
        # than leaving a default that 404s on the next message.
        if self._cfg.default_provider == row.get("name") and \
                self._cfg.model not in models:
            self._cfg.model = providers_mod.preferred_model(models, entry["base_url"])
        if self._cfg.vision_provider == row.get("name") and \
                self._cfg.vision_model not in models:
            self._cfg.vision_provider = ""     # back to automatic
            self._cfg.vision_model = ""
        try:
            save_config(self._cfg)
        except Exception:
            pass
        self._client = None
        return self.providers()

    def save_setup(self, preset: str, api_key: str, base_url: str = "",
                   model: str = ""):
        """First run: record which provider was picked, and store its key.

        Replaces the old save_api_key, which could only ever mean z.ai. That one
        stays for the moment because an older window may still call it.
        """
        preset = (preset or "").strip()
        api_key = (api_key or "").strip()
        known = providers_mod.preset(preset)
        if known:
            base_url = known["base_url"]
            # A preset normally names its model. Ollama cannot: which models
            # exist depends on what has been pulled here, so the page sends
            # back one read off the running server.
            model = known["model"] or (model or "").strip()
            if not model:
                return {"error": f"choose a {known['label']} model"}
            vision = known["vision_model"] or model
        else:
            # "Other": the endpoint and model are the whole point, so they are
            # required here in a way a preset's never are.
            preset = providers_mod.CUSTOM_KEY
            base_url = (base_url or "").strip().rstrip("/")
            model = (model or "").strip()
            if not base_url:
                return {"error": "paste the API's base URL"}
            if not model:
                return {"error": "type the model name"}
            vision = model
        # A local server (Ollama, LM Studio) genuinely has no key, so an empty
        # one is only refused where it cannot work -- and not even then if this
        # PC already has one for the chosen provider. Reinstalling leaves the
        # environment variable behind, so refusing an empty box would send
        # someone to the registry to copy out a key the app can already read.
        env_var = providers_mod.env_var_for(preset)
        reused = bool(env_var and os.environ.get(env_var, "").strip())
        needs_key = known.get("needs_key", True) if known else False
        if needs_key and not api_key and not reused:
            return {"error": f"paste your {known['label']} API key"}
        self._cfg.provider_preset = preset
        # Set here and nowhere else: getting to the end of this method is the
        # only thing that means "this install has been set up".
        self._cfg.setup_done = True
        self._cfg.base_url = base_url
        self._cfg.model = model
        # NOT set from the preset. `vision` is what setup would have written,
        # and writing it made an automatic answer look like a decision -- one
        # that then followed every chat to every other API. Left empty so it
        # resolves per chat, unless someone chooses otherwise in Settings.
        self._cfg.vision_model = ""
        self._cfg.vision_provider = ""
        persisted = False
        if not needs_key and not api_key:
            # A keyless provider must not inherit the previous one's key.
            # cfg.api_key is the "setx was blocked" fallback and would still be
            # holding a hosted key from an earlier setup -- which resolve_api_key
            # would then hand to a server on this machine. A placeholder, since
            # the request still carries an Authorization header and local
            # servers ignore what is in it.
            self._cfg.api_key = "local"
        elif api_key:
            try:
                persisted = persist_env_var(self._cfg.provider_env_var(), api_key)
            except Exception:
                persisted = False
            self._cfg.api_key = api_key   # fallback if the env write failed
        # Ask the provider what it serves, now that there is a key to ask with.
        # The catalogue's model is a preference; this is the only thing that
        # knows whether it still exists. Best-effort, like everything else
        # here: setup must finish even on a machine with no network yet.
        try:
            live = self._fetch_models(base_url, self._cfg.resolve_api_key())
        except Exception:
            live = []
        if live:
            self._cfg.available_models = live
            if self._cfg.model not in live:
                self._cfg.model = providers_mod.preferred_model(live, base_url)
        # Setup adds a row to the list like everything else does. It used to
        # write the loose fields above and stop, which is what made this one
        # provider unlike every other -- uneditable, undeletable, and needing
        # its own branch in every function that touched a provider.
        row = self._find_row(providers_mod.preset(preset)["label"] if known
                             else base_url)
        entry = normalize_provider({
            "name": (known or {}).get("label") or "",
            "base_url": base_url, "preset": preset,
            "api_key": self._cfg.api_key,
            "models": [model] if model else [],
            "all_models": list(live),
        })
        if row is None:
            self._cfg.providers.append(entry)
        else:
            row.clear()
            row.update(entry)
        self._cfg.default_provider = entry["name"]
        try:
            save_config(self._cfg)
        except Exception:
            pass
        self._client = None
        # Everything below is best-effort, for the same reason save_api_key is:
        # setup must complete once a key is entered, even where writing the
        # environment or reading old sessions fails.
        session, sessions = None, []
        try:
            session = self._resume_last()
            sessions = self.list_sessions()
        except Exception:
            pass
        # `reused` is not the same as `persisted`: nothing was written, but the
        # key IS on this PC for good. Without it the page would say "connected
        # for this session" -- which reads as "you will have to do this again".
        return {"ok": True, "persisted": persisted, "reused": reused and not api_key,
                "session": session, "sessions": sessions,
                "provider": (default_provider(self._cfg) or {}).get("name", "")}

    def save_api_key(self, key: str):
        key = (key or "").strip()
        if not key:
            return {"error": "empty key"}
        # Every step is defensive: onboarding must ALWAYS complete once a key
        # is entered. The key goes live via os.environ inside persist_env_var,
        # so even if persistence or resuming a prior session fails (a fresh or
        # locked-down machine), the app still opens ready to use.
        try:
            persisted = persist_env_var("ZAI_API_KEY", key)
        except Exception:
            persisted = False
        self._client = None
        session, sessions = None, []
        try:
            session = self._resume_last()
            sessions = self.list_sessions()
        except Exception:
            pass
        return {"ok": True, "persisted": persisted, "session": session,
                "sessions": sessions}

    def win(self, action: str):
        w = self._window
        if not w:
            return
        if action == "close":
            w.destroy()
        elif action == "min":
            w.minimize()
        elif action == "max":
            try:
                w.toggle_fullscreen()
            except Exception:
                pass

    def update_check(self):
        """Is there a newer version, and can it be taken? Changes nothing."""
        from .. import updater
        return updater.check()

    def update_apply(self):
        """Pull, then restart into the new code.

        The restart is spawned BEFORE this window closes and is detached from
        this process, because the alternative -- close, then start -- leaves a
        gap where the app is simply gone, and a failure in that gap is
        indistinguishable from the update having quit the app for good.

        A turn in flight is refused rather than interrupted: restarting mid-turn
        loses whatever the agent was part-way through, and an update is never
        so urgent that it cannot wait for a reply to finish.
        """
        from .. import updater
        busy = [cs.title or cs.sid for cs in self._chats.values()
                if cs.turn_lock.locked()]
        if busy:
            return {"ok": False,
                    "reason": f"'{busy[0]}' is still working. Let it finish, "
                              "then update -- restarting now would lose it."}
        result = updater.pull()
        if not result.get("ok"):
            return result
        if not result.get("updated"):
            return result
        try:
            self._save_current()
        except Exception:
            pass
        if not updater.spawn_restart():
            return {"ok": True, "updated": True, "restarted": False,
                    "changes": result.get("changes", []),
                    "reason": "Updated, but couldn't start the new copy. "
                              "Close the app and open it again."}
        # Give the new process a moment to get going before this window goes,
        # so the screen is never empty with nothing on the way.
        threading.Timer(1.2, lambda: self.win("close")).start()
        return {"ok": True, "updated": True, "restarted": True,
                "count": result.get("count", 0),
                "changes": result.get("changes", [])}

    def open_external(self, url: str):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def open_path(self, path: str):
        """Open a file/folder the agent mentioned, in whatever the OS has
        associated with it (editor for code, explorer for folders). Only ever
        called from a user's explicit click on a path in the chat."""
        if not isinstance(path, str) or not path.strip():
            return {"error": "empty"}
        p = Path(path.strip()).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            p = p.resolve()
        except OSError:
            return {"error": "bad path"}
        if not p.exists():
            return {"error": "not found"}
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606 -- user-initiated open
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True}

    # -- settings ---------------------------------------------------------- #

    def _settings(self):
        c = self._cfg
        return {
            "mode": c.mode, "model": c.model, "vision_model": c.vision_model,
            "vision_route": c.vision_route, "thinking": c.thinking,
            "thinking_mode": c.thinking_mode, "verify_edits": c.verify_edits,
            "auto_fix_tests": c.auto_fix_tests, "parallel_attempts": c.parallel_attempts,
            "codebase_memory_neural": c.codebase_memory_neural,
            "learn_from_mistakes": c.learn_from_mistakes,
            "show_reasoning": c.show_reasoning, "temperature": c.temperature,
            "cwd": str(Path.cwd()) if self.session_id else "",
            "background_custom": bool(c.background_path),
            "read_aloud": c.read_aloud, "tts_engine": c.tts_engine,
            "voice_engine": c.voice_engine, "live_voice": c.live_voice,
            # Whether ANY configured API can do speech to speech. This setting
            # is global while the API a chat runs on is per-chat, so asking
            # about one particular provider would grey the switch out for
            # someone who has a live-capable key and simply isn't using it in
            # the chat that happens to be open. Starting a session in a chat
            # that cannot do it says so by name at that point.
            "live_available": any(
                live.available(p.get("base_url", "")) for p in all_providers(c)),
            "tts_voice": c.tts_voice, "piper_voice": c.piper_voice, "tts_speed": c.tts_speed,
            "stt_model": c.stt_model, "stt_language": c.stt_language,
            "voice_sensitivity": c.voice_sensitivity,
            "voice_earcons": c.voice_earcons, "voice_ptt_key": c.voice_ptt_key,
            "voice_silence_ms": c.voice_silence_ms,
            "voice_wake_enabled": c.voice_wake_enabled,
            "voice_wake_word": c.voice_wake_word,
            "voice_wake_gated": c.voice_wake_gated,
            "voice_reply_language": c.voice_reply_language,
            "notifications": c.notifications, "reduce_effects": c.reduce_effects,
            "browser_headless": c.browser_headless,
            "browser_keep_logins": c.browser_keep_logins,
            "browser_connect_url": c.browser_connect_url,
            "browser_own": c.browser_own,
            "model_fallbacks": list(c.model_fallbacks or []),
            "browser_provider": c.browser_provider, "browser_model": c.browser_model,
            "path_rules": [dict(r) for r in c.path_rules],
            "github_clone_root": c.github_clone_root,
            "github_auto_pull": c.github_auto_pull, "github_auto_push": c.github_auto_push,
            "phone_app_url": c.phone_app_url,
        }

    def set_setting(self, key: str, value):
        c = self._cfg
        if key == "mode" and value in PERMISSION_MODES:
            if self._agent:
                self._agent.set_mode(value)
            else:
                c.mode = value
        elif key == "vision_route" and value in ("auto", "describe", "direct"):
            c.vision_route = value
        elif key == "thinking_mode" and value in ("low", "medium", "high", "max"):
            c.thinking_mode = value
            c.thinking = value != "low"  # keep the derived flag consistent
        elif key in ("thinking", "show_reasoning", "read_aloud", "notifications",
                     "reduce_effects", "browser_headless", "browser_keep_logins",
                     "verify_edits", "auto_fix_tests"):
            setattr(c, key, bool(value))
        elif key == "model_fallbacks":
            # A list of model ids, in order. Cleaned rather than trusted: it
            # comes from a text field, and a blank or duplicated entry would
            # silently make the chain shorter than it looks.
            seen, chain = set(), []
            for m in (value if isinstance(value, list) else []):
                m = str(m or "").strip()[:120]
                if m and m not in seen:
                    seen.add(m)
                    chain.append(m)
            c.model_fallbacks = chain[:6]
        elif key == "browser_own":
            c.browser_own = "auto" if str(value) in ("auto", "True", "true") or value is True else "off"
        elif key == "browser_connect_url":
            # Turning this on hands the agent the user's live logged-in
            # browser, so the value is checked rather than stored as typed: a
            # typo that silently did nothing would look like the feature being
            # broken, and pointing it off this machine is not a thing to do by
            # accident.
            url, err = _normalize_connect_url(value)
            if err:
                return {"error": err}
            c.browser_connect_url = url
        elif key in ("model", "vision_model") and isinstance(value, str) and value.strip():
            setattr(c, key, value.strip())
            if key == "model" and self._agent:
                self._agent.rebuild_system_prompt()
        elif key == "voice_engine" and value in ("local", "live"):
            c.voice_engine = value
        elif key == "live_voice" and isinstance(value, str) and value.strip():
            c.live_voice = value.strip()[:40]
        elif key == "tts_engine" and value in ("kokoro", "piper"):
            c.tts_engine = value
        elif key == "tts_voice" and isinstance(value, str) and value.strip():
            c.tts_voice = value.strip()
        elif key == "piper_voice" and isinstance(value, str) and value.strip():
            c.piper_voice = value.strip()
        elif key == "stt_model" and isinstance(value, str) and value.strip():
            c.stt_model = value.strip()
        elif key == "stt_language" and isinstance(value, str):
            c.stt_language = value.strip()
        elif key == "voice_sensitivity":
            try:
                c.voice_sensitivity = min(2.0, max(0.5, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "voice_earcons":
            c.voice_earcons = bool(value)
        elif key == "voice_ptt_key" and isinstance(value, str) and value.strip():
            c.voice_ptt_key = value.strip()[:32]
        elif key == "voice_silence_ms":
            try:
                c.voice_silence_ms = int(min(1600, max(400, float(value))))
            except (TypeError, ValueError):
                pass
        elif key == "voice_wake_enabled":
            c.voice_wake_enabled = bool(value)
        elif key == "voice_wake_word" and isinstance(value, str) and value.strip():
            c.voice_wake_word = value.strip()[:60]
        elif key == "voice_wake_gated":
            c.voice_wake_gated = bool(value)
        elif key == "voice_reply_language" and value in ("en", "match"):
            c.voice_reply_language = value
            # If a voice session is open, refresh its prompt so the change
            # applies right away rather than only on the next session.
            cs = self._active
            if cs is not None and cs.convo_agent is not None:
                try:
                    cs.convo_agent.rebuild_system_prompt()
                except Exception:
                    pass
        elif key == "tts_speed":
            try:
                c.tts_speed = min(2.0, max(0.5, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "temperature":
            try:
                c.temperature = min(1.5, max(0.0, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "codebase_memory_neural":
            c.codebase_memory_neural = bool(value)
            from .. import codebase_memory
            codebase_memory.set_neural_enabled(c.codebase_memory_neural)
            if c.codebase_memory_neural and not codebase_memory.NeuralEmbedder.packages_installed():
                self._install_neural_memory()   # background; falls back to lexical until ready
        elif key == "learn_from_mistakes":
            c.learn_from_mistakes = bool(value)
            # Takes effect in the open chat, not only in the next one: the
            # block lives in the system prompt, and the prompt is cached until
            # something rebuilds it (same reason the remember tool does this).
            if self._agent:
                self._agent.rebuild_system_prompt()
        elif key == "parallel_attempts":
            try:
                c.parallel_attempts = int(min(3, max(1, int(value))))
            except (TypeError, ValueError):
                pass
        elif key == "github_clone_root":
            c.github_clone_root = str(value or "").strip()
        elif key == "phone_app_url":
            c.phone_app_url = str(value or "").strip()
        elif key in ("github_auto_pull", "github_auto_push"):
            setattr(c, key, bool(value))
        elif key == "path_rules":
            # Mutate the existing list IN PLACE (c.path_rules[:] = ...) rather
            # than rebinding it: every live agent's PermissionEngine shares this
            # same list object, so in-place update applies the new rules to all
            # open chats immediately.
            c.path_rules[:] = _normalize_path_rules(value)
        else:
            return {"error": f"unknown setting {key}"}
        save_config(c)
        return self._settings()

    # -- background ---------------------------------------------------------- #

    def get_background(self) -> str:
        """Data URI for a CUSTOM background only. The DEFAULT background is
        served straight from disk by CSS (#bg loads bg-default.jpg relative to
        the page), so it never depends on this call, boot timing, or the file
        being base64-embeddable -- an empty string means "use the CSS default".
        (Regression: reading/encoding DEFAULT_BG here used to be able to raise
        or come back blank, leaving a fresh install with no background at all.)"""
        try:
            p = Path(self._cfg.background_path) if self._cfg.background_path else None
            if p and p.is_file():
                return _data_uri(p)
        except Exception:
            pass
        return ""

    def pick_background(self):
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)",
                        "All files (*.*)"),
        )
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            return {"error": "not an image file"}
        self._cfg.background_path = str(path)
        save_config(self._cfg)
        return {"background": self.get_background()}

    def reset_background(self):
        self._cfg.background_path = ""
        save_config(self._cfg)
        return {"background": self.get_background()}

    # -- sessions (chat history + per-project work folder) ------------------ #

    def list_sessions(self):
        return self._store.list()

    def search_chats(self, query: str):
        """Sidebar full-text search: matches chat titles and the full
        transcripts (which keep even compacted-away conversation)."""
        return {"sessions": search_sessions(self._store.list(), query)}

    # -- model providers (bring your own model) ------------------------- #

    def providers(self):
        """All providers (built-in + custom) with keys masked, plus the
        current chat's choice.

        `tier` travels with each row, and `chat_tier` with the current choice,
        so the UI never has to work out what a model costs. It used to print
        "$0.00" and "via z.ai" from constants in the markup, which was true
        only while z.ai was the only thing this app could talk to.
        """
        out = []
        used = usage_mod.today()
        # The refusals, which are the only first-hand number here. `used` is
        # measured against free_limits(), a table in this repository: it goes
        # stale when a provider changes a tier and knows nothing about models
        # it has no row for. A 429 is the provider saying no.
        refused = usage_mod.limited_today()
        for p in all_providers(self._cfg):
            models = p.get("models") or []
            # Where this provider's keys come from, so the key field can point
            # at the right console instead of naming z.ai whatever it is.
            known = providers_mod.preset_from_base_url(p["base_url"])
            # Requests made today against each model, with the free-tier
            # allowance where one is known. A day's quota here is small enough
            # to run out mid-task (20/day for Gemini Flash), so it belongs
            # where the model is chosen rather than in a settings page.
            quota = {}
            for m in (p.get("all_models") or models):
                lim = providers_mod.free_limits(p["base_url"], m)
                hit = refused.get(m) or {}
                if lim or used.get(m) or hit:
                    quota[m] = {"used": used.get(m, 0),
                                "rpd": (lim or {}).get("rpd"),
                                "rpm": (lim or {}).get("rpm"),
                                # Times this model refused today, and whether
                                # it is refusing right NOW -- which is what
                                # answers "why did it switch models on me".
                                "limited": int(hit.get("n", 0)),
                                "limited_at": hit.get("at", 0),
                                "cooling": api_mod.is_cooling_down(
                                    p["base_url"], m)}
            out.append({"name": p["name"], "base_url": p["base_url"],
                        "models": models, "quota": quota,
                        # Everything the provider listed, when that is more
                        # than the shortlist shown by default.
                        "all_models": p.get("all_models") or models,
                        "preset": p.get("preset", ""),
                        # Whether its key lives in an environment variable
                        # rather than the config file. The form needs to know,
                        # because that is the one field it cannot show back.
                        "env_var": p.get("env_var", ""),
                        "local": providers_mod.is_local(p["base_url"]),
                        "multimodal": providers_mod.is_multimodal(p["base_url"]),
                        "tier": providers_mod.model_tier(
                            p["base_url"], models[0] if models else ""),
                        "key_url": (known or {}).get("key_url", ""),
                        "has_key": bool(cfg_provider_key(p))})
        default = default_provider(self._cfg)
        chat_provider = self.session_provider or (default or {}).get("name", "")
        chat_model = self.session_model or cfg_default_model(self._cfg)
        chosen = find_provider(self._cfg, chat_provider)
        vprov, vmodel = vision_target(self._cfg, chosen, chat_model)
        return {"providers": out,
                "chat_provider": chat_provider,
                "chat_model": chat_model,
                # Which row new chats start on. Marked rather than positional:
                # "the first one" was the old rule and it was invisible, which
                # is how a list nobody could reorder ended up meaning something.
                "default_provider": (default or {}).get("name", ""),
                "default_model": cfg_default_model(self._cfg),
                # The image reader, and whether it was chosen or worked out.
                "vision_provider": vprov["name"] if vprov else "",
                "vision_model": vmodel,
                "vision_pinned": bool(self._cfg.vision_provider),
                "chat_tier": providers_mod.model_tier(
                    (chosen or {}).get("base_url", ""), chat_model)}

    def set_default_model(self, provider: str, model: str):
        """Which model new chats start on."""
        prov = find_provider(self._cfg, provider)
        if prov is None:
            return {"error": f'no API named "{provider}"'}
        if model and model not in (prov.get("all_models") or []):
            return {"error": f'{prov["name"]} does not list "{model}"'}
        self._cfg.default_provider = prov["name"]
        self._cfg.model = model or (prov.get("models") or [""])[0]
        save_config(self._cfg)
        return self.providers()

    def set_vision_model(self, provider: str, model: str):
        """Which model reads images. Empty provider means work it out per chat.

        A setting at all because it was never one: the image model was whatever
        setup happened to write, it belonged to the provider chosen there, and
        a chat that moved elsewhere still had its images -- and then its whole
        turn -- sent back to it.
        """
        if not provider:
            self._cfg.vision_provider = ""
            self._cfg.vision_model = ""
            save_config(self._cfg)
            return self.providers()
        prov = find_provider(self._cfg, provider)
        if prov is None:
            return {"error": f'no API named "{provider}"'}
        if model and model not in (prov.get("all_models") or []):
            return {"error": f'{prov["name"]} does not list "{model}"'}
        self._cfg.vision_provider = prov["name"]
        self._cfg.vision_model = model or (prov.get("models") or [""])[0]
        save_config(self._cfg)
        # Live chats cache a vision client; drop it so the change takes now.
        for cs in self._chats.values():
            if cs.agent is not None:
                cs.agent.vision_client = None
        return self.providers()

    def add_provider(self, name: str, base_url: str, api_key: str, models: str):
        return self.save_provider("", name, base_url, api_key, models)

    def _find_row(self, name: str) -> dict | None:
        """The stored dict for a provider, by any name it has been known by.

        The stored dict and not a normalized copy: this is the one edits are
        written into.
        """
        resolved = find_provider(self._cfg, name)
        if resolved is None:
            return None
        base = resolved.get("base_url", "")
        for row in self._cfg.providers:
            if row.get("name") == resolved["name"] or \
                    (row.get("base_url") or "").rstrip("/") == base:
                return row
        return None

    def save_provider(self, original_name: str, name: str, base_url: str,
                      api_key: str, models: str):
        """Add a new API, or save edits to any existing one. `original_name`
        is the row the form was opened from ("" = adding a new one).

        One path for every row. The provider chosen at setup used to take a
        different one that accepted nothing but a key -- so its name, its URL
        and its model list were the only ones in the app that could not be
        corrected, and the form silently discarded three of the four fields
        someone had just filled in.
        """
        original_name = (original_name or "").strip()
        name = (name or "").strip()
        api_key = (api_key or "").strip()
        base_url = (base_url or "").strip().rstrip("/")
        model_list = [m.strip() for m in (models or "").split(",") if m.strip()]
        existing = self._find_row(original_name) if original_name else None
        if original_name and existing is None:
            return {"error": f'no API named "{original_name}" to edit'}
        # Editing keeps whatever the form did not send, so a row can be
        # renamed without retyping its model list.
        if existing is not None:
            name = name or existing.get("name", "")
            base_url = base_url or (existing.get("base_url") or "")
            model_list = model_list or list(existing.get("models") or [])
        if not name or not base_url or not model_list:
            return {"error": "name, base URL and at least one model id are required"}
        clash = self._find_row(name)
        if clash is not None and clash is not existing:
            return {"error": f'an API named "{name}" already exists'}
        entry = normalize_provider({
            **(existing or {}),
            "name": name, "base_url": base_url, "models": model_list,
        })
        # Everything the endpoint lists stays reachable, but it belongs to the
        # URL: repointing a row at another provider must not leave the old
        # provider's models behind it under "show all".
        if existing is not None and (existing.get("base_url") or "").rstrip("/") != base_url:
            entry["all_models"] = list(model_list)
        persisted = None
        if api_key:
            # Into the provider's own environment variable when it has one --
            # the same place onboarding writes, so a key set here survives
            # deleting the config folder exactly as one set at setup does.
            if entry.get("env_var"):
                persisted = persist_env_var(entry["env_var"], api_key)
                # Kept as the fallback for a machine where writing the
                # environment is blocked by policy.
                entry["api_key"] = api_key
                if entry["base_url"] == (self._cfg.base_url or "").rstrip("/"):
                    self._cfg.api_key = api_key
            else:
                entry["api_key"] = api_key
        if existing is None:
            self._cfg.providers.append(entry)
        else:
            existing.clear()
            existing.update(entry)
            if original_name != name:
                # Chats and the two pointers follow the rename rather than
                # silently falling back to something else.
                for cs in self._chats.values():
                    if cs.provider == original_name:
                        cs.provider = name
                if self._cfg.default_provider == original_name:
                    self._cfg.default_provider = name
                if self._cfg.vision_provider == original_name:
                    self._cfg.vision_provider = name
        save_config(self._cfg)
        self._client = None       # rebuild with the new url/key on next use
        # The active chat picks up new url/key/models immediately; background
        # and reopened chats re-apply their provider on activation anyway.
        if self.session_provider in (name, original_name) and self._agent \
                and not self._agent.busy:
            keep = self.session_model if self.session_model in model_list else ""
            self._apply_chat_model(self._agent, name, keep)
            self._save_current()
        res = self.providers()
        if persisted is not None:
            res["persisted_env"] = persisted
        return res

    def delete_provider(self, name: str):
        """Remove an API. Any API -- there is no undeletable one.

        There used to be, and not on purpose: the row created at setup lived
        outside cfg.providers, so the filter below could not see it and the UI
        drew no delete button for it. Nothing about it was more permanent than
        any other row; it was just stored somewhere the delete could not reach.
        """
        row = self._find_row(name)
        if row is None:
            return {"error": f'no API named "{name}"'}
        gone = row.get("name", name)
        base = (row.get("base_url") or "").rstrip("/")
        self._cfg.providers = [p for p in self._cfg.providers if p is not row]
        # The legacy fields describe this same endpoint, and leaving them would
        # have __post_init__ helpfully put the row back on the next launch.
        if base and base == (self._cfg.base_url or "").rstrip("/"):
            self._cfg.base_url = ""
            self._cfg.provider_preset = ""
            self._cfg.api_key = ""
            self._cfg.available_models = []
        if self._cfg.vision_provider == gone:
            self._cfg.vision_provider = ""
            self._cfg.vision_model = ""
        if self._cfg.default_provider == gone:
            nxt = (self._cfg.providers or [None])[0]
            self._cfg.default_provider = (nxt or {}).get("name", "")
            self._cfg.model = ((nxt or {}).get("models") or [""])[0]
        if not self._cfg.providers:
            # Nothing left to talk to. Say so by sending the app back to the
            # screen for it, rather than leaving a chat window that fails on
            # every message.
            self._cfg.setup_done = False
        save_config(self._cfg)
        self._client = None
        for cs in self._chats.values():
            if cs.provider == gone:
                cs.provider = ""
                cs.model = ""
                if cs.agent is not None and not cs.agent.busy:
                    self._apply_chat_model(cs.agent, "", "")
        if self.session_provider == gone and self._agent:
            self._save_current()
        res = self.providers()
        res["needsKey"] = not self._cfg.providers
        return res

    # -- custom slash commands --------------------------------------------- #

    def commands(self):
        return {"commands": list(self._cfg.commands)}

    def add_command(self, name: str, template: str):
        name = (name or "").strip().lstrip("/").strip()
        template = (template or "").strip()
        if not name or not template:
            return {"error": "both a name and a prompt template are required"}
        if not name.replace("-", "").replace("_", "").isalnum():
            return {"error": "name may only contain letters, numbers, - and _"}
        self._cfg.commands = [c for c in self._cfg.commands if c.get("name") != name]
        self._cfg.commands.append({"name": name, "template": template})
        save_config(self._cfg)
        return self.commands()

    def delete_command(self, name: str):
        self._cfg.commands = [c for c in self._cfg.commands if c.get("name") != name]
        save_config(self._cfg)
        return self.commands()

    # -- export ------------------------------------------------------------ #

    def export_chat(self):
        """Save the active chat as a Markdown file (via a Save dialog)."""
        cs = self._active
        if not cs:
            return {"error": "no active chat"}
        title = cs.title or "chat"
        safe = re.sub(r"[^\w -]+", "", title).strip() or "chat"
        try:
            picked = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"{safe}.md")
        except Exception as e:
            return {"error": str(e)}
        if not picked:
            return {"cancelled": True}
        path = Path(picked if isinstance(picked, str) else picked[0])
        try:
            path.write_text(self._chat_markdown(cs), encoding="utf-8")
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True, "path": str(path)}

    def _chat_markdown(self, cs: "ChatState") -> str:
        lines = [f"# {cs.title or 'Conversation'}", "",
                 f"*Project: {cs.agent.workdir}*", ""]
        for it in to_display(cs.agent.messages):
            kind = it.get("kind")
            if kind == "user":
                lines += ["---", "", "### You", "", it.get("text", ""), ""]
            elif kind == "assistant":
                lines += ["### Agent", "", it.get("text", ""), ""]
            elif kind == "tool":
                args = it.get("args", {})
                summary = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                lines += [f"> 🔧 `{it.get('name')}`" + (f" ({summary})" if summary else ""), ""]
            elif kind == "compacted":
                lines += ["> *— context compacted —*", ""]
            elif kind == "steered":
                lines += [f"> ↪ *steered:* {it.get('text', '')}", ""]
        return "\n".join(lines).rstrip() + "\n"

    # -- MCP servers ------------------------------------------------------- #

    def mcp_status(self):
        """Configured MCP servers with live state, for Settings."""
        return {"servers": self._mcp.status()}

    def mcp_add(self, name: str, command: str):
        name = (name or "").strip()
        command = (command or "").strip()
        if not name or not command:
            return {"error": "both a name and a command are required"}
        if any(e.get("name") == name for e in self._cfg.mcp_servers):
            return {"error": f'an MCP server named "{name}" already exists'}
        self._cfg.mcp_servers.append({"name": name, "command": command})
        save_config(self._cfg)
        self._mcp.start_all_async()
        return self.mcp_status()

    def mcp_delete(self, name: str):
        self._cfg.mcp_servers = [e for e in self._cfg.mcp_servers
                                 if e.get("name") != name]
        save_config(self._cfg)
        self._mcp.start_all_async()  # also stops servers dropped from config
        return self.mcp_status()

    def mcp_restart(self, name: str):
        threading.Thread(target=self._mcp.restart, args=(name,),
                         daemon=True).start()
        return {"ok": True}

    def detect_local_providers(self):
        """Probe the well-known local OpenAI-compatible servers (Ollama,
        LM Studio) and add any that respond as providers."""
        import requests as _requests
        added = []
        probes = [
            ("Ollama (local)", "http://localhost:11434/v1",
             "http://localhost:11434/api/tags",
             lambda d: [m["name"] for m in d.get("models", [])]),
            ("LM Studio (local)", "http://localhost:1234/v1",
             "http://localhost:1234/v1/models",
             lambda d: [m["id"] for m in d.get("data", [])]),
        ]
        for name, base_url, probe_url, extract in probes:
            try:
                r = _requests.get(probe_url, timeout=0.8)
                models = extract(r.json())
            except Exception:
                continue
            if not models:
                continue
            # Matched on the URL, not the name: a local server already set up
            # through the Ollama preset is called "Ollama (on this PC)" and
            # would otherwise be added a second time under a different label.
            row = next((p for p in self._cfg.providers
                        if (p.get("base_url") or "").rstrip("/") == base_url), None)
            if row is not None:
                row["models"] = models
                row["all_models"] = models
            else:
                self._cfg.providers.append(normalize_provider(
                    {"name": name, "base_url": base_url,
                     "api_key": "local", "models": models,
                     "all_models": models}))
            added.append(f"{name} ({len(models)} models)")
        if added:
            save_config(self._cfg)
        res = self.providers()
        res["found"] = added
        return res

    def set_chat_model(self, provider_name: str, model: str):
        """Switch the CURRENT chat to a provider+model (per chat -- new chats
        keep the free default)."""
        if not self._agent or not self.session_id:
            return {"error": "no active chat"}
        if self._agent.busy:
            return {"error": "can't switch models while the agent is working"}
        if provider_name and not find_provider(self._cfg, provider_name):
            return {"error": f'unknown provider "{provider_name}"'}
        self._apply_chat_model(self._agent, provider_name, model)
        self._save_current()
        return self.providers()

    def _apply_chat_model(self, agent: Agent, provider_name: str, model: str) -> None:
        """Point an agent at the chat's chosen provider+model.

        One path, because there is one kind of provider now. It used to fork on
        `builtin`, and the two halves disagreed: the builtin half discarded any
        model equal to cfg.model (so picking the default back looked like it
        did nothing), and the custom half pinned `vision_client` at the setup
        provider -- which is how a chat moved to z.ai kept reading its images,
        and then answering, on Gemini.
        """
        prov = find_provider(self._cfg, provider_name) if provider_name else None
        if prov is None:
            prov = default_provider(self._cfg)
            model = model or cfg_default_model(self._cfg)
        if prov is None:
            return                      # nothing configured; setup will run
        # Worked out locally and then stored, rather than stored and read back.
        # session_model writes through to the active chat and silently does
        # nothing when there isn't one -- so reading it back handed the agent
        # None, and the chat kept answering on the model it was already using.
        chosen = model or (prov.get("models") or [""])[0]
        self.session_provider = prov["name"]
        self.session_model = chosen
        agent.client = ZaiClient(cfg_provider_key(prov), prov["base_url"])
        agent.model_override = chosen or None
        # Dropped, not pointed anywhere: the agent resolves the image model
        # from the chat's own provider now (Agent._vision_client_and_model),
        # and a stale client here would outrank it.
        agent.vision_client = None
        # The prompt names the model, so it has to be rebuilt when the model
        # changes. Without this the switch takes effect on the wire and not in
        # the prompt, and the two disagree for the rest of the chat.
        agent.rebuild_system_prompt()

    def _activate_session(self, sid: str, messages: list, cwd: str,
                          prompt_tokens: int, completion_tokens: int,
                          todos: list, title: str = "", auto_backup: bool = True,
                          model_provider: str = "", model: str = "",
                          turn_snapshots: list | None = None) -> dict:
        # A chat that's already open (possibly mid-turn in the background)
        # just becomes the active one -- its live agent, not a disk reload.
        if sid in self._chats:
            return self._switch_to_live(sid)
        cwd_ok = True
        if cwd:
            try:
                os.chdir(cwd)  # for the file-picker dialogs' starting folder
            except OSError:
                cwd_ok = False
        client = self._ensure_client()
        if client is None:
            return {"error": "no API key configured"}
        workdir = Path(cwd) if (cwd and cwd_ok) else Path.cwd()
        events = self._make_events(sid)
        agent = Agent(self._cfg, client, events=events, workdir=workdir)
        agent.mcp = self._mcp
        agent.load_messages(messages)
        agent.set_usage(prompt_tokens, completion_tokens)
        agent.todos = list(todos or [])
        self._chats[sid] = ChatState(sid, agent, events)
        self._chats[sid].turn_snapshots = list(turn_snapshots or [])
        self.session_id = sid
        self.session_title = title
        self.auto_backup = auto_backup
        self._apply_chat_model(agent, model_provider, model)
        self._backup_repo = BackupRepo(sid, workdir) if cwd_ok else None
        agent.backup_repo = self._backup_repo  # powers the review_changes tool
        # Append-only conversation log; rebuild so the system prompt gains
        # the note telling the model these files exist and how to grep them.
        agent.transcript = Transcript(sid, cwd=str(workdir))
        agent.rebuild_system_prompt()
        self._cfg.last_session_id = sid
        save_config(self._cfg)
        if cwd_ok:
            self._maybe_autopull(workdir)  # background pull if this is a connected repo
        return self._session_payload(self._chats[sid])

    def _switch_to_live(self, sid: str) -> dict:
        cs = self._chats[sid]
        self.session_id = sid
        try:
            os.chdir(cs.agent.workdir)
        except OSError:
            pass
        self._cfg.last_session_id = sid
        save_config(self._cfg)
        return self._session_payload(cs)

    def _session_payload(self, cs: "ChatState") -> dict:
        agent = cs.agent
        u = agent.session_usage
        items = to_display(agent.messages)
        for it in items:
            if it.get("kind") in ("tool_image", "tool_audio") and it.get("path"):
                try:
                    it["src"] = _data_uri(Path(it["path"]))
                except OSError:
                    it["src"] = ""  # file moved/deleted since it was shown
        return {
            "ok": True, "id": cs.sid, "cwd": str(agent.workdir),
            "cwd_missing": not agent.workdir.is_dir(),
            "items": items, "todos": agent.todos,
            "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
            "context": agent.context_estimate(),
            "busy": agent.busy,
            "needs_notes": self._needs_project_notes(agent.workdir),
            # The chat's workers, so the activity rail can be rebuilt for the
            # chat being opened. It was a bare frontend object that nothing
            # ever cleared, so switching chats left the previous one's workers
            # on screen -- and worker ids are per chat ("wk1", "wk2"), so the
            # new chat's first worker overwrote the old chat's entry and its
            # pill opened someone else's thread.
            "workers": self._worker_rows(agent),
        }

    @staticmethod
    def _worker_rows(agent) -> list:
        """What the rail needs about each worker, in dispatch order.

        Read from the coding agent, which since Agent.adopt_workers_of shares
        one registry with the chat's delegator -- so this is the whole chat's
        set however it was dispatched, and not just the typed half.
        """
        try:
            with agent._workers_lock:
                workers = list(agent._workers.values())
        except Exception:
            return []
        return [{"id": w["id"], "name": w["name"], "status": w["status"]}
                for w in workers]

    @staticmethod
    def _needs_project_notes(workdir: Path) -> bool:
        """True for a real project folder that has no agent-notes file yet, so
        the UI can offer to generate one. Skips the whiteboard and empty dirs."""
        try:
            if not workdir.is_dir() or workdir.resolve() == WHITEBOARD_DIR.resolve():
                return False
            from ..prompts import AGENT_MD_NAMES
            if any((workdir / n).is_file() for n in AGENT_MD_NAMES):
                return False
            # Only offer when there's actually code/content to learn.
            for entry in workdir.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.is_file() or entry.is_dir():
                    return True
            return False
        except OSError:
            return False

    def generate_project_notes(self):
        """Kick off a turn that explores the project and writes a GLM.md."""
        from ..prompts import GLM_MD_TASK
        if self._active is None:
            return {"error": "Open a chat first."}
        self.send(GLM_MD_TASK)
        return {"ok": True}

    def new_session(self, auto_backup: bool = True):
        """Start a brand-new chat. The user picks the project folder themselves —
        nothing is auto-created or defaulted. Other chats keep running."""
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_dir():
            return {"error": "not a folder"}
        res = self._activate_session(new_id(), [], str(path), 0, 0, [], auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        return res

    def new_session_in(self, folder: str, auto_backup: bool = True):
        """Start a chat in a folder the user has worked in before.

        Same as new_session without the folder dialog. Reopening a project you
        already have chats in was a trip through the OS picker every time, even
        though the path was sitting in the session list -- so the app knew the
        answer and asked anyway.

        The path is checked rather than trusted: it comes from a stored session
        and the folder may since have been moved, renamed or deleted, and the
        agent's whole idea of a workspace is its working directory.
        """
        folder = (folder or "").strip()
        if not folder:
            return {"error": "no folder"}
        path = Path(folder).expanduser()
        if not path.is_dir():
            return {"error": f"folder not found: {folder}"}
        res = self._activate_session(new_id(), [], str(path), 0, 0, [],
                                     auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        return res

    def open_whiteboard(self, auto_backup: bool = True):
        """Start a brand-new chat in the always-available scratch folder,
        creating it next to this app's own install directory if this is the
        first time it's used. No folder picker -- unlike new_session, there's
        nothing to choose."""
        WHITEBOARD_DIR.mkdir(parents=True, exist_ok=True)
        res = self._activate_session(new_id(), [], str(WHITEBOARD_DIR), 0, 0, [], auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        return res

    def clear_whiteboard(self):
        """Delete everything inside the whiteboard folder (not the folder
        itself, and not any chat history -- purely a filesystem reset for
        the scratch folder's contents)."""
        WHITEBOARD_DIR.mkdir(parents=True, exist_ok=True)
        for child in WHITEBOARD_DIR.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True}

    # -- Cross-device session sync (shared with the phone app) --------------- #
    #
    # Chats live encrypted on the repo's orphan `makenomistakes/state` branch,
    # under a key derived from a SYNC PASSPHRASE that is separate from the
    # GitHub token and never leaves this machine (stored via the same secure
    # secretstore). GitHub only ever sees ciphertext. See glmcode/syncstore.py.

    def _device_identity(self) -> tuple[str, str]:
        """(device_id, human label) for this install -- stable across
        launches, used only to tell devices apart for the sync lock (see
        syncstore.py's "SAME CHAT, TWO DEVICES"). Generated once, stashed in
        config.extra (it isn't a secret, no reason for its own store)."""
        did = self._cfg.extra.get("device_id")
        if not did:
            did = uuid.uuid4().hex
            self._cfg.extra["device_id"] = did
            save_config(self._cfg)
        label = "desktop"
        try:
            host = socket.gethostname().strip()
            if host:
                label = f"desktop ({host[:24]})"
        except OSError:
            pass
        return did, label

    def _try_acquire_device_lock(self, sid: str, force: bool = False) -> dict | None:
        """None if there's nothing to coordinate (sync off, or sync
        unreachable -- fails OPEN, since an unreachable sync store means the
        other device can't push either, so there's no race to protect
        against); {"locked": True, ...} if another device holds a live lock
        and `force` wasn't set. On success, starts the heartbeat that keeps
        the lock alive for the rest of this turn."""
        if not (syncstore.crypto_available() and syncstore.load_passphrase()):
            return None
        device_id, device_label = self._device_identity()
        try:
            store, err = self._open_sync_store()
            if err or not store:
                return None
            store.acquire_lock(sid, device_id, device_label, force=force)
        except syncstore.LockedElsewhere as e:
            return {"locked": True, "error": str(e),
                    "locked_by": e.device_label, "locked_since": e.since_ms}
        except (syncstore.SyncError, githubsync.GitHubError):
            return None
        self._chat_locks[sid] = self._start_lock_heartbeat(sid, store, device_id, device_label)
        return None

    def _start_lock_heartbeat(self, sid: str, store, device_id: str,
                              device_label: str) -> threading.Event:
        stop = threading.Event()

        def beat():
            while not stop.wait(syncstore.DEVICE_LOCK_HEARTBEAT_S):
                try:
                    if not store.renew_lock(sid, device_id, device_label):
                        self._make_events(sid).warn(
                            "Heads up: this chat is now also being used on another device.")
                        return
                except Exception:
                    pass   # transient -- the next heartbeat tries again
        threading.Thread(target=beat, daemon=True).start()
        return stop

    def _release_device_lock(self, sid: str) -> None:
        """Best-effort; a lock we fail to release here still self-heals via
        its own TTL within DEVICE_LOCK_TTL_MS."""
        stop = self._chat_locks.pop(sid, None)
        if stop:
            stop.set()
        if not (syncstore.crypto_available() and syncstore.load_passphrase()):
            return
        try:
            device_id, _label = self._device_identity()
            store, err = self._open_sync_store()
            if not err and store:
                store.release_lock(sid, device_id)
        except Exception:
            pass









    # ------------------------------------------------------------------ #
    # Telling the phone. The desktop finishes turns the phone was suspended
    # through, and the phone was never told -- you found out by opening the app
    # and looking. Web Push closes exactly that gap: it cannot run anything on
    # the phone, but it can say so.

    VAPID_ACCOUNT = "webpush-vapid"

    # ------------------------------------------------------------------ #
    # The runner. Work that continues with every device off -- which the
    # desktop pickup cannot promise, since the desktop has to be awake.











    def _chat_repo(self, cwd: str) -> dict | None:
        """The GitHub repo this project lives in, for the phone to work against.

        The phone has no filesystem: every tool it runs goes through the GitHub
        API, so it needs to know which repository the conversation is about.
        Returns None when this folder has no GitHub origin -- the phone then
        says so instead of quietly using whichever repo it had open last.
        """
        if not cwd:
            return None
        try:
            st = githubsync.status(Path(cwd))
            parsed = githubsync.repo_from_remote(st.remote_url)
            if not parsed:
                return None
            owner, repo = parsed
            return {"owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
                    "branch": st.branch or "main"}
        except Exception:
            return None   # a chat must never fail to sync over a git hiccup

    def _repo_state(self, cwd: str) -> dict | None:
        """This machine's git state for a project, published with the chat.

        The phone reads the repo over the GitHub API, so anything living only
        on this disk -- uncommitted edits, or commits not pushed yet -- is
        invisible to it. Without this it would read an older copy of a file and
        commit straight over the work sitting here.

        Three answers, and None is the one that used to be missing:

        - a state -- the phone warns, naming what it is up against;
        - `{}` -- KNOWN to be nothing to warn about (no cwd, or no GitHub
          remote at all), which correctly clears any warning still standing;
        - `None` -- we could not find out. Published as `{}` this was a git
          hiccup telling the phone everything was fine, and `SyncStore.save`
          MERGES, so an empty dict is present and overwrites the last true
          answer rather than leaving it be. Absent means "I have nothing to
          say about this" everywhere else in that store; it has to mean it
          here, where the thing not said is "editing this file may commit over
          work that only exists on the desktop".
        """
        if not cwd:
            return {}
        try:
            st = githubsync.status(Path(cwd))
        except Exception:
            return None   # a chat must never fail to sync over a git hiccup
        if not st.connected:
            return {}
        return {"branch": st.branch or "", "dirty": bool(st.dirty),
                "ahead": int(st.ahead or 0), "at": syncstore._now_ms()}



    def _maybe_autosync(self, sid: str, then_unlock: bool = False) -> None:
        """Push a finished turn to the sync repo, in the background.

        Seamless means the user never presses Upload. This runs off the turn
        thread and swallows everything: the local session is already saved, so a
        failed push costs nothing but a retry next turn.

        then_unlock=True (only when this turn held the cross-device lock)
        releases it only once THIS push actually lands, not when the turn's
        synchronous work finishes -- releasing any earlier would reopen the
        exact race the lock exists to close: another device could acquire and
        push its own (now stale) copy while ours is still in flight."""
        if not sid or not syncstore.load_passphrase():
            if then_unlock:
                self._release_device_lock(sid)
            return

        def push():
            try:
                self.sync_push_chat(sid)
            except Exception:
                pass  # offline / rate-limited / no token -- retried next turn
            finally:
                if then_unlock:
                    self._release_device_lock(sid)

        threading.Thread(target=push, daemon=True).start()

    def _maybe_autopull(self, workdir: Path) -> None:
        """Background best-effort pull when opening a connected session. Skips a
        dirty tree (never touches uncommitted local work automatically)."""
        if not self._cfg.github_auto_pull:
            return
        ev = self._events  # capture now; the active chat may change later
        def work():
            try:
                st = githubsync.status(workdir)
                if not st.connected:
                    return
                if st.dirty:
                    # Uncommitted work here, so pulling automatically could
                    # clobber it. Say so rather than leaving the agent to read
                    # stale files: silently working from an out-of-date tree is
                    # how the phone's commits get overwritten. Fetch first --
                    # 'behind' is counted against the last-seen origin ref, so
                    # without it the phone's fresh push looks like nothing.
                    githubsync.refresh_remote(workdir, self._gh_token())
                    st = githubsync.status(workdir)
                    if st.behind > 0:
                        ev.toast(
                            f"{st.behind} new commit(s) on GitHub, but this folder has "
                            "uncommitted changes — pull manually to catch up.", "warn")
                    return
                token = self._gh_token()
                out = githubsync.pull(workdir, token)
                if "up to date" not in out.lower():
                    ev.toast("Pulled latest from GitHub.", "info")
            except Exception:
                pass  # opening a chat must never fail because of a pull
        threading.Thread(target=work, daemon=True).start()

    def _maybe_autopush(self, cs: "ChatState") -> None:
        """Background best-effort commit+push after a turn that changed files."""
        if not self._cfg.github_auto_push:
            return
        workdir = Path(cs.agent.workdir)
        ev = cs.events
        def work():
            try:
                st = githubsync.status(workdir)
                if not st.connected or not (st.dirty or st.ahead > 0):
                    return
                token = self._gh_token()
                githubsync.sync(workdir, token,
                                message=cs.title or "Update via Make No Mistakes")
                ev.toast("Synced changes to GitHub.", "info")
            except githubsync.GitHubError as e:
                ev.toast(f"GitHub sync failed: {e}", "warn")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # -- scheduled & watched tasks ----------------------------------------- #

    def scheduled_tasks(self):
        from .. import scheduler as sched
        return {"tasks": [{**t, "desc": sched.describe(t)} for t in self._cfg.scheduled_tasks]}

    def save_scheduled_task(self, task: dict):
        from .. import scheduler as sched
        norm = sched.normalize_task(task or {})
        if norm is None:
            return {"error": "That task is missing a prompt, folder, or a valid schedule."}
        tasks = self._cfg.scheduled_tasks
        # For a watch task, record the current baseline so it fires on the NEXT
        # change, not immediately.
        if norm["schedule"]["kind"] == "watch" and not norm["last_sig"]:
            norm["last_sig"] = sched.folder_signature(norm["schedule"]["path"])
        for i, t in enumerate(tasks):
            if t.get("id") == norm["id"]:
                tasks[i] = norm
                break
        else:
            if len(tasks) >= sched.MAX_TASKS:
                return {"error": "You have the maximum number of scheduled tasks."}
            tasks.append(norm)
        save_config(self._cfg)
        return self.scheduled_tasks()

    def delete_scheduled_task(self, task_id: str):
        self._cfg.scheduled_tasks = [t for t in self._cfg.scheduled_tasks
                                     if t.get("id") != task_id]
        save_config(self._cfg)
        return self.scheduled_tasks()

    def set_scheduled_enabled(self, task_id: str, enabled: bool):
        for t in self._cfg.scheduled_tasks:
            if t.get("id") == task_id:
                t["enabled"] = bool(enabled)
        save_config(self._cfg)
        return self.scheduled_tasks()

    def run_scheduled_task_now(self, task_id: str):
        for t in self._cfg.scheduled_tasks:
            if t.get("id") == task_id:
                self._fire_scheduled_task(t)
                t["last_run"] = time.time()
                save_config(self._cfg)
                return {"ok": True}
        return {"error": "task not found"}

    def pick_task_folder(self):
        try:
            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            picked = None
        if not picked:
            return {"cancelled": True}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        return {"path": str(path)}

    def _install_neural_memory(self) -> None:
        """Background pip-install of the local embedding model package the first
        time neural code search is turned on. Until it's ready, search_code just
        uses the lexical index, so nothing breaks meanwhile."""
        ev = self._events

        def work():
            import sys as _sys
            from ..tools import NO_WINDOW_KWARGS
            try:
                ev.toast("Setting up semantic code search (one-time model download)…", "info")
                proc = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", "--user", "--upgrade",
                     "sentence-transformers"],
                    capture_output=True, text=True, timeout=900, **NO_WINDOW_KWARGS)
                from .. import codebase_memory
                if proc.returncode == 0 and codebase_memory.NeuralEmbedder.packages_installed():
                    ev.toast("Semantic code search is ready.", "info")
                else:
                    ev.toast("Couldn't install the embedding model; using keyword search "
                             "instead. You can turn this off in Settings.", "warn")
            except Exception:
                ev.toast("Couldn't set up semantic code search; using keyword search.", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _scheduler_loop(self) -> None:
        from .. import scheduler as sched
        while not self._sched_stop.wait(30):
            # Turns a phone was suspended part-way through. In its own try, and
            # before the early `continue` below: a machine with no scheduled
            # tasks is still the machine that can finish the phone's work.
            try:
                self.sync_finish_interrupted()
            except Exception:
                pass   # offline, or sync off — nothing to report every 30s
            try:
                tasks = self._cfg.scheduled_tasks
                if not tasks:
                    continue
                now = time.time()
                dirty = False
                for t in tasks:
                    kind = t.get("schedule", {}).get("kind")
                    sig = (sched.folder_signature(t["schedule"]["path"])
                           if kind == "watch" else None)
                    if t.get("enabled", True) and sched.is_due(t, now, sig):
                        self._fire_scheduled_task(t)
                        t["last_run"] = now
                        if kind == "watch":
                            t["last_sig"] = sig
                        dirty = True
                    elif kind == "watch" and sig and not t.get("last_sig"):
                        t["last_sig"] = sig   # establish the baseline
                        dirty = True
                if dirty:
                    save_config(self._cfg)
            except Exception:
                pass   # a scheduler hiccup must never take the app down

    def _fire_scheduled_task(self, task: dict) -> None:
        """Run a task's prompt headlessly in its project folder as a background
        chat (shows up in the sidebar), then notify. Best-effort."""
        cwd = task.get("cwd", "")
        if not cwd or not Path(cwd).is_dir():
            return
        client = self._ensure_client()
        if client is None:
            return
        sid = new_id()
        workdir = Path(cwd)
        events = self._make_events(sid)
        agent = Agent(self._cfg, client, events=events, workdir=workdir)
        agent.mcp = self._mcp
        cs = ChatState(sid, agent, events)
        cs.title = (task.get("name") or "Scheduled task")[:60]
        self._chats[sid] = cs
        if backup_module.available():
            cs.backup_repo = BackupRepo(sid, workdir)
            agent.backup_repo = cs.backup_repo
        agent.transcript = Transcript(sid, cwd=str(workdir))
        agent.rebuild_system_prompt()

        def work():
            try:
                agent.run_turn({"role": "user", "content": task["prompt"]})
            except Exception as e:
                events.error(f"scheduled task failed: {e}")
            finally:
                self._save_chat(cs)
                try:
                    self._maybe_autopush(cs)
                    self._maybe_autosync(cs.sid)
                except Exception:
                    pass
                try:
                    events.emit("bg_refresh", sessions=self.list_sessions())
                except Exception:
                    pass
                notify(APP_NAME, f"Scheduled task “{cs.title}” finished.")
        threading.Thread(target=work, daemon=True).start()

    def open_session(self, sid: str):
        # Live chats (running or not) switch instantly; others load from disk.
        if sid in self._chats:
            res = self._switch_to_live(sid)
            res["sessions"] = self.list_sessions()
            return res
        data = self._store.load(sid)
        if not data:
            return {"error": "session not found"}
        res = self._activate_session(
            sid, data.get("messages", []), data.get("cwd", ""),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("todos", []), data.get("title", ""),
            auto_backup=data.get("auto_backup", True),
            model_provider=data.get("model_provider", ""),
            model=data.get("model", ""),
            turn_snapshots=data.get("turn_snapshots", []),
        )
        res["sessions"] = self.list_sessions()
        return res

    def delete_session(self, sid: str):
        live = self._chats.get(sid)
        if live and live.agent.busy:
            return {"error": "that chat is still working — stop it first"}
        if live:
            live.agent.close_browser()  # don't leak a control_chrome window
        self._chats.pop(sid, None)
        self._store.delete(sid)
        Transcript(sid).delete()  # its transcript goes with it
        closed_active = sid == self.session_id
        if closed_active:
            self.session_id = None
            if self._cfg.last_session_id == sid:
                self._cfg.last_session_id = ""
                save_config(self._cfg)
        return {"ok": True, "sessions": self.list_sessions(), "closed_active": closed_active}

    def _save_chat(self, cs: "ChatState") -> None:
        """Persist ONE chat -- callable from its own turn thread, so a
        background chat saves itself without touching the active one."""
        u = cs.agent.session_usage
        self._store.save(cs.sid, str(cs.agent.workdir), cs.agent.messages,
                         u.prompt_tokens, u.completion_tokens,
                         todos=cs.agent.todos, title=cs.title,
                         auto_backup=cs.auto_backup,
                         model_provider=cs.provider, model=cs.model,
                         turn_snapshots=cs.turn_snapshots)

    def _save_current(self) -> None:
        if self._active:
            self._save_chat(self._active)

    # -- backups (per-chat shadow git repo) --------------------------------- #

    def backup_status(self):
        available = backup_module.available()
        snapshots = []
        if available and self._backup_repo:
            snapshots = [
                {"commit": s.commit, "message": s.message, "timestamp": s.timestamp}
                for s in reversed(self._backup_repo.list_snapshots())
            ]
        return {"available": available, "enabled": self.auto_backup, "snapshots": snapshots}

    def set_backup_enabled(self, enabled: bool):
        self.auto_backup = bool(enabled)
        self._save_current()
        return {"ok": True}

    def turn_changes(self):
        """Per-file changes since the pre-turn snapshot, for the review card
        shown after each turn. Empty when backups are off (no baseline)."""
        if not (self.auto_backup and self._backup_repo):
            return {"files": []}
        try:
            return {"files": self._backup_repo.turn_changes()}
        except Exception:
            return {"files": []}

    def revert_change(self, path: str):
        """Revert ONE file to its pre-turn state (from the review card)."""
        if not self._backup_repo:
            return {"error": "no active chat"}
        if self._agent and self._agent.busy:
            return {"error": "can't revert while the agent is working"}
        try:
            self._backup_repo.revert_file(path)
        except Exception as e:
            return {"error": str(e)}
        return self.turn_changes()

    def revert_backup(self, commit: str):
        if not self._backup_repo:
            return {"error": "no active chat"}
        if self._agent and self._agent.busy:
            return {"error": "can't revert while the agent is working"}
        try:
            self._backup_repo.revert_to(commit)
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}

    def fork_at(self, turn_ordinal):
        """Branch the conversation at one of your past messages.

        The history underneath has always been a TREE and was only ever offered
        as a line: backup.py commits a snapshot of the work tree before every
        user turn, and rewind_to reverts to one -- discarding everything after
        it, permanently. For an agent that is wrong a fair share of the time,
        "try it both ways and compare" is a better primitive than undo, and the
        storage for it is already being written.

        So this keeps the original chat untouched and opens a NEW one holding
        the conversation up to that point, with the project files reverted to
        the same snapshot. The two then diverge.

        `turn_ordinal` is the message's send-turn number, resolved to an
        absolute position through the same display mapping the JS sees -- the
        same contract rewind_to uses, so the two cannot disagree about which
        message was meant.
        """
        cs = self._active
        if not cs:
            return {"error": "no active chat"}
        if cs.agent.busy:
            return {"error": "can't fork a chat while the agent is working"}
        try:
            turn_ordinal = int(turn_ordinal)
        except (TypeError, ValueError):
            return {"error": "bad message reference"}

        msg_index = next((it["msg_index"] for it in to_display(cs.agent.messages)
                          if it.get("kind") == "user"
                          and it.get("turn_ordinal") == turn_ordinal), None)
        if msg_index is None or not (0 <= msg_index < len(cs.agent.messages)) \
                or cs.agent.messages[msg_index].get("role") != "user":
            return {"error": "that message is no longer available"}

        # Everything BEFORE the chosen message: the fork starts where that turn
        # was about to be sent, so its own text is available to retype or edit
        # rather than already spent.
        history = [dict(m) for m in cs.agent.messages[:msg_index]]
        snapshots = [dict(t) for t in cs.turn_snapshots[:turn_ordinal]]
        parent_title = cs.title or "Chat"
        source_sid = cs.sid

        # Revert the FILES before the new chat's BackupRepo is built, so its
        # first snapshot records the state the fork actually starts from
        # rather than the parent's latest.
        commit = (cs.turn_snapshots[turn_ordinal]["commit"]
                  if 0 <= turn_ordinal < len(cs.turn_snapshots) else None)
        reverted = False
        if commit and cs.backup_repo:
            try:
                cs.backup_repo.revert_to(commit)
                reverted = True
            except Exception as e:
                return {"error": f"couldn't revert the project files: {e}"}

        payload = self._activate_session(
            new_id(), history, str(cs.agent.workdir),
            0, 0, [], title=f"{parent_title} (fork)",
            auto_backup=cs.auto_backup,
            model_provider=cs.provider, model=cs.model,
            turn_snapshots=snapshots)
        if payload.get("error"):
            return payload
        payload["forked_from"] = source_sid
        payload["reverted"] = reverted
        payload["had_snapshot"] = bool(commit)
        payload["sessions"] = self.list_sessions()
        return payload

    def rewind_to(self, turn_ordinal):
        """Edit & resend: rewind the active chat to just before one of your
        past messages -- revert the project files to that turn's pre-turn
        snapshot and truncate the conversation there -- so the JS can re-send
        the edited text as a fresh turn. `turn_ordinal` is the message's
        send-turn number (its position among your messages, counting from 0);
        the absolute truncation point is resolved from it here, so the JS only
        has to count user bubbles -- no per-bubble bookkeeping to drift."""
        cs = self._active
        if not cs:
            return {"error": "no active chat"}
        if cs.agent.busy:
            return {"error": "can't edit a message while the agent is working"}
        agent = cs.agent
        try:
            turn_ordinal = int(turn_ordinal)
        except (TypeError, ValueError):
            return {"error": "bad message reference"}
        # Resolve the turn ordinal to an absolute message position via the same
        # display mapping the JS sees, so the two can't disagree.
        msg_index = next((it["msg_index"] for it in to_display(agent.messages)
                          if it.get("kind") == "user"
                          and it.get("turn_ordinal") == turn_ordinal), None)
        if msg_index is None or not (0 <= msg_index < len(agent.messages)) \
                or agent.messages[msg_index].get("role") != "user":
            return {"error": "that message is no longer available"}

        # Revert files to how they looked right before this turn ran, if we
        # have that snapshot (backups may have been off for it, or it predates
        # a compaction that cleared the map).
        reverted = False
        had_snapshot = 0 <= turn_ordinal < len(cs.turn_snapshots)
        commit = cs.turn_snapshots[turn_ordinal]["commit"] if had_snapshot else None
        if commit and cs.backup_repo:
            try:
                cs.backup_repo.revert_to(commit)
                reverted = True
            except Exception as e:
                return {"error": f"couldn't revert the project files: {e}"}

        # Rewind the conversation and the snapshot map to this point. The JS
        # re-sends the edited text next, which appends a fresh turn (and a
        # fresh snapshot) from here.
        del agent.messages[msg_index:]
        del cs.turn_snapshots[turn_ordinal:]
        agent.todos = []  # any checklist from the undone turns is stale now
        self._save_chat(cs)
        payload = self._session_payload(cs)
        payload["reverted"] = reverted
        payload["had_snapshot"] = bool(commit)
        return payload

    def _generate_title(self, first_message: str) -> str:
        """Ask the model for a short chat name from the first user message.
        Best-effort: any failure just falls back to the derived title."""
        client = self._ensure_client()
        if not client or not first_message.strip():
            return ""
        try:
            res = client.chat(
                model=cfg_default_model(self._cfg),
                messages=[{"role": "user",
                           "content": TITLE_PROMPT.format(message=first_message[:2000])}],
                tools=None, temperature=0.3, max_tokens=24, thinking=False,
            )
            title = " ".join((res.content or "").split()).strip().strip('"\'').rstrip(".")
            return title[:64]
        except Exception:
            return ""

    # -- attachments ------------------------------------------------------ #

    def pick_files(self):
        """Pick any file(s) to attach -- not just images. Copied into the
        project's uploads/ folder on send (see Agent.attach_files); only
        image files get a real thumbnail here, others show a generic icon."""
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("All files (*.*)",),
        )
        if not picked:
            return []
        out = []
        for p in picked:
            path = Path(p)
            if not path.is_file():
                continue
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS
            out.append({"path": str(path), "name": path.name,
                        "thumb": _thumb_uri(path) if is_image else ""})
        return out

    def attach_paths(self, paths: list):
        """Turn dropped-file paths (drag & drop onto the window) into the
        same attachment shape pick_files returns, so the composer pipeline
        treats them identically."""
        out = []
        for p in paths or []:
            path = Path(str(p))
            if not path.is_file():
                continue
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS
            out.append({"path": str(path), "name": path.name,
                        "thumb": _thumb_uri(path) if is_image else ""})
        return out

    def paste_image(self, data_url: str):
        """A screenshot pasted into the composer (Win+Shift+S -> Ctrl+V)
        arrives as a base64 data URL from the JS paste handler. Save it to a
        real file under ~/.makenomistakes/pasted/ so it flows through the
        exact same attachment -> uploads/ pipeline as picked/dropped files."""
        try:
            head, _, b64 = str(data_url or "").partition(",")
            if not b64 or not head.startswith("data:image/"):
                return {"error": "Clipboard did not contain an image."}
            ext = {"data:image/png": ".png", "data:image/jpeg": ".jpg",
                   "data:image/gif": ".gif", "data:image/webp": ".webp",
                   "data:image/bmp": ".bmp"}.get(head.split(";")[0], ".png")
            raw = base64.b64decode(b64)
            if len(raw) > 30_000_000:
                return {"error": "Pasted image is too large (>30MB)."}
            folder = CONFIG_DIR / "pasted"
            folder.mkdir(parents=True, exist_ok=True)
            name = time.strftime("pasted-%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}{ext}"
            path = folder / name
            path.write_bytes(raw)
            return {"path": str(path), "name": name, "thumb": _thumb_uri(path)}
        except Exception as e:
            return {"error": f"Couldn't save pasted image: {e}"}

    def _on_drop(self, event):
        """Native file drop handler (bound in main() to
        window.dom.document.events.drop). pywebview resolves each dropped
        file's real disk path into pywebviewFullPath on the PYTHON-side event
        only -- JS can't see it -- so the actual attaching happens here, then
        we hand the resolved attachments back to the page."""
        try:
            files = ((event or {}).get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files
                     if isinstance(f, dict) and f.get("pywebviewFullPath")]
            atts = self.attach_paths(paths) if paths else []
            if self._window:
                self._window.evaluate_js(
                    "window.__onDropResult(" + json.dumps(atts) + ")")
        except Exception:
            pass

    def list_project_files(self, query: str = ""):
        """Fuzzy file search in the active chat's project, for the composer's
        @-mention picker. Fast (the file list is cached per folder)."""
        cs = self._active
        if not cs:
            return {"files": []}
        try:
            files = tools_search_project_files(cs.agent.workdir, query or "", limit=30)
        except Exception:
            files = []
        return {"files": files}

    # -- chat ---------------------------------------------------------- #

    def send(self, text: str, file_paths: list | None = None, plan: bool = False,
             force: bool = False):
        """Start a turn in the ACTIVE chat and return immediately -- the turn
        runs on its own thread, so the user can switch to (or create) other
        chats while it works. Completion arrives as a "turn_complete" event
        tagged with the chat's sid.

        If this chat is synced and another device holds a live lock on it,
        returns {"locked": True, ...} instead of starting the turn (nothing is
        appended to the chat) -- pass force=True to send anyway."""
        cs = self._active
        if cs is None:
            return {"error": "no active chat — start a New Chat first"}
        text = (text or "").strip()
        paths = [Path(p) for p in (file_paths or []) if Path(p).is_file()]
        if not text and not paths:
            return {"error": "empty"}
        if not cs.turn_lock.acquire(blocking=False):
            return {"error": "busy"}
        lock_result = self._try_acquire_device_lock(cs.sid, force=force)
        if lock_result is not None:
            cs.turn_lock.release()
            return lock_result
        threading.Thread(target=self._run_send_turn,
                         args=(cs, text, paths, plan), daemon=True).start()
        return {"ok": True, "started": True}

    def _run_send_turn(self, cs: "ChatState", text: str, paths: list,
                       plan: bool) -> None:
        """The body of one chat turn, on its own thread. Everything here uses
        `cs`, never the active-chat accessors -- the user may be looking at a
        completely different chat by the time this finishes."""
        agent, events = cs.agent, cs.events
        raw_text = text  # original user text, for title generation
        ok = False
        try:
            events.emit("chat_busy")
            # Anything a voice-dispatched worker reported while this agent was
            # idle, and any spoken exchange that landed while it was busy. Done
            # here, under the turn lock, because this is the one moment its
            # history is not being written by anyone else.
            self._drain_worker_reports(cs)
            self._drain_voice_turns(cs)
            # @-mentioned files. Two things happen here: the "@" is stripped from
            # every mention that resolves to a real file (so the model gets a
            # clean path like generated/x.jpg, not "@generated/x.jpg" which it
            # would try to open literally), text files have their contents
            # inlined, and image files are collected to embed (direct mode) or
            # left as a clean path for view_image (describe mode). Best-effort.
            mention_images: list = []
            try:
                mentions = tools_resolve_mentions(agent.workdir, text)
                for mn in mentions:
                    text = text.replace("@" + mn["token"], mn["rel"])
                text_files = [(mn["rel"], mn["path"]) for mn in mentions
                              if not mn["is_image"]]
                mention_images = [mn["path"] for mn in mentions if mn["is_image"]]
                file_ctx = tools_build_text_file_context(text_files)
            except Exception:
                file_ctx = ""
            if plan and text:
                # Read-only planning turn: the preamble sets expectations and
                # permissions.plan_only (below) makes them non-negotiable.
                text = PLAN_MODE_PREAMBLE.format(text=text)
            # File contents are appended after the marker so to_display keeps
            # them off the on-screen message.
            if file_ctx:
                text = text + file_ctx
            if paths or mention_images:
                msg = agent.attach_uploads(text, paths, embed_images=mention_images)
            else:
                msg = {"role": "user", "content": text}
            agent.permissions.plan_only = bool(plan and text)
            # File backup: commit the project's current state (i.e. how it
            # looked right before this message's own edits) so "revert to
            # here" later can put it back. Best-effort -- a backup failure
            # must never block sending a message. One turn_snapshots entry is
            # recorded PER turn regardless (commit None when backups are off),
            # so its index stays the turn ordinal that "edit & resend" uses.
            commit = None
            if cs.auto_backup and cs.backup_repo:
                try:
                    # Visible in the status chip: on a big project the git
                    # snapshot can take a moment, and silent pre-turn latency
                    # reads as "the app is slow" rather than "it's working".
                    with events.status("backing up project files..."):
                        commit = cs.backup_repo.snapshot(text or "(files attached)")
                except Exception as e:
                    events.warn(f"backup snapshot failed: {e}")
            cs.turn_snapshots.append({"commit": commit})
            # Snapshot the read-aloud toggle for this turn only: if it's off
            # right now, TTS is never touched below, even if the user flips
            # it mid-response; if it's on, it stays on for this whole turn
            # regardless of later toggling.
            events.start_turn(self._cfg.read_aloud)
            agent.run_turn(msg)
            # First turn of a fresh chat: let the model name it for the sidebar.
            if not cs.title and raw_text:
                t = self._generate_title(raw_text)
                if t:
                    cs.title = t
                    if agent.transcript:
                        # searchable by topic, not just by session id
                        agent.transcript.set_title(t)
            ok = True
        except Exception as e:
            events.error(f"{type(e).__name__}: {e}")
        finally:
            agent.permissions.plan_only = False  # never outlive the turn
            self._save_chat(cs)
            cs.turn_lock.release()
            u = agent.session_usage
            events.emit("turn_complete", ok=ok, plan=bool(plan),
                        prompt_tokens=u.prompt_tokens,
                        completion_tokens=u.completion_tokens,
                        context=agent.context_estimate(),
                        title=cs.title, sessions=self.list_sessions())
            self._maybe_autopush(cs)  # background commit+push if connected
            # then_unlock=True: harmless no-op if this turn never held the
            # cross-device lock (sync off, or the acquire failed open).
            self._maybe_autosync(cs.sid, then_unlock=True)
            self._os_attention(cs.sid, "Done -- waiting for you."
                               if ok else "Stopped on an error -- waiting for you.")

    def execute_plan(self):
        """The 'Execute plan' button: a normal (non-plan) turn with a canned
        instruction to carry out the plan the user just approved."""
        return self.send(EXECUTE_PLAN_MESSAGE)

    def cancel(self):
        if self._agent:
            self._agent.request_cancel()
        return {"ok": True}

    def stop_powershell(self, call_id: str):
        """Stop one blocking shell command (the Stop button on its chat box)
        by killing its process tree, so the agent's turn -- stuck waiting on
        a command that never exits, like a dev server -- unblocks at once.
        Process-global registry keyed by unique per-call tokens, so this
        reaches the right command even across parallel chats."""
        from ..tools import stop_foreground
        return {"ok": bool(stop_foreground(call_id or ""))}

    def steer(self, text: str):
        text = (text or "").strip()
        if not text:
            return {"error": "empty"}
        if not self._agent or not self._agent.busy:
            return {"error": "nothing running to steer"}
        if not self._agent.steer(text):
            return {"error": "a steering message is already queued"}
        return {"ok": True}

    def steer_clear(self):
        if self._agent:
            self._agent.clear_steer()
        return {"ok": True}

    def steer_subagent(self, aid: str, text: str):
        text = (text or "").strip()
        if not text:
            return {"error": "empty"}
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.steer_subagent(aid, text):
            return {"error": "that sub-agent is no longer running, or already has a queued message"}
        return {"ok": True}

    def steer_subagent_clear(self, aid: str):
        if self._agent:
            self._agent.clear_steer_subagent(aid)
        return {"ok": True}

    def wrapup_subagent(self, aid: str):
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.wrapup_subagent(aid):
            return {"error": "that sub-agent is no longer running"}
        return {"ok": True}

    def set_active_view(self, view: str = ""):
        """Tell read-aloud which live stream to read from: '' for the main
        chat, or a sub-agent's id while its inspector panel is focused on it.
        The frontend calls this on every panel open/switch/close."""
        self._events.set_active_view(view or "")
        return {"ok": True}

    def set_browser_model(self, provider_name: str, model: str):
        """Pick the dedicated Browser Agent model ('' + '' = same as chat).
        Driving a page is the hardest thing the small free model does, so
        routing just control_chrome to a stronger configured model is the
        single biggest browsing-reliability lever."""
        provider_name = (provider_name or "").strip()
        model = (model or "").strip()
        if provider_name and not find_provider(self._cfg, provider_name):
            return {"error": f'unknown provider "{provider_name}"'}
        self._cfg.browser_provider = provider_name
        self._cfg.browser_model = model if provider_name else ""
        save_config(self._cfg)
        return {"ok": True, "browser_provider": self._cfg.browser_provider,
                "browser_model": self._cfg.browser_model}

    def browser_extension_status(self):
        """Everything the Browser panel needs, in one call.

        The port is opened while this panel is on screen even if the feature is
        off, so someone can install the extension and SEE it connect before
        deciding to turn it on. Verifying first is the right order for a switch
        that hands the agent a logged-in browser.
        """
        from .. import browser_extension, installed_browsers
        st = browser_extension.status(self._cfg, listen=True)
        st["path"] = str(EXTENSION_DIR)
        st["installed"] = EXTENSION_DIR.joinpath("manifest.json").is_file()
        st["browsers"] = installed_browsers.find()
        # What it would actually act on, so "my own browser" is a specific
        # window rather than a leap of faith. Cheap, and only when connected.
        st["tab"] = {}
        if st["connected"]:
            try:
                b = browser_extension.bridge(start=False)
                st["tab"] = b.call("status", timeout=4) or {}
            except Exception:
                st["tab"] = {}
        return st

    def open_extensions_page(self, path: str = ""):
        """Kept so an older page calling it gets an answer, not an exception.

        It no longer opens anything. One program cannot raise another's window
        without platform-specific APIs, and both attempts at faking it made the
        feature look broken: passing chrome://extensions on the command line
        got the URL dropped and an empty window opened, and launching with no
        argument opens a fresh blank window on Windows.
        """
        from .. import installed_browsers
        return {"error": installed_browsers.open_browser(path)[1],
                "url": installed_browsers.EXTENSIONS_URL}

    def open_extension_folder(self):
        """Reveal the extension folder in the OS file manager, so 'Load
        unpacked' has somewhere to be pointed at without anyone typing a path."""
        if not EXTENSION_DIR.is_dir():
            return {"error": f"The extension folder is missing: {EXTENSION_DIR}"}
        return self.open_path(str(EXTENSION_DIR))

    def browser_attach_check(self, url: str = ""):
        """Is a browser actually listening on that DevTools endpoint?

        Every browser with the port open answers /json/version with the
        product string, so this turns "attach" from a setting you enable and
        then find out about ten minutes later, mid-task, into one you can
        confirm before you rely on it. The hint is returned either way,
        because "nothing is listening" and "here is the command that makes
        something listen" are the same conversation.
        """
        import json as _json
        import urllib.error
        import urllib.request
        from ..browser_session import DEBUG_PORT_HINT

        target, err = _normalize_connect_url(url or self._cfg.browser_connect_url)
        if err:
            return {"error": err, "hint": DEBUG_PORT_HINT}
        if not target:
            return {"error": "No endpoint set.", "hint": DEBUG_PORT_HINT}
        try:
            with urllib.request.urlopen(target + "/json/version", timeout=3) as r:
                info = _json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            return {"ok": False, "url": target, "hint": DEBUG_PORT_HINT,
                    "error": f"Nothing answered at {target} ({e.reason})."}
        except Exception as e:
            return {"ok": False, "url": target, "hint": DEBUG_PORT_HINT,
                    "error": f"{target} answered, but not like a browser: {e}"}
        return {"ok": True, "url": target,
                "browser": str(info.get("Browser") or "a browser"),
                "hint": DEBUG_PORT_HINT}

    def clear_browser_profile(self):
        """Delete the saved agent-browser profile (cookies, logins). The
        escape hatch that keeps 'Remember browser logins' from being a
        one-way door. Refuses while any chat's browser is open on it."""
        for cs in self._chats.values():
            sess = getattr(cs.agent, "browser_session", None)
            if sess is not None and sess.is_open:
                return {"error": "a chat's browser is still open — close it first"}
        p = CONFIG_DIR / "browser-profile"
        try:
            if p.exists():
                shutil.rmtree(p)
        except OSError as e:
            return {"error": f"couldn't delete the profile: {e}"}
        return {"ok": True}

    def pause_browser(self):
        """Freeze the running Browser Agent so the user can take over the
        browser window; resume_browser continues the same agent."""
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.pause_browser_agent():
            return {"error": "no browser agent is running"}
        return {"ok": True}

    def resume_browser(self):
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.resume_browser_agent():
            return {"error": "no browser agent is running"}
        return {"ok": True}

    def permission_response(self, rid: str, answer: str, feedback: str = ""):
        self._events.resolve_permission(rid, answer, feedback)
        return {"ok": True}

    def clear_chat(self):
        """Start a fresh chat in the same project folder; the old conversation
        stays in history (nothing is discarded)."""
        if self._agent and self._agent.busy:
            return {"error": "busy"}
        if not self.session_id:
            return {"error": "no active chat"}
        cwd = str(self._agent.workdir) if self._agent else str(Path.cwd())
        # Don't delete the old session — it stays in the sidebar as history
        res = self._activate_session(new_id(), [], cwd, 0, 0, [])
        res["sessions"] = self.list_sessions()  # refresh sidebar
        return res

    def compact_chat(self):
        if not self._agent or self._agent.busy:
            return {"error": "busy or not ready"}
        try:
            note = self._agent.compact()
            self._save_current()
            return {"ok": True, "note": note, "sessions": self.list_sessions(),
                    "context": self._agent.context_estimate()}
        except Exception as e:
            return {"error": str(e)}

    def usage(self):
        if not self._agent:
            return {"prompt_tokens": 0, "completion_tokens": 0, "context": 0}
        u = self._agent.session_usage
        return {"prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "context": self._agent.context_estimate()}


# --------------------------------------------------------------------- #

def _show_error(title: str, message: str) -> None:
    """Show a visible error dialog even under pythonw (no console)."""
    try:
        import tkinter.messagebox as mb
        mb.showerror(title, message)
    except Exception:
        try:
            from pathlib import Path
            (Path.home() / ".makenomistakes" / "crash.log").write_text(
                f"{title}\n\n{message}", encoding="utf-8"
            )
        except OSError:
            pass


GUI_DIR = Path(__file__).parent          # glmcode/gui/
ICO_PATH = GUI_DIR / "app_icon.ico"     # pre-built, ships with package

STARTUP_LOG = Path.home() / ".makenomistakes" / "startup.log"


def _startup_log(stage: str) -> None:
    """Append a timestamped breadcrumb so a silent startup hang is locatable.

    A "not responding" freeze prints no traceback, so we can't rely on the
    crash handler. Instead each startup stage drops a line here; whatever
    stage is *last* in the file is where it hung. The file is truncated at
    the start of every launch so it always reflects the most recent run.
    """
    try:
        from datetime import datetime
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STARTUP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {stage}\n")
    except OSError:
        pass


def main():
    # Fresh breadcrumb trail for this launch (see _startup_log).
    try:
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    _startup_log(f"main() start  platform={sys.platform}  python={sys.version.split()[0]}")

    # Give the process its own taskbar identity. Without an explicit
    # AppUserModelID, Windows groups the window under pythonw.exe and can't
    # attach our window icon to the taskbar button (it shows a blank/generic
    # icon). Setting this makes the window's own icon appear on the taskbar.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MakeNoMistakes.DesktopApp"
            )
            _startup_log("set AppUserModelID")
        except Exception as e:
            _startup_log(f"AppUserModelID failed: {e}")

    # Verify the web assets exist (common issue if files are missing)
    index = WEB_DIR / "index.html"
    if not index.is_file():
        _startup_log(f"ABORT missing web assets: {index}")
        _show_error("Make No Mistakes",
                    f"Missing web assets: {index}\n"
                    f"Make sure the glmcode/gui/web/ folder was extracted correctly.")
        return

    _startup_log("creating Api()")
    api = Api()

    _startup_log("creating window")
    window = webview.create_window(
        title="Make No Mistakes",
        url=str(index),
        js_api=api,
        width=1240,
        height=820,
        min_size=(880, 600),
        frameless=True,
        easy_drag=False,
        background_color="#0a0d16",
    )
    api._window = window
    api._events_global._window = window
    for cs in api._chats.values():  # chats created before the window existed
        cs.events._window = window

    # Native file drag & drop: the dropped files' real disk paths are only
    # available in pywebview's PYTHON-side drop event (as pywebviewFullPath),
    # so bind a Python handler once the DOM is ready. Best-effort -- older
    # pywebview without DOM-event support just leaves the paperclip button.
    def _bind_drop(w=window):
        try:
            from webview.dom import DOMEventHandler
            w.dom.document.events.drop += DOMEventHandler(
                api._on_drop, prevent_default=True, stop_propagation=False)
            _startup_log("native drop handler bound")
        except Exception as e:
            _startup_log(f"native drop unavailable: {type(e).__name__}: {e}")
    try:
        window.events.loaded += _bind_drop
    except Exception as e:
        _startup_log(f"could not subscribe loaded for drop: {e}")

    # Build webview.start() kwargs
    start_kwargs = dict(debug="--debug" in sys.argv)

    # Deliberately NOT setting a persistent storage_path: a WebView2 profile
    # that survives across launches can keep serving stale cached copies of
    # index.html/app.js/style.css from before a code update, causing DOM/JS
    # version-skew (e.g. app.js referencing an element index.html hasn't
    # added yet), which throws and can silently kill this app's boot
    # sequence with no visible error. The default per-launch temp profile
    # avoids that entirely at the cost of not reusing WebView2's cache.

    if sys.platform == "win32":
        # Force EdgeChromium backend — skip auto-detection which can cause
        # "not responding" hangs during startup on some Windows installs.
        start_kwargs["gui"] = "edgechromium"
        # Disabling GPU acceleration avoids hangs when WebView2's GPU process
        # stalls (older GPUs, VMs, remote desktop, flaky drivers). Use ONLY
        # --disable-gpu: it falls back to software rendering. Do NOT also pass
        # --disable-software-rasterizer, which removes that fallback and can
        # leave the window blank. setdefault() lets a user override the flags.
        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu",
        )
    # Set icon via start() (pywebview 5.x/6.x — icon is NOT a create_window param)
    if ICO_PATH.is_file():
        start_kwargs["icon"] = str(ICO_PATH.resolve())

    _startup_log(
        "calling webview.start  "
        f"gui={start_kwargs.get('gui', 'auto')}  "
        f"flags={os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '')!r}  "
        f"storage={start_kwargs.get('storage_path', '(default)')}"
    )

    try:
        webview.start(**start_kwargs)
        _startup_log("webview.start returned (window closed normally)")
    except Exception as e:
        _startup_log(f"webview.start raised {type(e).__name__}: {e}")
        _show_error("Make No Mistakes - webview failed",
                    f"{type(e).__name__}: {e}\n\n"
                    f"Make sure WebView2 is installed:\n"
                    f"https://developer.microsoft.com/en-us/microsoft-edge/webview2/\n\n"
                    f"Or run this in a terminal to see the full error:\n"
                    f"  python -m glmcode.gui --debug")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        _show_error("Make No Mistakes crashed", traceback.format_exc())

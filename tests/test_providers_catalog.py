"""The provider catalogue: presets, and the config plumbing under them.

z.ai used to be wired in as *the* provider -- a DEFAULT_BASE_URL, a ZAI_API_KEY
environment variable, and a provider hardcoded to the name "z.ai (free)"
whatever it actually pointed at. These cover the two things that had to become
true instead: that a preset is only a label and some instructions, and that
adding a second one cannot break the first.
"""

import types

from glmcode import config as cfgmod
from glmcode import providers


def _cfg(**kw):
    # Through the constructor, not by assignment afterwards: a Config folds the
    # setup provider into its provider list on construction, so a base_url set
    # later would describe an endpoint no row exists for.
    return cfgmod.Config(**kw)


def _setup_row(cfg):
    """The provider row setup created -- an ordinary row like any other now.

    It used to be fabricated on demand by builtin_provider() out of the loose
    cfg.base_url / cfg.model / cfg.vision_model fields, which is exactly what
    made it a different kind of object from every other provider.
    """
    return cfgmod.all_providers(cfg)[0]


# ---- the presets themselves ------------------------------------------------

def test_every_preset_can_actually_be_used():
    """A preset that is missing any of these is not a shortcut, it is a dead
    end the user has to work around."""
    for p in providers.PRESETS:
        assert p["key"] and p["label"]
        # https for anything hosted. A server on this machine is the exception
        # and the only one: Ollama speaks plain http on localhost, and there is
        # no certificate it could present for it.
        if providers.is_local(p["base_url"]):
            assert p["base_url"].startswith("http://"), p["key"]
        else:
            assert p["base_url"].startswith("https://"), p["key"]
        # A preset normally names the model it starts on. A local one cannot:
        # which models exist depends on what has been pulled onto the machine,
        # so it starts on nothing and reads the list off the running server.
        if p["model"]:
            assert p["model"] in p["models"], \
                f"{p['key']} starts on a model it does not list"
        else:
            assert p.get("needs_key") is False, \
                f"{p['key']} names no model but is not a local server"
            assert p.get("suggest_pull"), \
                f"{p['key']} has no model and nothing to suggest pulling"
        assert p["key_url"].startswith("https://"), p["key"]
        assert p["steps"], f"{p['key']} tells you nothing about getting a key"
        # Somewhere to put a key, or an explicit statement that it needs none.
        assert providers.env_var_for(p["key"]) or p.get("needs_key") is False, \
            f"{p['key']} has nowhere to put its key"


def test_presets_do_not_share_an_environment_variable():
    """Sharing one would mean configuring the second provider silently
    overwrites the first one's key."""
    # "" means "no key to hold" (Ollama, Skoolie) and may repeat.
    names = [n for n in (providers.env_var_for(p["key"])
                         for p in providers.PRESETS) if n]
    assert len(set(names)) == len(names)


def test_google_points_at_the_openai_compatible_endpoint():
    """The native generateContent API is a different protocol and would need a
    client of its own; this app only speaks /chat/completions."""
    g = providers.preset("google")
    assert g["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_google_says_plainly_that_free_prompts_train_the_model():
    """The one thing about this option someone might mind, and would otherwise
    find out afterwards. This app sends source code."""
    g = providers.preset("google")
    assert g["caveat"], "the free-tier training caveat is missing"
    assert "free" in g["caveat"].lower()


def test_a_preset_becomes_an_ordinary_provider():
    """Nothing downstream should be able to tell a preset from a typed-in one."""
    prov = providers.to_provider("google", "KEY123")
    assert prov["base_url"] == providers.preset("google")["base_url"]
    assert prov["api_key"] == "KEY123"
    assert prov["models"] == providers.preset("google")["models"]
    assert set(prov) >= {"name", "base_url", "api_key", "models"}


def test_an_unknown_preset_is_none_rather_than_a_broken_provider():
    assert providers.to_provider("nope") is None
    assert providers.preset("nope") is None


def test_the_setup_screen_is_offered_every_preset_plus_other():
    keys = [c["key"] for c in providers.choices()]
    assert keys[:len(providers.PRESETS)] == providers.preset_keys()
    assert keys[-1] == providers.CUSTOM_KEY
    for c in providers.choices():
        assert c["label"] and c["blurb"] and c["steps"]


def test_a_base_url_is_recognised_as_its_preset():
    """Installs that predate presets have only a base_url. Without this they
    would all read as "custom" and lose their instructions."""
    assert providers.preset_from_base_url(
        "https://api.z.ai/api/paas/v4")["key"] == "zai"
    # Trailing slash and case are not meaningful in a URL people paste by hand.
    assert providers.preset_from_base_url(
        "https://API.Z.AI/api/paas/v4/")["key"] == "zai"
    assert providers.preset_from_base_url("https://example.invalid/v1") is None
    assert providers.preset_from_base_url("") is None


# ---- the config plumbing ---------------------------------------------------

def test_the_primary_provider_is_named_after_what_it_actually_is():
    assert cfgmod.builtin_provider_name(_cfg(provider_preset="google")) == "Google AI Studio"
    assert cfgmod.builtin_provider_name(_cfg(provider_preset="zai")) == "Z.AI"


def test_an_old_config_is_named_from_its_base_url():
    """No provider_preset, because it was written before presets existed."""
    assert cfgmod.builtin_provider_name(
        _cfg(base_url="https://api.z.ai/api/paas/v4")) == "Z.AI"


def test_a_hand_typed_provider_is_not_labelled_as_a_vendor_it_is_not():
    """The old code called every primary provider "z.ai (free)", including one
    pointed at something else entirely."""
    name = cfgmod.builtin_provider_name(_cfg(base_url="http://127.0.0.1:9999/v1"))
    assert "z.ai" not in name.lower()
    assert "127.0.0.1:9999" in name


def test_ollamas_own_url_is_recognised_rather_than_shown_as_a_host():
    """It used to come out as the bare "localhost:11434", because a local
    server was only ever something you typed in yourself."""
    name = cfgmod.builtin_provider_name(_cfg(base_url="http://localhost:11434/v1"))
    assert name == "Ollama (on this PC)"


def test_a_chat_saved_before_ollama_was_a_preset_still_resolves():
    """That rename would otherwise strand chats saved in between: the stored
    provider name no longer matches anything, and the chat silently falls back
    to the default model mid-conversation."""
    cfg = _cfg(base_url="http://localhost:11434/v1")
    found = cfgmod.find_provider(cfg, "localhost:11434")
    assert found is not None
    assert found["base_url"] == "http://localhost:11434/v1"


def test_each_preset_reads_its_own_key(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert _cfg(provider_preset="zai").resolve_api_key() == "zai-key"
    assert _cfg(provider_preset="google").resolve_api_key() == "google-key"


def test_an_existing_key_still_works_after_presets_arrive(monkeypatch):
    """The key is in an environment variable under the old name and no
    migration can reach it, so the old name has to keep resolving."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ZAI_API_KEY", "the-old-key")
    assert _cfg().resolve_api_key() == "the-old-key"
    assert _cfg(provider_preset="zai").resolve_api_key() == "the-old-key"


def test_the_config_file_key_is_still_the_last_resort(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert _cfg(api_key="from-file").resolve_api_key() == "from-file"


def test_the_primary_provider_carries_its_preset(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    prov = _setup_row(_cfg(provider_preset="google"))
    assert prov["preset"] == "google"
    # The key is not baked into the row -- the row names the variable it lives
    # in, and provider_key reads it. So a row can be stored, synced or shown
    # without carrying a credential around inside it.
    assert prov["env_var"] == "GOOGLE_API_KEY"
    assert cfgmod.provider_key(prov) == "g"


# ---- first-run setup -------------------------------------------------------

def _api(monkeypatch, **cfg_kw):
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = _cfg(**cfg_kw)
    api._client = "sentinel"
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    monkeypatch.setattr(gui_app.Api, "_resume_last", lambda self: None)
    monkeypatch.setattr(gui_app.Api, "list_sessions", lambda self: [])
    return api, gui_app


def test_setup_offers_the_catalogue_rather_than_a_hardcoded_screen(monkeypatch):
    api, _ = _api(monkeypatch)
    keys = [c["key"] for c in api.provider_choices()["choices"]]
    assert "zai" in keys and "google" in keys and keys[-1] == providers.CUSTOM_KEY


def test_choosing_google_configures_it_and_stores_the_key_in_its_own_var(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    res = api.save_setup("google", "g-key")
    assert "error" not in res
    assert api._cfg.provider_preset == "google"
    assert api._cfg.base_url == providers.preset("google")["base_url"]
    assert api._cfg.model == providers.preset("google")["model"]
    assert seen == {"GOOGLE_API_KEY": "g-key"}
    assert res["provider"] == "Google AI Studio"


def test_choosing_zai_does_not_reach_for_googles_variable(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    api.save_setup("zai", "z-key")
    assert seen == {"ZAI_API_KEY": "z-key"}
    assert api._cfg.provider_preset == "zai"


def test_other_needs_a_url_and_a_model_because_nothing_can_guess_them(monkeypatch):
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    assert "error" in api.save_setup("custom", "k", "", "m")
    assert "error" in api.save_setup("custom", "k", "https://x/v1", "")


def test_other_works_with_no_key_at_all(monkeypatch):
    """Ollama and LM Studio have no key. Demanding one makes the app unusable
    with the very setups "Other" exists for."""
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    res = api.save_setup("custom", "", "http://localhost:11434/v1/", "llama3")
    assert "error" not in res
    assert api._cfg.base_url == "http://localhost:11434/v1"   # trailing / trimmed
    assert api._cfg.model == "llama3"
    assert api._cfg.provider_preset == "custom"


def test_a_preset_still_insists_on_a_key(monkeypatch):
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    assert "error" in api.save_setup("google", "")


def test_a_hand_typed_endpoint_does_not_store_its_key_as_a_zai_key(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    api.save_setup("custom", "sk-abc", "https://openrouter.ai/api/v1", "some/model")
    assert list(seen) == ["MNM_API_KEY"], seen


def test_setup_completes_even_when_the_environment_cannot_be_written(monkeypatch):
    """The long-standing rule for this screen: a locked-down machine must still
    end up with a working app, because the key is live in os.environ anyway."""
    api, gui_app = _api(monkeypatch)

    def blows_up(name, value):
        raise OSError("setx is blocked")

    monkeypatch.setattr(gui_app, "persist_env_var", blows_up)
    res = api.save_setup("google", "g-key")
    assert "error" not in res
    assert res["persisted"] is False
    assert api._cfg.api_key == "g-key", "no fallback source for the key"


def test_a_model_whose_price_is_unknown_is_not_claimed_to_be_free():
    """No model is labelled free unless the catalogue explicitly says so.

    Asserted by property rather than by naming a model, because naming one is
    the mistake this file keeps making: "Flash is free" was true when written
    and then Flash itself was withdrawn. A test pinned to a model name goes
    stale exactly when the catalogue does, and stops guarding anything.
    """
    for c in providers.choices():
        p = providers.preset(c["key"])
        declared = set((p or {}).get("free_models") or [])
        for m in c["model_options"]:
            if m["tier"] == "free":
                assert m["name"] in declared, f'{m["name"]} claimed free'


def test_google_makes_no_free_claim_about_any_individual_model():
    """Which models Google's free tier covers has changed repeatedly, and the
    models themselves are retired ahead of their announced dates. The tier is
    real; which model it applies to is AI Studio's answer, not this file's."""
    g = next(c for c in providers.choices() if c["key"] == "google")
    assert all(m["tier"] == "" for m in g["model_options"])
    assert "free" in g["free"].lower()
    # The line may quote allowances -- those came off a real quota page -- but
    # it must not label one model free and leave the rest ambiguous.
    assert not any(m["name"] in g["free"] for m in g["model_options"])


def test_a_model_with_no_free_allowance_is_not_offered():
    """This replaces a test asserting the opposite. Hiding Pro used to be
    wrong -- being unsure of a price is no reason to withhold a better model.
    It is right now for a different reason: a real free-tier quota page shows
    every Pro model at 0 / 0, i.e. no access at all, so offering one would put
    a model in the picker that fails the moment it is chosen. The change is in
    the evidence, not in the principle."""
    assert not any("pro" in m for m in providers.preset("google")["models"])


# -- what a model costs, and when to say nothing ------------------------- #

def test_a_hand_typed_endpoint_gets_no_price_claim():
    """The bug this whole tier exists to prevent. The app printed "$0.00" and
    "via z.ai" from constants in the markup -- fine while z.ai was the only
    thing it could talk to, and a claim about a stranger's billing the moment
    it could talk to anything else."""
    assert providers.model_tier("https://openrouter.ai/api/v1", "gpt-4o") == ""


def test_a_model_running_on_this_machine_is_known_to_be_free():
    """Not a free tier -- there is no quota, no account, and no policy that
    can change next month. That is worth saying, and it is knowable."""
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1",
                "http://[::1]:8080/v1"):
        assert providers.model_tier(url, "qwen2.5-coder") == "local", url


def test_a_remote_host_is_never_mistaken_for_a_local_one():
    """Substring matching on "localhost" would call this local. It is not."""
    assert not providers.is_local("https://localhost.evil.example.com/v1")
    assert not providers.is_local("https://api.z.ai/api/paas/v4")


def test_the_free_tier_is_only_claimed_for_the_models_it_covers():
    z = providers.preset("zai")
    assert providers.model_tier(z["base_url"], z["free_models"][0]) == "free"
    # A model the catalogue has never heard of: no claim either way.
    assert providers.model_tier(z["base_url"], "glm-99-ultra") == ""
    # And Google claims nothing per-model at all.
    g = providers.preset("google")["base_url"]
    assert providers.model_tier(g, "gemini-3.5-flash") == ""


def test_a_trailing_slash_does_not_lose_the_provider():
    """Base URLs are stored rstrip'd in some paths and not others."""
    z = providers.preset("zai")
    assert providers.model_tier(
        z["base_url"] + "/", z["free_models"][0]) == "free"


# ---- what the model picker is given ---------------------------------------
#
# Connecting Google produced a picker with one entry. builtin_provider()
# reported [model, vision_model] and the UI kept the first, so every other
# model on the same key was unreachable without adding the provider again by
# hand -- a list the catalogue already had.

def test_the_builtin_provider_offers_every_model_on_its_key():
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-2.5-flash", vision_model="gemini-2.5-flash")
    models = _setup_row(cfg)["models"]
    # More than one, and exactly what the catalogue offers -- named by asking
    # the catalogue rather than by writing model names into the assertion.
    assert len(models) > 1
    assert models == providers.chat_models(cfg.base_url)


def test_the_vision_model_is_not_offered_as_something_to_code_with():
    """z.ai's vision model is routed to automatically for images. Choosing it
    to hold a coding conversation in is a mistake the picker should not
    invite -- which is what the old slice(0, 1) was really protecting."""
    cfg = _cfg(provider_preset="zai", base_url="https://api.z.ai/api/paas/v4",
               model="glm-4.7-flash", vision_model="glm-4.6v-flash")
    models = _setup_row(cfg)["models"]
    assert models == ["glm-4.7-flash"]


def test_a_model_set_by_hand_stays_selectable():
    """It is in use. Dropping it because the catalogue does not list it would
    take the current chat's model out of its own picker."""
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-3.0-experimental")
    models = _setup_row(cfg)["models"]
    assert models[0] == "gemini-3.0-experimental"
    # ...alongside, not instead of, what the catalogue recommends.
    assert set(models[1:]) & set(providers.chat_models(cfg.base_url))


def test_an_unknown_endpoint_offers_what_it_was_configured_with():
    cfg = _cfg(provider_preset="custom", base_url="https://example.test/v1",
               model="some-model")
    assert _setup_row(cfg)["models"] == ["some-model"]


def test_setup_offers_the_same_models_the_picker_will():
    """The two screens read the same list, or you choose one thing at setup
    and find another afterwards."""
    google = next(c for c in providers.choices() if c["key"] == "google")
    names = [m["name"] for m in google["model_options"]]
    assert names == providers.chat_models(
        "https://generativelanguage.googleapis.com/v1beta/openai")


def test_a_lite_tier_model_is_offered():
    """Flash-Lite was once dropped on an unverified claim that it does not
    stream tool-call arguments. Cheap models are a real choice and stay on
    offer; matched by shape so it survives the next rename."""
    names = [m["name"] for m in
             next(c for c in providers.choices() if c["key"] == "google")["model_options"]]
    assert any("lite" in n for n in names), names


def test_the_newest_models_are_preferred_over_the_retired_ones():
    """Google withdraws models ahead of their published shutdown dates, so the
    preference order has to lead with the current generation -- the 2.5 names
    are a fallback for keys that still have them, not the default."""
    order = providers.chat_models(providers.preset("google")["base_url"])
    newest = [i for i, n in enumerate(order) if n.startswith("gemini-3")]
    oldest = [i for i, n in enumerate(order) if n.startswith("gemini-2")]
    assert newest and oldest
    assert max(newest) < min(oldest)


# ---- a catalogue of model names cannot stay right -------------------------
#
# gemini-2.5-flash was the documented default and then answered
#   404 This model is no longer available to new users
# on a key issued days later. Nothing in this file could have anticipated
# that, so the catalogue is a PREFERENCE and the key is the authority.

def test_the_live_list_beats_the_catalogue():
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-2.5-flash",
               available_models=["gemini-3-flash", "gemini-3-pro"])
    models = _setup_row(cfg)["models"]
    assert "gemini-3-pro" in models
    assert "gemini-2.5-pro" not in models, "catalogue outranked the provider"


def test_the_catalogue_is_used_until_the_provider_has_been_asked():
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-2.5-flash")
    assert _setup_row(cfg)["models"] == providers.shortlist(
        providers.chat_models(cfg.base_url))


def test_the_preferred_model_is_the_best_one_actually_on_offer():
    url = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Flash is the catalogue's first preference and it is available.
    assert providers.preferred_model(
        ["gemini-2.5-pro", "gemini-2.5-flash"], url) == "gemini-2.5-flash"


def test_a_working_model_beats_a_preferred_one_that_is_gone():
    """The actual failure: every name the catalogue knows has been retired."""
    url = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert providers.preferred_model(["gemini-3-flash"], url) == "gemini-3-flash"


def test_nothing_usable_is_not_reported_as_a_model():
    assert providers.preferred_model([], "https://x.test/v1") == ""
    assert providers.preferred_model(["text-embedding-004"], "https://x.test/v1") == ""


def test_models_you_cannot_chat_with_are_kept_out_of_the_picker():
    """A /models listing is everything the key can reach -- embeddings, image
    and speech models included. Offering one as a chat model fails at the
    first request."""
    for junk in ("text-embedding-004", "imagen-3.0-generate", "veo-2.0",
                 "gemini-2.5-flash-tts", "aqa"):
        assert not providers.is_chat_model(junk), junk
    for real in ("gemini-2.5-pro", "gemini-3-flash", "glm-4.7-flash",
                 "qwen2.5-coder:7b"):
        assert providers.is_chat_model(real), real


# ---- a listing is not a list of what your key can use ---------------------
#
# Refreshing from Google returned ~40 models, several of which 404 when called.
# Google publishes previews, experiments, dated snapshots and separately
# licensed open models on the same endpoint and does not filter by key access,
# so the listing cannot be shown raw -- but nothing may be put out of reach
# either, since it is also the only list there is.

def test_previews_and_snapshots_are_not_in_the_default_menu():
    listing = [
        "gemini-3.5-flash",                       # the real thing
        "gemini-3.5-flash-preview-05-20",         # a preview
        "gemini-3.5-flash-exp",                   # an experiment
        "gemini-2.5-pro-002",                     # a pinned snapshot
        "gemini-flash-latest",                    # a moving alias
        "gemma-3-27b",                            # separately licensed
        "text-embedding-004",                     # not a chat model at all
    ]
    assert providers.shortlist(listing) == ["gemini-3.5-flash"]


def test_show_all_still_reaches_everything_chatlike():
    """Narrowing the default view must not narrow the choice: a key really may
    be entitled to a preview, and hiding it would be the worse failure."""
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-3.5-flash",
               available_models=["gemini-3.5-flash", "gemini-3.5-flash-preview-05-20"])
    row = _setup_row(cfg)
    assert row["models"] == ["gemini-3.5-flash"]
    assert "gemini-3.5-flash-preview-05-20" in row["all_models"]


def test_a_provider_of_nothing_but_previews_still_offers_them():
    """Better a menu of previews than an empty one."""
    only = ["gemini-4.0-flash-preview", "gemini-4.0-pro-preview"]
    assert providers.shortlist(only) == only


def test_the_model_in_use_is_never_shortlisted_away():
    """Being on a preview is a good reason to see it in its own picker."""
    cfg = _cfg(provider_preset="google",
               base_url="https://generativelanguage.googleapis.com/v1beta/openai",
               model="gemini-3.5-flash-preview-05-20",
               available_models=["gemini-3.5-flash", "gemini-3.5-flash-preview-05-20"])
    assert cfg.model in _setup_row(cfg)["models"]


def test_stability_is_judged_by_shape_not_by_a_list_of_names():
    for unstable in ("gemini-9-pro-preview", "gemini-9-pro-exp",
                     "gemini-9-pro-001", "gemini-9-pro-2027-01-15",
                     "gemini-flash-latest"):
        assert not providers.is_stable(unstable), unstable
    for stable in ("gemini-3.5-flash", "glm-4.7-flash", "qwen2.5-coder:7b"):
        assert providers.is_stable(stable), stable


# ---- what the agent is told it is -----------------------------------------
#
# Reported: a chat switched to Gemini 3.6 Flash, asked what model it was,
# answered "2.5 Flash / 3.6 Flash". The prompt was built from cfg.model while
# requests used model_override -- so the app was telling it the name of a model
# it was not.

def test_the_prompt_names_the_model_that_will_actually_be_called():
    from glmcode.prompts import build_system_prompt
    p = build_system_prompt(model="gemini-3.6-flash")
    assert "gemini-3.6-flash" in p


def test_the_prompt_does_not_claim_the_app_is_a_model():
    """The first line names the AGENT. Without saying so, a model asked what
    it is blends that name with its own."""
    from glmcode.prompts import build_system_prompt
    p = build_system_prompt(model="gemini-3.6-flash")
    assert "is not a model" in p.replace("\n", " ")


def test_no_vendor_is_written_into_the_agents_identity():
    """It said "You are GLM Code" to every model, whoever was serving it."""
    from glmcode.prompts import SYSTEM_PROMPT
    first = SYSTEM_PROMPT.splitlines()[0].lower()
    for vendor in ("glm", "z.ai", "zai", "gemini", "google", "openai"):
        assert vendor not in first, f"{vendor} in the agent's own name"


# ---- images go to the model that can read them ----------------------------

def test_gemini_reads_images_itself():
    assert providers.is_multimodal(
        "https://generativelanguage.googleapis.com/v1beta/openai")


def test_a_provider_with_a_separate_vision_model_does_not():
    assert not providers.is_multimodal("https://api.z.ai/api/paas/v4")


def test_an_unknown_endpoint_gets_the_safe_route():
    """Narrating an image to a model that could have read it wastes a call;
    sending one to a model that cannot read it fails the turn."""
    assert not providers.is_multimodal("https://example.test/v1")


def test_the_google_default_is_a_model_that_still_exists():
    """The preset's `model` was left on gemini-2.5-flash when its model LIST
    was updated -- so setup kept choosing a model retired for new keys."""
    g = providers.preset("google")
    assert g["model"] in g["chat_models"]
    assert g["vision_model"] in g["chat_models"]
    assert g["model"] == g["chat_models"][0], "default is not the first preference"

# Working in this repo

## Merging

**Always merge your PR to `main` yourself unless explicitly told not to.** Do not
leave it open waiting for review. This has had to be said repeatedly; treat it as
standing instruction, not a per-task question.

## The phone app only ever sees `main`

`mobile/` is a PWA served from GitHub Pages, and `.github/workflows/pages.yml`
triggers on `push` to **`main`** only:

```yaml
on:
  push:
    branches: [main]
    paths: ["mobile/**", ".github/workflows/pages.yml"]
```

A push to a feature branch changes nothing on the device. If someone reports the
phone still showing an old build, check what `main` points at before suspecting
the service worker or iOS's cache — the usual answer is that the fix is sitting
on a branch that never deployed.

The publish job stamps the short SHA into `mobile/index.html` as `mnm-build`, and
the app shows it in Settings. That stamp is the source of truth for "which build
is this phone running", and it names a commit on `main`.

`gh-pages` is a build output: a fresh single-commit orphan branch, force-pushed
by that workflow. Never commit to it by hand. Publishing also depends on a repo
*setting* — Settings → Pages → Source must stay "Deploy from a branch" →
`gh-pages` / (root). Switching it to "GitHub Actions" silently ignores the
workflow.

## PRs are squash-merged, which matters when reverting

Each PR lands on `main` as one squashed commit, so the local commit and the one
on `main` are different objects with the same content.

To revert something already merged, **rebase onto `main` and revert the squashed
commit there** — do not stack a revert on top of the original local commit. A
branch that both applies a change and reverts it has a net-zero diff against its
merge base, and a squash merge of that branch computes exactly that diff: it
merges cleanly and deploys nothing.

```
git checkout -B <branch> origin/main
git revert --no-commit <squashed-sha-on-main>
```

Confirm with `git log --oneline origin/main..HEAD` that the branch carries only
the intended commit before pushing.

## The phone cannot work in the background, and never will

`runAgent` runs in the page and calls the model with `fetch` from the tab, so
the phone is doing the work. When iOS suspends the app the request in flight is
killed under it, `model.chat` throws, and the turn ends on its error path.

Nothing inside a PWA changes that. WebKit has never shipped Background Sync or
Background Fetch, and Web Push (iOS 16.4+, installed PWAs) delivers a
notification but cannot run anything. Do not go looking for a web API for this;
there isn't one. The only real answer for work that must continue while the app
is closed is to run it somewhere else — the desktop, via the handoff in
`session.pending`.

**This is a fact about the platform, not a rule about what to offer.** The
phone's voice mode used to be restricted to reading plus `needs_desktop`
*because* of the above, and that was the wrong inference: it meant asking for
work got a note left for the computer instead of the work. It now has the same
tools the desktop's voice mode has — the writing tools and the full
`dispatch_worker` / `check_workers` / `steer_worker` / `stop_worker` /
`worker_changes` / `revert_worker` set, mirrored from `CONVERSATIONAL_SCHEMAS`
and pinned by `tests/test_phone_workers.py`.

What the constraint actually requires is **honesty, not refusal**. A worker is a
floating promise in the page, so it progresses only while the app is open and
foregrounded, and a suspended tab kills the request in flight. So a worker
killed that way is reported as `interrupted`, with the reason said out loud —
never as `done`. Claiming work finished when it did not is the one outcome worse
than not offering to do it.

Two things about `dispatch_worker` on the phone are load-bearing:

- **It must not be awaited.** Live function calling is synchronous: the model is
  stopped until the tool returns, so a dispatch that waited for the work would
  hold the spoken conversation silent for the whole of it.
  `tests/mobile_ui/test_voice_workers.py` proves the return happens with the
  worker's first model call still open, and races every tool call against a
  timer so this regression fails instead of hanging the suite.
- **Its writes snapshot the previous content first** (`beforeWrite` in
  `makeTools`), because that is the only moment it is still knowable, and
  `revert_worker` needs it. Reverting re-reads the *current* sha rather than
  reusing a remembered one — GitHub rejects a write to an existing path without
  it, and re-reading also means a file changed since fails loudly instead of
  being silently clobbered.

Speech-to-speech does not change this and must not be described as if it
does. A Live session is a WebSocket opened by the page, so it is subject to
exactly the same suspension: iOS freezes the tab, the socket dies, and the
session is over. The phone ends it deliberately on `visibilitychange` rather
than letting it die silently — that is the whole of the difference. Work that
must continue while the app is closed still goes to the desktop, through
`session.pending`.

What *is* done, in `withRun`:

- A screen wake lock is held for the duration of a turn, so the phone does not
  lock itself while you wait. Covers the common case; does nothing for
  app-switching. Absent below iOS 16.4 and refused in Low Power Mode, so both
  must stay non-fatal.
- A turn cut off while hidden is resumed on the way back. `hiddenDuringRun` must
  be watched for the whole turn, not sampled at the start — the real shape is
  starting a turn and *then* leaving. Only a turn that was hidden and did not
  reach a terminal event counts: resuming a genuine foreground error would call
  the same endpoint and fail identically, forever.
- `session.interrupted` is set *before* the turn, not after. If the OS kills the
  app outright the `finally` never runs, and a flag written at the end would say
  the turn had never started.
- Resuming runs the saved history through `healInterruptedTurn`. A turn killed
  between a tool running and its result being recorded leaves `tool_calls` with
  no matching reply, which OpenAI-compatible APIs reject outright — so without
  it the resume fails on its first call and the chat is stuck for good.

## The desktop finishes turns the phone couldn't

Since the phone genuinely cannot work in the background, the machine that can
picks up after it. The phone marks the chat `interrupted` and publishes that in
the synced chat; the desktop scans for it on its 30-second timer
(`sync_finish_interrupted`) and finishes the turn with `pickup_note()`.

The whole design is about one failure: **two devices running the same turn.**
Both would call the model, both would commit, and each would end up holding a
history the other has never seen. Everything below exists to make that
impossible, so do not remove any of it as redundant.

- **A grace period** (`PICKUP_GRACE_MS`, 2 min). The phone resumes its own turn
  the instant it returns, so the desktop must not race it. Measured from the
  chat's `updated` stamp — the phone refreshes that when it saves on being
  backgrounded, so the clock starts when the phone went away.
- **The index is a cache, never the decision.** `pickup_candidates` shortlists
  from index rows; the chat body is re-read and re-checked before acting,
  because the phone may have come back in between.
- **The device lock is taken without `force`.** A phone that is awake is
  finishing its own turn; a stolen lock is exactly the bad case.
- **Chats already running here are skipped before the pull**, not after.
  `sync_pull_chat` re-activates the session from the store, so pulling over a
  live chat overwrites the messages the running agent is holding. The
  `turn_lock` check after the pull is too late to prevent that.
- **`interrupted` is always sent explicitly.** `SyncStore.save` merges, and an
  absent field means "nothing to say" — so omitting it would leave the last
  `True` standing and the chat would be picked up forever.
- **The phone awaits `refreshOpenChatFromSync` before resuming.** Otherwise it
  redoes a turn the desktop already finished.

The same rule now covers the Live session builder: `glmcode/live.py` and the
`live*` functions in `mobile/agent-core.js` must produce byte-identical setup
messages for the same inputs. Both devices open their own socket to the same
model with the same tools, so a difference is not cosmetic — it is one device
offering the model a tool the other cannot run. `tests/test_live.py` pins them
against each other by driving the phone's copy under Node.

`heal_interrupted_turn` exists in both `glmcode/syncstore.py` and
`mobile/agent-core.js` and the two must stay in step: each end adopts the
other's histories, and a turn killed between a tool running and its result
being recorded is unsendable until repaired. On the desktop it runs inside
`chat_to_session`, so every route in is covered — including the manual pull
button, which could always land such a history and fail on its first request.

## The pairing QR is read by a camera, and that constrains what goes in it

`glmcode/pairing.py` seals and `mobile/agent-core.js` opens, and the layout is a
version byte, two fixed-length fields and a compression scheme. Nothing in
either file can tell you whether the other agrees; a mismatch surfaces as "that
pairing link is damaged" on a phone with the working copy on the other machine.
`tests/test_pairing.py` drives the phone's copy under Node against a real
desktop seal, both directions.

Two things about that payload are not free to change:

- **It is deflated before encryption** (wire version 2). The payload is a
  photograph's worth of detail: a realistic one — two APIs with their model
  lists, a GitHub PAT, a sync passphrase — was a 113-module QR, and a camera has
  to resolve every module of it off a laptop screen. Compressed it is 73. Base
  URLs and model names repeat heavily; the keys are random and do not compress
  at all. Version 1 is still *read*, so a phone that updated ahead of its
  desktop can still pair. Adding a field to the payload spends this margin —
  `tests/test_pairing.py` and `tests/test_pair_qr.py` assert the module count
  for that reason.
- **The QR holds the bare token, with no URL around it.** It used to hold a
  link, and that was the bug behind "the code won't scan": the phone's *Camera*
  app read it perfectly, opened Safari, and paired Safari's storage — which for
  an app installed to the home screen is not the app's. The keys arrived
  somewhere that was not the app, and the app still had none. A bare token
  cannot be opened by anything, so scanning it with the wrong app does nothing,
  which is the better failure. The URL belongs in the install code, one step
  earlier in the same sheet.

The other half was the scanner. `getUserMedia` asked for no resolution at all
(browsers hand back 640×480) and the decode frame was capped at 720px, which is
about 1.7 pixels per module — unreadable by anything. It now asks for 1920×1080
and caps at 1440. **Keep a cap**: jsQR is plain JavaScript costing per pixel, so
a full 1920×1080 frame drops the scan rate to a crawl on the device this is for.

`tests/mobile_ui/test_scan_pairing.py` plays a real sealed QR through a fake
camera as an actual video track. What it cannot test is whether a real phone
reads it: canvas frames are perfectly sharp, square-on and noiseless, and jsQR
reads about two pixels per module from those while a lens does not. So the
margin is asserted where it is a fact — the pixels the decoder is handed —
rather than by trusting a synthetic decode.

## The sync passphrase is generated, never invented

It reads like a password and is not one. It is the key the chats are encrypted
under, and its only job is to be **identical on both devices** — nobody logs in
with it, and there is nothing to "remember". Asking a person to make one up
bought exactly two things: a chance to pick something weak, and a chance to
mistype it on the second device and fork the history into two halves that can
never read each other. On a phone keyboard, for a string that has to match
another machine exactly, that was the worst possible place to ask.

So `syncstore.make_passphrase()` and `makeSyncPassphrase()` in
`mobile/agent-core.js` generate one: four groups of five from the pairing code's
no-lookalike alphabet, 100 bits. Same shape on both, so a code made on either
can be copied to the other.

Three cases, and only one involves typing:

- **paired** — pairing already carries the key, so sync just works. This is the
  common path and it has no dialog in it at all.
- **nothing exists yet** — the device makes one. `open_sync` / `openSync`
  already report `created`, and `central_has_store` / `syncStoreExists` are what
  decide which case this is *before* generating.
- **a store already exists** — the key is decided and cannot be guessed, so the
  recovery code gets copied across. One field, no confirmation: it is a code
  being copied off another screen, not a secret being invented.

Never generate a key without checking for an existing store first. A fresh key
against one that exists cannot read a single chat in it, and surfaces as `Wrong
sync passphrase` — true, and useless, for a passphrase the user never chose.

The trade this makes: nobody types a passphrase, and nobody has one memorised
either. The only copies are the machine's credential store and any paired
phone. So **the code is shown, not hidden** — desktop Settings → Your phone →
"Show recovery code", and the same on the phone. Lose both devices without it
and the chats stay ciphertext forever. "Change passphrase" is gone; it was never
a useful thing to do, since a new key cannot read anything already uploaded.

## Everything the agent reads is data; only the conversation gives orders

`fetch_url` and `web_search` already said so. Nothing else did — which left the
two channels that matter most completely unmarked:

- **MCP tool output.** Third-party code, started from a command line the user
  pasted, whose text goes straight into context with no line numbers and no
  structure around it. A sentence in it reads exactly like a message. The note
  names the server, because "which server said this" is the first question when
  a result looks wrong.
- **The `@`-mention block**, which is appended to the *user's own message*.
  Anything arriving there sits in the most trusted position in the
  conversation, and the user pointed at the file without writing what is in it.
  The label goes **before** the content: a warning after ten thousand
  characters of attacker-controlled text has already been read in the wrong
  frame.

`read_file` is deliberately left alone. Its output is line-numbered
(`  12 | text`), which already frames it as file content rather than speech,
and it is the hottest tool in the app — the rule is stated once in the system
prompt, where it is always in context and costs nothing per call.

Two things worth keeping:

- **The note counts inside `MAX_TOOL_OUTPUT`, not on top of it.** Appending
  after the truncate put MCP results back over the cap — the one thing that cap
  exists to prevent, after an uncapped result once ballooned a chat to ~1.5M
  tokens. Room is reserved before truncating, so the total is what it was
  before the note existed. (`_truncate` also lands ~43 characters past the
  limit it is given; that is pre-existing and why the cap test carries slack.)
- **`UNTRUSTED_INPUT_RULE` is a named constant on both devices**, and
  `tests/test_untrusted_input.py` pins them word for word. This is how the gap
  was found: the desktop prompt was fixed and nothing failed, because the phone
  had no such rule at all. Both devices work on the same repository — the phone
  reads it over the GitHub API and every write it makes is a commit — so a rule
  that holds on only one of them is worth much less than it looks. Paraphrasing
  it on one side is a different instruction with nothing to say so.

## The runner is the only machine that is never off

`glmcode/ci.py` has been able to run the agent headless on a GitHub runner for
a long time — a `/agent` comment, work on a branch, a draft pull request, a
comment back with the link. All of it finished, and none of it reachable: the
workflow was a file in `docs/` you were told to copy into another repository by
hand, so the honest description of the feature was "you can do this yourself".

That gap is worth more than convenience. The phone hands work to the desktop,
and the desktop has to be awake; a runner does not.

- **The workflow is read from `docs/agent-workflow.yml`, never duplicated as a
  string.** The copy people install by hand and the copy the app installs would
  otherwise drift into two different workflows.
- **`workflow_dispatch` is gated by GitHub already** (it needs write access),
  which is why the job's `if:` lets it through unconditionally. The
  `issue_comment` path is NOT gated by GitHub and must keep checking
  `author_association` for itself — a stranger opening an issue must not be
  able to start a runner.
- **`MNM_TASK` reads `inputs.task || github.event.comment.body`.** Two trigger
  paths, one variable; a dispatch that left it empty would start a runner with
  nothing to do and say so ten minutes later.
- **The acknowledgement step is skipped on a dispatch run,** because reacting
  to `context.payload.comment.id` with no comment fails the step.
- **`workflow_status` reports "installed but out of date".** A workflow
  installed before `workflow_dispatch` existed cannot be started from the app,
  and saying nothing would make the button look broken.
- **The app cannot set the Actions secret** and says so rather than appearing
  to have finished — a feature that silently needs one more step is one that
  looks broken the first time it is used.

## Web Push is the one thing a suspended phone can still do

The desktop finishes turns the phone was suspended through, and for a long time
never told it — you found out by opening the app and looking. Web Push closes
exactly that: it cannot **run** anything on iOS (WebKit still has no Background
Sync or Background Fetch, and that section above stands), but it can deliver a
notification, and a notification was the whole missing piece.

**No server is introduced.** The desktop is the sender: `glmcode/webpush.py`
implements RFC 8291 (message encryption) and RFC 8292 (VAPID) against
`cryptography`, which is already a dependency. The subscription and the
desktop's VAPID public key travel in the encrypted sync store the two devices
already share (`devices/push.json`, `devices/vapid.json`).

Things that are load-bearing:

- **The RFC's own worked example is the test.** Hand-rolling push encryption is
  only defensible if it is checked against the specification rather than
  against one's reading of it, so `tests/test_webpush.py` runs RFC 8291 §5 —
  their keys, their salt, their plaintext, their exact output bytes. If that
  test ever fails, do not adjust it to match the code.
- **Salt and ephemeral key are random per message.** Reusing the pair against
  one subscription reuses the AES-GCM nonce, which leaks the plaintext. That is
  the worst mistake available here and there is a test for it.
- **The VAPID `aud` is the endpoint's ORIGIN,** not the endpoint. Sending the
  full URL earns a 401 that names no field.
- **The key goes through the sync store, not the pairing QR.** That payload is
  read by a camera and its module count decides whether scanning works at all.
- **`userVisibleOnly` is a promise, not a hint.** A worker that receives a push
  and shows nothing gets its subscription revoked, so `sw.js` raises a
  notification even for an empty or unreadable payload.
- **A 404/410 means forget the device; a 500 does not.** Dropping a
  subscription over a transient server error silences that phone for good.
- **The service worker's `CACHE` name must be bumped** whenever its handlers
  change (`tests/test_phone_icons.py` pins it, so the bump is deliberate). A
  phone on the old worker receives the push and does nothing with it.

## Values both devices need are generated, not restated

`scripts/gen_mobile_core.py` writes a marked block inside
`mobile/agent-core.js` from the Python that defines those values —
wire-format version bytes, PBKDF2 iterations, the sync repo and branch names,
device-lock timings, the Live sample rates, `CONVERSATIONAL_SCHEMAS`, and
`UNTRUSTED_INPUT_RULE`. Change the **Python** and run the script; editing the
block by hand only makes the phone disagree with the desktop.
`python scripts/gen_mobile_core.py --check` runs in CI and
`tests/test_generated_core.py` fails if the committed block is stale.

**Only data goes in the block.** The crypto, the agent loop and the sync store
stay hand-written on both sides, pinned by the node tests. Generating a
*function* across two languages is a different and much worse idea than
generating the numbers it operates on — a test asserts everything collected is
plain data, so this line does not quietly move.

**It is a block, not a separate file, on purpose.** The phone has no build step
and that is deliberate: a folder of static files with no toolchain to rot. A
generated `.js` of its own would need a script tag in `index.html`, an entry in
the service worker's precache list, and a module lookup that works under both a
plain `<script>` and `node --test` — three new ways to half-deploy a phone, to
save one file. The block needs none of them.

This does not retire the node parity tests and must not be read as doing so.
They cover the half that is still written twice.

## The Api class comes apart along subjects, not at a line count

`gui/app.py` held one `Api` class of 190 methods across ~3,600 lines, and every
feature landed in it. Two seams are taken so far, each a subject that shares
its whole vocabulary:

- `glmcode/gui/devices_api.py` — sync, pairing, Web Push, the CI runner:
  reaching a machine that is not this one.
- `glmcode/gui/voice_api.py` — speech-to-text, text-to-speech, and the spoken
  conversation: the delegator agent, its `<sid>::voice` event sink, the convo
  lock, and the two queues that carry work done by voice back to the coding
  agent.
- `glmcode/gui/github_api.py` — cloning and connecting a project, the token,
  the clone root, push and pull, and reviewing a pull request: everything that
  speaks `githubsync` about the chat's own repository.
- `glmcode/gui/checkup_api.py` — "is any of this actually working": the one
  panel that asks every other subject at once.

All four follow the same rules.

- **A mixin, not a collaborator object.** These methods reach all over the
  instance (`self._cfg`, `self._chats`, `self._active`, `self._store`,
  `self._save_chat`). More importantly, **pywebview exposes the Api instance's
  public methods by inspection**: an inherited method is found exactly like a
  defined one, and a method moved onto a *collaborator* would not be — failing
  only at runtime, in the app, on the one path nobody re-tests.
- **The proof is the untouched tests.** The sync, push, CI and GitHub suites
  were not modified and still pass. A refactor that needed its tests rewritten
  would not have been behaviour-preserving.
- **Nothing is left behind.** A copy still defined on `Api` would shadow the
  mixin's, and which one wins depends on the MRO — `tests/test_api_split.py`
  fails if a name exists in both.

**The voice seam is one subject and must not be halved.** The app has two
speech engines and the choice is per-session: Gemini Live hears and speaks for
itself, while the local engine is Whisper plus Kokoro/Piper with this app in
between. `voice_mode` readies one, `live_voice_config` readies the other, and
`_persist_voice_turn` records the result identically whichever ran. Splitting
"the spoken conversation" from "the speech engines" would cut through the
middle of that rather than along a seam.

Cutting the voice seam is also what moved `WebEvents` out of `app.py`, into
`glmcode/gui/events.py`. It was never part of the `Api` class — it is the other
side of the bridge — and `_ensure_convo` builds a second one for the spoken
conversation, which a mixin in its own module cannot do without an import
cycle. Two things follow:

- **A lazy `from .app import X` inside a function is not the fix.** It works,
  and it is the seam leaking back: the cycle is still there, hidden until the
  next split. `tests/test_api_split.py` fails on that string.
- **`app.py` re-exports what it used to define** — `WebEvents`, `_TtsFeeder`,
  `_tts_engine_voice`, `_data_uri`, `_thumb_uri`. That is why
  `tests/test_webevents.py`, `tests/test_read_aloud.py`, `tests/test_background.py`
  and `tests/test_tts_engine.py` still import them from `glmcode.gui.app`, were
  not touched, and still pass.

`speech.py`, `media.py` and `paths.py` are leaves and must stay leaves: they
exist so that `events.py`, `voice_api.py` and `github_api.py` can each have
what they need without importing the other. An import back up the stack
recreates exactly the cycle they were carved out to break.

**Not every GitHub call belongs to the GitHub seam.** Chat sync, pairing and
the CI runner also talk to GitHub, and they stay in `devices_api.py`, because
their subject is reaching a machine that is not this one — GitHub is the
transport they happen to use, not what they are about. `sync_env` calling
`self._gh_token()` across that line is two mixins sharing one instance, which
is what a mixin is for, and `tests/test_api_split.py` pins it so nobody
"fixes" it later by duplicating the helper.

The same question moved `get_phone_app` and `get_pair_phone` in the other
direction: setting the phone up *is* reaching the other machine, and they had
been left in `app.py` by the first split. Expect a seam to correct the one
before it — that is the subject boundary being found, not churn.

**A dead-looking import may be a lookup path.** `pyflakes` called `pairing`
and `qrcode_util` unused in `app.py` once their methods moved, and removing
them broke four pairing tests: they monkeypatch `gui_app.qrcode_util.qr_svg`,
which needs the name to still resolve there. Re-exported with `# noqa: F401`,
same as `WebEvents` and the rest. The test failing is the good outcome —
check what reaches a name through `gui.app` before deleting it.

**The checkup seam was forced by the size guard, and that is not a
contradiction.** `test_app_py_actually_got_smaller` exists to stop `Api`
reaccumulating, and nine new methods landing on it is precisely the
reaccumulation it watches for — the failure is the signal to ask *which
subject is this*, not a licence to move code until a number drops. The answer
was a real one: a check, a status and a fix are its own vocabulary, and it
shares almost nothing with the rest of `Api`. That it CALLS `sync_env`,
`_open_sync_store`, `ci_status` and `_active_repo_coords` across three other
mixins is not a smell — one instance holding all of them is what a mixin is
for, the same as `sync_env` reaching `self._gh_token()`.

The next seams, when it is worth it, are the same shape: a subject with its own
vocabulary. Do not split by size.

## An undo you cannot undo is not a safety net

Found by asking which failures are EXPENSIVE rather than which are visible.
Every report so far has been about something you can see; a papercut costs a
minute and losing an afternoon's work costs the project. Two defects, both in
the file whose entire job is not losing work.

**`revert_to` destroyed work that was in no commit at all.** Snapshots are
taken BEFORE a user turn, so everything the newest turn produced — and
anything edited by hand since — existed only on disk. `reset --hard` plus
`clean -fd` then threw it away with nothing holding it: edit-and-resend a
message from five turns ago and those five turns were gone for good. It
snapshots first now, and the label says what the point is, because it shows up
in Settings' restore list beside the per-turn ones.

**The history only ever moves forward.** `reset --hard` was the obvious way to
put the files back and it rewinds the branch pointer, which orphans every
commit after the target — including the safety snapshot taken one line above.
The first version of this fix kept a copy `list_snapshots` could no longer see,
and its own test caught it. A restore-point log that deletes restore points is
not one. `read-tree` + `checkout-index` + `clean` do to the FILES exactly what
`reset --hard` does, and nothing to the branch.

**Path containment was a string prefix.** `"/home/you/proj"` prefixes
`"/home/you/proj-evil"`, so a sibling directory satisfied the check — on the
two functions here whose job includes deleting files. The comment beside one of
them already said *"a check that only holds because of where the caller got its
input is not a check"*; it simply was not true of itself. `is_relative_to`
walks components and does not.

## The index is a cache, and a cache whose loss is permanent is not one

Every synced chat is its own encrypted file under `chats/`. One small file
beside them, `index.json`, is the only thing that names them — **nothing in
normal operation enumerates that directory**. So the index is the single point
of failure for the entire store, and it was read by code that collapsed three
different answers into two:

- **there is no index yet** — a first device against a fresh store;
- **there is one and we did not get it** — GitHub down, a phone with no signal;
- **there is one and it will not decrypt.**

The second was answered with *"you have no chats"*, which is a sentence about
your data offered in place of one about the network, and it is by far the most
common of the three.

The third was destructive. It returned an empty index **carrying the real
sha**, so the very next `save` wrote that emptiness straight over the good
index — leaving every chat's ciphertext sitting there readable and unnamed,
with nothing left that knew the ids. On both devices at once, since they share
the file.

`_read_index` / `readIndex` now answer all three separately, and refusing is
the point: one failed sync costs a retry, and guessing cost the list. The
distinction is drawn on a **status**, so `GitHubError` carries one (0 for
"could not reach GitHub", None for a fake API or an older path, which are
treated as absent because that is what they have always meant) and the phone's
client attaches `err.status` for the same reason.

`rebuild_index` / `rebuildIndex` is the other half, and it is what makes the
claim in the rest of this file true — *"the index is a cache; the body is the
truth"* was written all over the store and was not true **of** it, because
nothing could turn the bodies back into a list.

- **A chat is dropped only when its body says so.** A body that will not
  decrypt is counted and **left alone**: "this key can't read it" is not "it
  isn't wanted", and deleting it would finish the job the damaged index started.
- **Tombstones survive only if the old index can still be read**, and the
  result says how many were found rather than implying the delete history came
  through. Losing them lets a chat deleted on another device come back on the
  next save, and keep coming back.
- **`chats/<id>.lock.json` is not a chat**, and it lives in the same directory.
- **It is offered for damage, never for a network failure.** Rebuilding from a
  store you cannot read writes an empty index over a good one — the original
  bug, wearing a button.
- **Both devices have it.** The phone is the one you are most likely to be
  holding when the list comes up empty.

**One builder for the index row** (`_index_row` / `indexRow`), because `save`
and the rebuild both produce it. That is not tidiness: `interrupted` was simply
**absent** from the phone's row, and `pickup_candidates` reads rows. So a turn
the phone was suspended through was saved with the flag in its body and no flag
in its row, and *"the desktop finishes turns the phone couldn't"* — the whole
section above — never fired for a single chat. Nothing failed; it just never
happened.

The row is also built from the **merged** body rather than the incoming write.
Absent means "I have nothing to say about this" everywhere else in this store;
one field further out, in the row, it still meant "delete it", so a device that
knew nothing about a chat's repo blanked that column while the body kept it.

## A result belongs to the call that made it, not to the last box on screen

Reported as a photograph: a `browser_new_tab` chip, red, showing an error about
`run_command`.

The page attached every `tool_result` to `t.lastTool` — the chip it built most
recently. That is correct exactly as long as calls and results strictly
alternate, and `_handle_tool_calls` has **one path where they don't**:
arguments the model sent that will not parse as JSON were answered with a reply
and no call, because there is nothing to run. So that result landed on the
*previous* tool's chip, and `finishToolEl` overwrites the body and adds the
error class — relabelling a finished, often successful call with a different
tool's failure, under its own name. The failed call appeared nowhere at all.

**Reloading the chat put it right, which is most of why this was hard to
report.** `sessions.to_display` pairs by `tool_call_id` and was always correct;
only the live stream was guessing. A bug that fixes itself when you look again
gets described as "sometimes it shows the wrong thing".

Both halves are fixed, and the second is what stops the next one:

- **The unparseable call announces itself.** The name is known — only the
  arguments are unreadable — so it gets a chip of its own and the error goes in
  it.
- **`tool_result` carries `call_id`, and the page matches on it**, falling back
  to the last chip only when there is no id (an older backend, a replayed
  event). `data-call-id` therefore goes on **every** chip; it used to be set
  only for `STOPPABLE_TOOLS`, because Stop was the only thing that had needed
  it.

The phone had the same defect wearing different clothes: `JSON.parse(...)
catch (e) {}` left `args = {}` and **ran the tool anyway**. A write whose
`content` was mangled became a write of nothing, and the only party who could
have noticed — the model — was never told. It reports it now and does not run
the tool, which is what the desktop already did.

## `env` is not a command, it is a way of starting one

`is_readonly_command` is not a convenience. It is what **plan mode enforces
itself with** — documented as a hard deny "regardless of ask/autoedit/yolo mode
or session allowlists" — and it is also the only thing standing between a shell
command and the per-path rules that protect `.env` and migrations, since those
apply to file-*write* tools and a shell command is not one.

Its own bar is *"provably read-only"* and *"when in doubt, we ask"*. Three
things were getting through it.

- **`env` and `command` were in the safe list.** So `env rm -rf build` and
  `command rm x` were classified as reading, in a turn that had promised only
  to explore. Refusing them outright would have been wrong too: `env
  NODE_ENV=production npm ls` is an ordinary thing to write and reads exactly
  as much as `npm ls` does. The prefix is stripped and what is left is judged
  on its own merits — recursion **bounded**, because `env env env rm x` is
  legal.
- **An unrecognised flag fails closed.** `env -S` takes a whole command as a
  *string*; a flag whose meaning is not known may be carrying one.
- **Three of the safe commands take an output file as an argument.**
  Redirection was already refused and these need none: `sort -o FILE`,
  `uniq in out`, `xxd -r in out`. The value-taking flags are listed per command
  so `uniq -f 2 in.txt` is not read as naming an output.

`command -v` goes the other way and is allowed: that flag is what stops it
running anything.

## "I could not find out" is not "there is nothing to worry about"

The same shape as the sync index, in two more places. Both are about losing
work, and both were found by asking what a failure path RETURNS rather than
whether it crashes.

**`_repo_state` answered any exception with `{}`.** The phone reads the repo
over the GitHub API, so uncommitted work on the desktop is invisible to it and
this field is the only thing that warns — *"editing these files risks
committing over that work"*. An empty dict produces no warning at all, and
`SyncStore.save` **merges**, so the empty dict is *present* and overwrites the
last true answer. One git hiccup therefore silenced the warning until the next
successful read, on the failure the field exists to prevent.

It returns `None` for "could not find out" now, and `session_to_chat` omits the
key — absent means "I have nothing to say about this" everywhere else in that
store. `{}` is kept for the case that is genuinely *known* to be nothing to
warn about (no cwd, or no GitHub remote), because publishing it is what clears
a warning that no longer applies.

**Every secret shares one blob, and `set` is a read-modify-write over it.**
`_read` answered an unreadable file with `{}`, and the comment beside it —
*"the user just re-enters the token; nothing else depends on it"* — was true of
a read and false of a write: storing one credential wrote an empty blob over
the GitHub token, the API keys, the push keypair and the sync passphrase.

The passphrase is the expensive one. It is generated rather than chosen, and
its only copies are this store and a paired phone, so a silent wipe halves the
number of ways the chats can ever be read again.

- **`get` stays quiet, `set` and `delete` do not.** Re-entering one token costs
  a minute; the others are not recoverable by re-entering anything. `delete`
  swallows failures because "already gone" is its normal case — that is not
  this case, and the user has just been told it succeeded.
- **`SecretStore.read` is the strict twin of `get`**, for callers where the two
  answers lead somewhere different. `webpush_keys` is the example, and its own
  docstring already said *"this must never be a 'create if missing' that
  mistakes a read failure for missing"* — which is exactly what it was, because
  `get` answers both with `None`. Regenerating that keypair silently
  invalidates every push subscription already out there.

## Nothing said so — which is what "Check my setup" is for

Every expensive bug in this app has had the same shape: something was off, or
half-configured, or quietly damaged, and **nothing anywhere said so**. A chat
list that came up empty. An extension reporting Connected while every command
timed out. A credential blob that would not decrypt, found out about by storing
one credential and losing the rest.

Each of those has a panel. Each panel is somewhere you only go once you already
suspect that thing. `Api.self_check` is the one place that asks all of them
without being asked about any of them in particular.

Three rules, and the third is what the feature is for:

1. **A check says what to DO.** "Sync is broken" is a dead end; the fix belongs
   in the same row. `tests/desktop_ui/test_setup_check.py` fails if a `warn` or
   `fail` row has no `fix`.
2. **One check failing must not stop the others.** A diagnostic that dies half
   way through is worse than none, because the rows it did print look like the
   whole answer. Each runs independently and an unexpected error becomes that
   check's own `unknown` row — with its id named in the runner's table rather
   than derived from the method name, so the one row that matters most is not
   the one that comes back unlabelled.
3. **A check that could not run reports `unknown`, never `ok`.** Reporting
   "fine" for something untested is the mistake behind half the sections in
   this file, and it would be a poor thing for the diagnostic itself to make.
   Sync distinguishes damaged (offer the rebuild) from unreachable (say so and
   pass no judgement on the data), because collapsing those two is what cost
   the chat list in the first place.

**It found a real one immediately, on the machine running the tests.**
`import keyring` proves the package is installed, not that anything is behind
it — and `keyring` installs happily where there is no OS credential store: a
headless Linux box, a container, any desktop without gnome-keyring or kwallet.
`_best_backend` selected it on the strength of the import, and then:

- Settings reported the backend as `keyring`, and **secure**;
- `get` swallowed `NoKeyringError` and answered `None` for every credential, so
  the GitHub token read as "not set" however many times it was entered;
- `set` raised out of the Settings button with nothing shown;
- and `EncryptedFileBackend`, which exists for exactly that machine, was never
  reached.

`KeyringBackend.__init__` now does one read of a name that does not exist. It
costs nothing, it is a read so it leaves no stray entry, and it is the
difference between working and appearing to.

**The stripe was not drawn, and only a photograph said so.** The first
stylesheet used `border: 1px solid var(--line)`, and `--line` is not a token
here. An undefined custom property makes the whole shorthand invalid at
computed-value time, so `border-style` stays `none` and the width computes to
`0px` — every colour correct, nothing rendered. All eight tests passed, because
a class name is not a picture. The test now asserts the **computed** border
width, which is the only form of that assertion worth having.

## The history is a tree; rewinding is only one way down it

`backup.py` commits a snapshot of the work tree before **every** user turn, and
edit-and-resend already reverts both the conversation and the files to one. So
the structure underneath has always been a tree. It was offered as a line:
`rewind_to` truncates in place and the branch you were on is gone.

`fork_at` is the other move — the original chat is untouched, and a new one
opens holding the conversation up to that message with the files reverted to
the same snapshot. For an agent that is wrong a fair share of the time, "try it
both ways and compare" is a better primitive than undo, and it costs no extra
storage because those commits are already being written.

- **Up to, not including.** The fork starts where that turn was about to be
  sent, so its text is still yours to retype or change.
- **The files are reverted BEFORE the new chat is built,** so its own
  `BackupRepo` records the state the fork actually starts from rather than the
  parent's latest. A failed revert returns an error and creates nothing: half a
  fork — a new chat whose files belong to the other branch — is worse than none.
- **The inherited snapshots come with it,** so the new chat can be rewound or
  forked again. The tree keeps branching instead of flattening at the first
  fork.
- **Same turn ordinal as the edit path** (position among user bubbles), so the
  two cannot disagree about which message was meant.

## Setting a system prompt means `set_system_prompt`, not the attribute

`Agent.__init__` calls `rebuild_system_prompt()`, so `messages[0]` is written
before any caller can touch `_base_system_prompt` — and `messages[0]` is what
`_messages_for_call` sends. Assigning the attribute afterwards updates a cache
nothing re-reads: the change looks applied and is not.

That is how the Browser Agent ran with the **coding** agent's system prompt.
And since its goal lived only in that specialised prompt, the goal reached the
model nowhere at all — the user turn said "Begin. Work toward the goal", so the
conversation contained no goal to work toward. It answered by asking what the
task was, on every provider, which is why it looked like the Gemini sub-agent
bug and was not.

Two rules came out of it:

- **`set_system_prompt(text)` installs it**, into the cache *and* into
  `messages[0]`. Nothing should assign `_base_system_prompt` from outside.
- **A task goes in the TURN, not only in the system prompt.**
  `SUBAGENT_PREAMBLE` always did this; `BROWSER_AGENT_TASK` now does too. A
  system prompt is what the model *is*; the turn is what it was *asked*. Some
  providers treat the first as background, and a request whose conversation
  contains no request is answered with a question.

`tests/test_browser_agent_task.py` checks what reaches the wire rather than
what the code appears to set — the whole bug lived in that gap.

## A request ends on the turn, not on a note about it

The context-usage figure is sent at the END of the message list, not inside the
system prompt. That much is right and must stay: the figure changes every turn,
so putting it first gave every request a different prefix from the last, and a
prefix cache only matches an identical run of *leading* tokens. This app
re-sends ~12,400 tokens of system prompt and tool schemas on every request.

Dead last was wrong, though. A request must end on the user's turn or a tool
result. Google's OpenAI-compatibility layer does not treat a trailing `system`
message the way z.ai does, and sub-agents showed it at its worst: their first
request is `[system prompt, user(the mission), system(context usage)]`, so the
last thing the model read was a sentence about token budgets — and it answered
*that*, with the mission sitting directly above it. A coordinator handing out
three missions got back three sub-agents asking what the task was, on Gemini
only, which is exactly what makes it look like a model problem rather than a
message-ordering one.

The note goes **next-to-last** now. In any conversation long enough for the
cache to matter this leaves the same stable prefix, so nothing was given up.
`tests/test_request_ends_on_the_turn.py` pins both halves — that the request
ends on the turn, and that the prefix before the newest turn is unchanged
between requests.

## The live voice engine has to be wired IN, not alongside

Four separate desktop bugs turned out to be one mistake: `startLiveVoice` was
added next to the local engine rather than into it, so everything the local
engine owns was simply absent when the toggle said "Gemini Live".

- **Push-to-talk is enforced in `onaudioprocess`,** because that is the only
  path audio takes to the model. It was enforced nowhere: the mic streamed
  continuously whatever the toggle said, and the *button* ran the local
  recorder — so live + push-to-talk gave you a hands-free live session with a
  local transcribe-and-send bolted on top. `pttPress`/`pttRelease` branch on
  the engine now, and release sends `audioStreamEnd` to flush the turn (that
  pauses the input stream; sending audio again resumes it — it does not close
  the session).
- **`voice.speaking` is a LOCAL-engine flag.** The live engine schedules
  playback straight onto an AudioContext and sets nothing. Anything that must
  not talk over the assistant has to ask `liveSpeaking()` too, or it is right
  in one mode and wrong in the other — which is what let finished workers talk
  over the model.
- **Worker announcements go through the live session,** as a text turn into
  its own socket. Routing them to the local convo agent is what put two voices
  in the room at once, one of them reading from a conversation the live model
  had never seen.
- **`liveSpeaking()` alone is not enough to serialise announcements.** Audio
  takes a moment to come back, and it is false for that whole gap, so two
  workers finishing together both got through. `liveVoice.pendingTurn` covers
  the gap and is **time-boxed** (`LIVE_TURN_WAIT_MS`): `turnComplete` is the
  only thing that clears it, and a plain boolean would silence every later
  announcement for the rest of the session if that frame never arrived.
- **The transcript is the local engine's `.voice-you`/`.voice-it` blocks.** The
  live path assigned `textContent` on the whole caption element, which wiped
  every earlier turn — including the local engine's, so switching engines
  mid-session erased the conversation. It appends through the same helpers now.

## Talking and typing are one conversation

A spoken exchange used to reach the append-only transcript file and nothing
else. You could talk for ten minutes, close the overlay, and the chat window
still showed whatever had been typed before — and the coding agent, asked about
it by typing, had never heard any of it.

The phone did the right thing from the start: `voiceRecordTurn` pushes both
halves into `session.messages` and renders them as bubbles.
`_persist_voice_turn` now does the same on the desktop — transcript *and*
chat — and emits `voice_chat_turn` on the **coding** chat's sink, since the
voice sid drives the overlay, which is not where a chat message belongs.

- **The turn lock is tried, not taken.** Appending to `agent.messages`
  underneath a running turn is how you get a `tool_call` with no matching
  reply. An idle chat is written to and saved immediately, so it survives a
  reload; a busy one queues, and `_drain_voice_turns` picks it up at the top of
  the next turn — the same shape as the worker reports beside it.
- **The live engine passes its transcript in.** Gemini transcribes both
  directions and hands them over, so `reply=` is explicit rather than dug back
  out of the delegator's history — which is a copy at best, and absent
  entirely once voice mode closes and `convo_agent` is dropped.

Worth knowing about **whose transcription this is**, because it is asked: with
the live engine both devices already use Gemini's own `inputTranscription` /
`outputTranscription`. There is nothing to switch. With the local engine there
is no Gemini in the loop at all — Whisper hears, the delegator answers — so the
question does not arise there either.

## A chat has two agents and ONE set of workers

Reported as *"there are problems with the connection between the voice agents
and the text agents — no context works"*, and it was literal. `_workers` is an
instance attribute and a chat builds **two** Agents: the one you type to and
the delegator you speak to. Each kept its own registry.

So a worker started by speaking was invisible to `check_workers` typed into
the same chat, and `steer_worker`, `stop_worker`, `worker_changes` and
`revert_worker` all answered *"no worker matches"* for it. The reverse held
too — dispatch by typing, ask out loud how it is going, and the delegator said
nothing had been dispatched.

What made this worse rather than merely incomplete: the **result** already
crossed over (`worker_reports`). So the coding agent was told a worker had
finished and then could not find the worker it had just been told about.

The REPORT had the mirror of this bug, on the other side. `voice.announceQ`
is the voice route's, so a worker dispatched by typing finished on its own
daemon thread and the agent you were typing to was simply never told — a
shared registry means `check_workers` can find it, but being told is what
makes a follow-up answerable without the model thinking to look. Both routes
call `record_worker_result` now; a worker reports on the sink of whoever
dispatched it, so there is exactly one route per worker and nothing is filed
twice.

`Agent.adopt_workers_of` is the fix for the registry, called from
`_ensure_convo`. Three notes:

- **The live Agent objects come with it** (`_active_subagents`). Steering and
  stopping reach a worker *through* them, so sharing only the records would
  make a worker visible from the other side and un-steerable — which looks
  like it worked, and is the worse failure.
- **Each agent keeps its own id counter**, so the number alone is not unique.
  `_dispatch_worker` walks past a taken id under the shared lock; without that
  the second dispatch overwrites the first one's record.
- **`_worker_perms` is deliberately NOT shared**, and sharing it was the first
  version. Blocked permission requests are released in bulk by two callers who
  each mean only their own — closing voice mode denies the delegator's,
  cancelling a turn denies the coding agent's. Pooled, stopping a typed turn
  would silently deny a card a spoken worker was waiting on. Answering ONE
  needs no pool: the frontend hands back an rid and
  `Api.resolve_worker_permission` tries both agents for it.

## "No frontend attached" was said to an app that has one

*"And when the text agent spawns agents, it doesn't work."*

A sub-agent's event sink (`_CaptureEvents`) inherits `AgentEvents`, whose
`ask_permission` refuses with **"no frontend attached to approve this"** — a
message written for the CLI, arriving in the GUI. An `ask` handler was passed
only when the parent was `conversational`, so in voice mode a worker could be
approved out loud and everywhere else it could not be approved at all.

In the default `ask` mode that denied **every** `write_file`, `edit_file` and
`run_command` a spawned sub-agent tried, silently, with no prompt anywhere.
`spawn_agents` could not change a single file unless the user was in `yolo`.
"It doesn't work" was exactly right.

- **`_worker_ask` is the mechanism, not the main permission modal.** It is
  per-request (`rid`) and blocks only the asking thread, so several sub-agents
  asking at once queue instead of overwriting each other. `spawn_agents` runs
  up to `MAX_SUBAGENTS` in parallel; the modal was a single slot.
- **The card carries a name, not an id.** A `spawn_agents` sub-agent has an
  aid like `sa9f3c21-2` and no entry in the worker registry, so looking its
  name up there would put that string in front of the user.
- **A typed spawn does not speak.** `worker_permission` queues its sentence
  for TTS, so sending one from a typed spawn would have the app start talking
  at someone who never opened voice mode. `spoken` is empty unless the parent
  is conversational.
- **Cancelling a turn releases the blocked asks.** A sub-agent parked in
  `_worker_ask` is not watching `self.cancel` — it is waiting on an Event for
  five minutes — and `spawn_agents` JOINS its threads. Without it a cancelled
  turn sits there with the app showing a stopped turn that has plainly not
  stopped.

**The card is one queue with two sources.** `permId = ev.id` was right for the
main agent (one thread, blocked on the answer) and wrong for sub-agents. Each
entry says which backend call answers it — `resolve_worker_permission` by rid
for a sub-agent, `permission_response` by id for the main agent — rather than
the card guessing. A spoken worker's question still goes to the dock's card
instead: it is answerable by saying yes, and a modal over the app is not.

## The inspector is the same panel, whoever dispatched the work

A worker's id IS its sub-agent id, so the rail's pills had nothing to build —
they open the panel everything else uses. What they opened was **empty**.

`handleVoiceEvent` dropped `subagent` and `subagent_stream` on the floor, so a
worker dispatched by speaking never had a thread created (`subagent_stream` is
what builds it) and never had its status recorded (`subagent` is the only
thing that sets `subagentStatus`, which gates the steer/stop composer). The
panel came up blank and dead, on the worker you are most likely to want to
redirect. This is the same bug as *"worker_update was handled only on the
voice route"* one section down, found on the other side of the same seam.

**Only the transcript ROW is skipped on that route.** A spoken dispatch has no
turn in the chat to hang one on, and the rail already shows it.

## A worker's report outlives the voice session

Work dispatched by voice used to report back only to the delegator, which is a
separate voice-only agent. Close the overlay, type "what did that change?", and
the agent you are typing to had never been told anything happened.

`worker_report_note()` is filed in two places: the delegator's history, so a
spoken follow-up can be answered, and `ChatState.worker_reports`, drained into
the coding agent at the top of its next turn.

**Queued, not appended.** A worker finishes on its own daemon thread at a
moment nothing controls, and the coding agent may be mid-turn — writing into
`agent.messages` underneath it produces a `tool_call` with no matching reply,
which OpenAI-compatible APIs reject outright. `_drain_worker_reports` runs
inside `_run_send_turn`, under the turn lock, which is the one moment that
history is not being written by anyone else. The queue is bounded, because
every entry is spent from the coding agent's context the moment it next runs.

The note carries `WORKER_REPORT_PREFIX` so `sessions.to_display` keeps it out
of the rendered chat: it arrives in the `user` role (the role this app injects
plumbing under) and nothing else would distinguish it from something typed.

## The reasons are in git, and the agent could not reach them

This repository writes down *why*. Nearly every non-obvious decision carries the
failure that produced it — in the comment above the code, and in the commit
body, which for a squash-merged PR is the whole PR description. `CLAUDE.md`
opens a section with "Do not re-litigate these. Each cost a round trip to a real
phone."

The agent read none of it. It reads **code**, which is the one artefact that
cannot say what was tried and reverted: a line that came back looks identical to
a line never touched. So it proposes the thing that was measured on a device and
undone, and the only defence is that a human remembers.

`why(path, line)` is the fix, and it is mostly a reading of `git log -L`.

- **It answers even when git cannot.** The comment block is computed and
  returned first; a file outside a repository, or one never committed, still
  gets what the code says about itself, with a sentence explaining the absence.
  A tool that returns nothing for an uncommitted file is useless exactly when
  the agent has just written it.
- **A blank line ends a comment run, and indentation gates the enclosing
  block.** A comment separated from its code is usually about something else,
  and the nearest `def` *above* a line is frequently the one that already ended
  — a comment between two functions belongs to the one below. Handing back a
  neighbour's docstring reads exactly like an answer, which is worse than
  returning nothing, so both heuristics fail closed.
- **The docstring is not at `def + 1`.** Signatures here wrap routinely, so the
  scan walks to the end of the header (parens balanced, line ending in a colon)
  before looking. In this codebase the reason sits in the docstring at least as
  often as in a comment above it; looking only upwards finds half of it.
- **`_git()` takes an argv list, not a shell string.** The other git helpers
  interpolate into a shell command, which is fine for the paths they take.
  `git log -L` takes a `start,end:path` argument with a colon in it and a path
  that may hold spaces — quoting that for two different shells is a worse
  problem than using neither.
- **Trailers are stripped.** `Co-Authored-By` is plumbing, not a reason, and it
  costs context on every call.
- **The output ends by saying what it is.** Without the closing line it reads as
  a changelog, and a changelog is something you skim rather than act on. A
  commit that says an approach was tried and reverted is an instruction.

The system prompt names the trigger, because a tool the model never thinks to
call is not a feature: reach for it when a line looks *unnecessary* — a magic
constant, a workaround, a retry, an ordering that looks arbitrary, a guard that
looks redundant.

## A rate limit should slow you down, not stop you

The free tiers this app is built around are metered in requests per day and per
minute. Hitting one does not make you a worse programmer; it just stops you.
`model_fallbacks` is an ordered chain — "3.6 flash, then 3.5 flash, then 3.5
flash lite" — walked when the preferred model is refusing for quota.

Two things it costs, and both are worth paying:

- **The prompt cache.** This app re-sends ~12,400 tokens of system prompt and
  tool schemas on every request, and a different model has no cache for that
  prefix. But the alternative is not "keep the cache" — it is "get nothing",
  because the preferred model is refusing.
- **Quality.** A weaker model is worse at tool calling, and switching mid-task
  is where that shows.

The conversation itself is unaffected: history is plain OpenAI-format messages
and this app already switches models per chat.

What keeps the cost bounded:

- **Only a 429 walks the chain.** A weaker model is not the answer to a server
  fault or a bad request, and falling back on any error would quietly answer
  every hiccup with a worse model.
- **Switching does not sit out the backoff.** The wait is the thing the chain
  exists to avoid; sleeping the full `retry-after` and then switching gives up
  the entire benefit.
- **A rate-limited model is skipped for `MODEL_COOLDOWN`, then tried again.**
  Asking one that just refused for quota spends a request to be told the same
  thing — but a per-minute limit recovers in a minute, and being exiled to the
  weakest model for the rest of the session is the failure this must not have.
- **If the whole chain is cooling down, it asks the preferred model anyway.**
  Waiting beats refusing outright.
- **Cooldowns are per endpoint.** The same model name on another provider is a
  different quota.
- **Only the main turn gets the chain.** Sub-agents and compaction keep the
  plain path: the main turn is the long, tool-calling one a rate limit actually
  kills, and it is the one whose model the user chose.
- **The switch is said out loud, once.** A silent downgrade is the worst
  version of this — the chat quietly gets worse at tool calling and nothing
  anywhere explains why. Once per switch, not per request: news the first time,
  noise after that.

**The chain is a list you order, not a list you type.** It was a textarea, one
model name per line, and everything wrong with that was the same thing: the app
already knows every model you have configured and it was asking you to retype
them from memory.

- **A typo is silent, and silent at the worst moment.** The chain only matters
  when the preferred model is refusing, so a name that does not exist is
  discovered as a second failure on top of the first. A name the chat's API does
  not serve is now marked on its own row as one that will be skipped.
- **A fallback is a different model on the SAME client** -- same base URL, same
  key -- so a model another provider serves cannot work at all. The picker
  offers only what the chat's own API serves, which makes that unrepresentable
  rather than merely documented.
- **The numbers that decide the ORDER go on the rows.** What is left of today's
  allowance, and how many times the provider has actually refused, were in a
  different panel from the list they exist to justify.
- **The head row shows what the chain falls back FROM.** "Fall back to" never
  said, and the answer is per-chat.

Not done, and deliberately: no per-model context-limit check. Model context
windows differ, and a fallback with a smaller one can fail on a long chat — but
inventing a table of limits would be a guess that goes stale, and the failure
is loud rather than silent.

## Updating is a pull and a restart, and both halves fail quietly

The app is installed by cloning it, so the Update button in Settings → General
runs `git pull` and starts a fresh copy. Every part of that has a failure mode
invisible from a button, and a button that half-works leaves someone's app
directory in a state they did not ask for and cannot see.

- **Two steps, never one.** `check()` looks and changes nothing; `pull()` only
  runs after a clean check. A single-click "update" would do all of it to
  someone who wanted to know whether there *was* one.
- **Every refusal names the actual state** — local edits, detached HEAD, no
  upstream, not a git checkout at all. "Couldn't update" leaves a button that
  does not work and no way to find out why.
- **`--ff-only`.** A merge commit created by a button in someone's app
  directory is not something they asked for, and a *conflicted* merge leaves
  the app in a state it cannot run from. Refusing is recoverable; half-merging
  is not.
- **Local changes stop it.** The person editing their own copy is exactly the
  person most likely to press this.
- **The restart is spawned BEFORE the window closes,** and detached. Close-then-
  start leaves a gap where the app is simply gone, and a failure in that gap is
  indistinguishable from the update having quit the app for good. On Windows a
  child in the same console group dies with its parent, so `DETACHED_PROCESS`
  is not optional.
- **`sys.executable`, not `"python"`.** On Windows that may be a different
  install, or absent from PATH.
- **A running turn refuses the update.** Restarting mid-turn loses whatever the
  agent was part-way through, and an update is never that urgent.

## Reaching the user's own browser means going in through the side

`control_chrome` launches its own Chromium by default and that is unchanged: a
throwaway profile, or the dedicated agent profile behind "Remember browser
logins", and nothing it does can touch the browser the user is signed into.

For driving the browser they *already have open*, there are two routes and only
one of them is usable.

**`browser_connect_url` (the DevTools port) cannot reach a running browser.**
The port only opens at launch — there is no way to switch it on afterwards — so
the honest description of that feature was always "quit your browser and reopen
it with these flags", and current Chrome additionally refuses the port on the
default profile directory. It is kept, behind an Advanced disclosure, because
it gives real browser-level input events. It is not the way in.

**The extension is.** It is already inside the browser: same profile, same
logins, same tabs, no flags and no relaunch. So the direction of the connection
flips — the app stops trying to reach into the browser, and the browser dials
the app.

- **WebSocket, not a fetch loop.** An MV3 service worker is killed after 30
  seconds idle, and WebSocket activity is the documented thing that resets that
  timer. A poll loop would have the worker dying underneath it every half
  minute.
- **The server is hand-rolled, and `Origin` is the entire security boundary.** A
  WebSocket is not subject to CORS, so any page in any tab can open a socket to
  `127.0.0.1` and start issuing commands. The browser sets `Origin` and page
  JavaScript cannot forge it, so only `chrome-extension://` is accepted; the
  socket is bound to loopback so nothing off the machine reaches it at all.
  `tests/test_extension_bridge.py` drives a real client over a real socket,
  including the RFC 6455 §1.3 worked example for the handshake.
- **`stop()` joins the accept thread before closing.** Closing a listening
  socket while another thread sits in `accept()` does *not* release the port —
  the close waits on the blocked call, which never returns. Without the join a
  stopped bridge holds its port for the life of the process, and the next one
  silently walks down to the next port in the list. This was found by a test
  that started and stopped eight bridges.
- **The extension gets a Playwright-Page-shaped object, not a second set of
  ops.** `ExtensionPage` implements the dozen calls `_op_*` actually makes, so
  the snapshot, the stable refs, the region grouping, the error messages and the
  Browser Agent's whole prompt are untouched. Teaching every op a second way to
  work would have been two subtly different pages described to one model.
- **The snapshot is GENERATED into the extension** (`scripts/gen_extension_page.py`).
  MV3 forbids `unsafe-eval`, so an extension cannot run a JavaScript string it
  was handed — the snapshot has to *be* code inside it, and a second hand-written
  copy would drift from the one Playwright gets.
- **Clicks are a full event sequence, and fills go through the native value
  setter.** `el.click()` skips the pointer and mouse events frameworks actually
  listen on, and `el.value = x` is ignored by React, which tracks its own
  last-rendered value and concludes nothing changed. Both show up as "the page
  did nothing".
- **`is_attached` is true for both routes**, because everything keying off it
  cares only whose browser this is. The Browser Agent gets
  `BROWSER_ATTACHED_NOTE` either way.

**The install is the feature.** It cannot be automated away — Chrome has no API
for loading an unpacked extension and the Web Store is not a route this project
can take — so the only thing left is to remove every step around it:

- **Browsers are found and named** (`installed_browsers.py`), and each gets a
  button that opens its own extensions page. "Open chrome://extensions" assumes
  one browser and assumes it is Chrome; someone who lives in Edge reads that,
  installs it in the wrong browser, and wonders why nothing connects. Passing
  the `chrome://` URL on the command line is the only way to reach that page
  from outside the browser, and a running browser answers it with a new tab.
- **The port opens while the panel is on screen, before the switch is on**
  (`status(cfg, listen=True)`). It used to open only once the feature was
  enabled, so the sheet said "Waiting for the extension…" forever to anyone who
  had not flipped the switch first — there was nothing to wait on. Verifying
  the install *before* handing over a logged-in browser is also the right order.
- **The sheet carries the switch.** Finishing the instructions and then having
  to find the control you were sent away from is where people stop.
- **Connected-but-off is called out as nearly done**, not shown as an error.
  It is the state everyone lands in after installing.
- **The panel names the tab it would act on.** "My own browser" is otherwise a
  leap of faith taken at the moment the agent starts clicking things.

**An open socket is not evidence of a live browser** — and an *answering*
socket is not evidence of a live service worker. `connected` used to mean "a
file descriptor exists", which produced the worst version of this feature:
Settings said *Connected* while every browser action sat for the full timeout
and then failed. A laptop that slept, a browser that was killed, a FIN that
never arrived — all leave a socket that reads as fine and answers nothing. So
the bridge pings on a heartbeat and `connected` asks when the extension was
last heard from.

**That was still not enough, and `background.js` said why in its own
comments:** a WebSocket ping is answered by Chrome's network stack *without the
service worker being woken at all*. The worker is what runs `tabs`, `snapshot`,
`click` — everything. So a reaped worker leaves a socket that pongs forever and
answers nothing, and the app called that Connected while `browser_tabs` failed
with "the browser did not answer in time". The same report, one level down.

- **Only what the EXTENSION said counts** — a `hello`, a `keepalive`, or a
  reply. `_last_message` is tracked apart from `_last_seen` because they are
  evidence of different things.
- **A connection that never sends keepalives keeps the old any-frame rule.**
  The extension is loaded unpacked, so a copy predating them is a real
  possibility, and declaring it dead every fifty seconds for speaking an older
  language would be a worse bug than the one being fixed.
- **Dropping the dead one IS the recovery**, not bookkeeping. The extension
  re-dials on its next wake and a fresh connection replaces it; holding the
  corpse is what left every command timing out with nothing to fix.
- **`setInterval` does not survive a reap** any more than `setTimeout` does —
  the same lesson, eighty lines further down the same file. The keepalive is
  the only thing telling the app the worker is alive, so the reconnect alarm
  restarts it.
- **The alarm is no longer cleared on a successful connect.** It was, and the
  reason given was true at the time: waking the worker every thirty seconds for
  a live connection is pure cost. The keepalive changed that — while connected
  the worker is deliberately held *awake*, so there is nothing to wake and the
  cost is zero, while clearing it removed the only timer that outlives a reap.
- **`connect()` guards on the socket being USABLE**, not merely non-null. A
  socket reaching CLOSING/CLOSED without `onclose` having run made it a
  permanent early return, and the extension never re-dialled.
- **The timeout message names the cause.** "Did not answer in time" on its own
  sent every report of this to the wrong place: the user reads Settings, sees
  Connected, and concludes the app is lying to them.

The heartbeat **ticks finely and decides from elapsed time** rather than
sleeping for the whole interval: a loop parked in an eight-second wait cannot
notice a shutdown, and a test that shortens the timings finds a loop that never
re-reads them.

**`my_tabs` is on the CODING agent, not just inside the Browser Agent.** Asked
"what tabs do I have open?", the model said it had no access — true, since
`control_chrome` was its only browser tool and nothing said the user's own tabs
were reachable. A question does not deserve a sub-agent either. It is offered
unconditionally rather than only when connected, so the answer is "your browser
isn't connected, here is why" instead of "I can't": a tool the model does not
know exists cannot tell it anything.

**Three ways this fails in silence, all of them found by one bug report.** The
symptom was a second Chrome window opening with a blank tab, which says nothing
about any of the causes:

- **An MV3 service worker is killed after 30 seconds idle, and a pending
  `setTimeout` does not survive it.** The reconnect loop was a `setTimeout`, so
  the ordinary install order — load the extension while the app is not yet
  listening — burned a few retries, got terminated, and never tried again.
  `chrome.alarms` is the one timer that outlives the worker; `tabs.onActivated`
  and `windows.onFocusChanged` cover the gap below the alarm's 30-second floor,
  since browsing wakes the worker anyway. `connect()` must therefore stay a
  no-op when a socket is already USABLE, or browsing sprays connections --
  guarded on `readyState`, not on the variable being non-null, since a socket
  left CLOSING/CLOSED without `onclose` having run would otherwise make this a
  permanent early return.
- **The port was opened lazily, by the Settings panel's status call.** So a
  normal launch — app starts, setting already on, nobody opens Settings — left
  nothing to connect to. `boot()` opens it now when the setting is on, and only
  then.
- **The fallback was silent.** With the setting on and nothing connected,
  `control_chrome` quietly launched a separate browser. It warns now
  (`not_connected_hint()`), naming the port and what to check, because the
  feature looked broken rather than off.

**A blink is not a failure.** The socket goes away for ordinary reasons — Chrome
recycling the service worker, an extension reload, a browser restart — each a
gap well under a second. A call arriving inside one waits for the reconnect and
retries on the new socket, because a task dying because the transport blinked is
not something the model or the user can act on. Only READS are repeated
(`SAFE_TO_REPEAT`): a click whose connection dropped might or might not have
happened, and a form submitted twice is not something a later snapshot undoes.

**`SO_REUSEADDR` means two different things.** On Unix it only permits rebinding
a port left in `TIME_WAIT`. On **Windows** it permits binding a port that is
already in ACTIVE USE, and which socket then receives connections is undefined —
so a second copy of the app can take the port from under a running bridge, and
the extension starts getting `ERR_CONNECTION_REFUSED` while the first app still
believes it owns the socket. `SO_EXCLUSIVEADDRUSE` is the Windows option that
means what `SO_REUSEADDR` means everywhere else.

**The connection log is in Settings because nothing else could settle it.**
Every report of this feature came down to "it said connected but wasn't", and
neither end could say when it went or why: Chrome's extensions page timestamps
its errors too vaguely, and the app was throwing the information away. The
bridge keeps the last dozen connect/drop events with reasons and real
timestamps, and `boot()` no longer swallows the one failure the user cannot see
from outside — a port that never opened.

**The whole feature is: open some tabs, open the app, ask for something, and it
works.** Anything the user has to do beyond installing the extension once is a
bug in this feature, and it has had two.

**There is no second opt-in.** `browser_own` defaults to `"auto"`, meaning the
extension is used whenever it is connected. Loading an unpacked extension into
your own browser is already a deliberate, several-step act — requiring a switch
afterwards meant people finished the hard part and found nothing worked. It is
a NEW config field rather than a flipped default on the old one, because the
old one defaulted to `False` and a persisted `False` cannot be told apart from
a choice. `"off"` is the way out, and it closes the port.

The cost is that the port is open by default. That is the price of the
extension ever being able to reach anything on first install, and it is
loopback-bound and `Origin`-gated (above).

**The agent chooses a tab; it does not inherit one.** Driving "the active tab"
silently meant whatever happened to be in front, and it followed the user
around as they switched tabs. `browser_tabs` / `browser_switch_tab` /
`browser_new_tab` are added to the Browser Agent's schemas **only when
`supports_tabs`**, since a launched browser holds the single page this app made
it and three tools that always answer with the current page are noise in the
longest prompt in the app.

- **Their open tabs are the workspace, and the prompt says so.** A first
  version told the model to *prefer a new tab* — reasoning about not hijacking
  the page someone is reading — and that fights the actual use case head on:
  "do something in those tabs" is the normal request. The rule is now: list the
  tabs, work in the one the goal is about, and open a new tab only when the
  goal needs a page that is not open yet.
- **A selected tab is pinned in the extension** (`pinnedTabId`), so a long task
  stays where it was put. Dropped when that tab closes.
- **Switching brings the tab to the front.** The user should be able to see
  where the agent is working, and a background tab throttles timers and
  rendering in ways that make a page behave differently.
- **`supports_tabs` is answered from the backend, not the page.** The page only
  exists after `start()`, and a property that said False until then would drop
  the tab tools for anyone who asked in the wrong order.
- **The fallback warning is skipped when `browser_connect_url` is set.**
  Someone who deliberately configured the DevTools route has not asked about
  the extension.

**Nothing can open another program's `chrome://` page.** Chrome refuses those
URLs on the command line (they were a malware vector), a web page cannot link
to them, and there is no API. Passing one anyway is *worse than doing nothing*:
the browser drops the URL and opens an empty window — which is exactly what a
button labelled "Open in Chrome" appeared to do, and it read as the whole
feature misfiring. The address is copied for pasting instead, the panel says
why, and the per-browser button only brings that browser to the front (with no
argument, a running browser is focused rather than handed a blank window).

What the extension route gives up: input is dispatched as page events, so a site
that checks `event.isTrusted` (bot detection on sign-in pages, mostly) will
refuse it. The launched browser uses real browser-level input and remains the
better tool there. Say so rather than letting it look broken.

## The browser agent is a worker, not a blocking call

`control_chrome` ran the Browser Agent inline and the whole conversation waited
on it. That is the wrong shape for the one thing the user is most likely to be
*watching*: they could not ask about it, could not redirect it, and could not
get on with anything else. Steering existed, but only through the sub-agent
panel — the agent they were talking to was frozen.

It is a background worker now (`background=true`), reporting through the same
registry as every other one, so `check_workers` / `steer_worker` / `stop_worker`
apply to it unchanged. Voice reaches the same thing with
`dispatch_worker(kind="browser")`, which is the shape that was asked for: watch
the browser, talk to the assistant, have it steered mid-flight.

- **The coding agent had NO worker tools.** Not an oversight to fix later —
  it is *why* a background browser was impossible. Everything it could delegate
  blocked until it finished, so there was never anything running to ask about.
  `WORKER_SCHEMAS` gives it the same three the spoken side has.
- **Blocking stays the default.** A quick look whose answer is needed to
  continue is a worse conversation as a worker, not a better one.
- **The browser is opened before the thread starts.** "The browser would not
  open" is an answer the model can act on; raised on the worker thread it would
  land in a report nobody is waiting for.
- **Workers are named from their goal** (`open-dashboard-check-error`).
  `steer_worker` takes a name as well as an id, and "wk3" is not something
  anyone says out loud — least of all in voice mode, where this matters most.
- **The phone shares the schema and has no browser.** `kind="browser"` there
  returns a refusal naming `needs_desktop`. A shared schema that silently did
  something else would have the model report browsing it never did — which is
  exactly the failure the parity tests exist to prevent.

## The agent's browser and the user's browser are two different things

`control_chrome` launches its own Chromium — a throwaway profile, or the
dedicated agent profile behind "Remember browser logins". Nothing it does can
touch the browser the user is signed into, and **that stays the default**.

`browser_connect_url` is the opt-in opposite: attach over CDP to a browser the
user is already running and drive the window in front of them, as them. It is
strictly more dangerous and exists because the user asked for the choice — so
the job is to make it honest, not to make it safe.

- **The port cannot be opened on a running browser.** There is no way to switch
  DevTools on after the fact, so attaching always means quitting the browser and
  reopening it with `--remote-debugging-port`. Current Chrome also refuses the
  port when it would use the default profile directory, so `--user-data-dir` is
  not optional — pointed at a copy of the profile is how logins come along.
  `DEBUG_PORT_HINT` carries the per-platform command and is returned from the
  failure path *and* the check, because "nothing is listening" and "here is what
  makes something listen" are one conversation.
- **Teardown disconnects and nothing else.** `context.close()` or
  `browser.close()` reaches across the connection and shuts the user's own
  windows. `_real_attach`'s teardown is `pw.stop()` alone, and
  `tests/test_browser_attach.py` asserts it against the real function rather
  than a fake of it — that is the test that catches someone tidying it up later.
  A tab opened because the browser had none is left behind for the same reason.
- **The tab is chosen by `document.visibilityState`, not by index.** Playwright
  has no active-tab concept and `context.pages` is creation order, so `pages[0]`
  is whatever they opened first. A foreground tab reports `visible` and every
  background tab reports `hidden` — the page telling us itself. The search runs
  across every context, since a second (or incognito) window is a separate one.
- **The viewport is re-read from the real window.** Every snapshot header quotes
  `self.viewport` and `browser_click_at` *refuses* coordinates outside it, so a
  stale 1280×800 would both describe the page wrongly and then reject the right
  click for being out of bounds. Launched sessions ask for their size and are
  unaffected; only the attach path syncs.
- **The endpoint must be localhost.** Not a security boundary — the debugging
  port is unauthenticated, so anything that can reach it has already won — but a
  typo that aimed this at another machine would point the agent at someone
  else's signed-in browser. `_normalize_connect_url` refuses out loud and stores
  nothing; a setting that silently ignored a bad value would look broken.
- **`BROWSER_ATTACHED_NOTE` goes in the SYSTEM prompt.** Not the turn: it is
  what the model *is* this time round and has to hold over every improvised
  action, not just the first. A model that believes it is in a scratch profile
  will click "Sign out" to get a clean login form, or empty a cart to start
  over — reasonable in a sandbox, destructive in the window someone lives in.
  It is appended *after* `.format()`, so it is free to contain braces.

## Swedish came back as HÃ¤r Ã¤r — one line of requests' documented behaviour

Reported with a photograph: every non-ASCII character in a Gemini reply arrived
as the Latin-1 reading of its own UTF-8 bytes, and got stored in the chat that
way.

`iter_lines(decode_unicode=True)` decodes using `resp.encoding`, and
`resp.encoding` comes from the Content-Type header — where, for a `text/*` type
carrying **no charset**, RFC 2616 says ISO-8859-1 and requests obeys it.
Server-sent events carry no charset, because the WHATWG spec defines them as
UTF-8. So a provider sending a bare `text/event-stream` was mangled and one
sending `charset=utf-8` was not, which is why this looked like one model's
problem rather than ours.

`resp.encoding = "utf-8"` before the loop. Not a guess about a provider — it is
what both specifications covering that stream already say it is (SSE is UTF-8;
JSON is UTF-8 by RFC 8259).

**The test drives real `requests` objects**, not a fake of the decode. The bug
lives entirely in what requests does with a header, so a mock of the decoding
step would only have agreed with whatever I believed it did. One test asserts
the surprising half directly — that the same stream decodes correctly the
moment a charset appears — because the whole fix rests on it.

## A read can be abandoned; nothing else can

Found by scanning rather than by being reported, which is the point of having
scanned. Two facts that only bite together:

- **`web_search`'s default backend had no timeout.** `DDGS()` takes one and was
  not given one, and DuckDuckGo is the default whenever no Tavily key is set —
  the ordinary case on the free tier this app is built around.
- **Stop reached a shell command and a sub-agent, and nothing else.** A network
  read has no process to kill and no cancel Event to set.

So a hung search was unbounded AND unstoppable: no way out but killing the app.

`interruptible()` is the general answer. You cannot kill a thread in Python;
what you can do is stop WAITING for one. The call is **abandoned, not
cancelled** — it finishes into nothing on its own timeout — which is only
defensible for a read, the same line the extension bridge draws with
`SAFE_TO_REPEAT`. `run_command` deliberately does not use it: a command that
might have half-run is not something to walk away from, and killing its process
tree is the harder guarantee it needs.

- **It waits in slices** (`_INTERRUPT_POLL`) so a Stop is acted on within a
  poll interval rather than at the mercy of the far end.
- **A stale cancel is cleared on entry.** Tokens are per tool call, but a flag
  left set would make the next read fail instantly for no reason.
- **The pool is shut down with `wait=False`.** Waiting for the abandoned call
  to wind down would give back exactly what giving up was for.
- **No token means no machinery.** The CLI and the tests have nothing to stop,
  and the call should not be pushed onto a thread for nothing.

## The extension caps the page text, and the cap has to travel with it

`ExtensionPage.inner_text` asks for at most `TEXT_LIMIT` characters, and Python
pages through what it gets. So a cap applied silently on the extension side
would have `browser_read` report a TRUNCATED document's length as the page
total: the agent pages tidily to "the end" and is told it read all of it.

That is the same defect `browser_read` was just fixed for, one level down — and
it arrived *with* the paging rather than being found by it. `page.js` returns
`{text, total}` now, and the read says the rest is beyond what the extension
will hand over, naming what to do instead, rather than offering an offset that
would come back empty. A bare string from an older unpacked copy still works.

## Stop has to reach what the turn is waiting ON

Reported: *"interrupting is not reliable, especially when it's using tools,
they can't be interrupted."*

`self.cancel` is a flag checked BETWEEN steps and BETWEEN tool calls. That
stops a thinking turn instantly and does nothing at all for a turn blocked
inside a tool — which is most of the time a turn is slow enough to want
stopping. Three things a turn blocks on, and none of them watch a flag:

- **A sub-agent.** `spawn_agents` and an inline `control_chrome` JOIN their
  threads, and a sub-agent has its OWN `cancel` Event. Cancelling only the
  coordinator left Stop waiting for up to `MAX_SUBAGENTS` missions to each run
  to completion. It recurses, because a browser agent inside a worker is two
  levels down, and one sub-agent raising must not spare the others.
- **A shell command.** It is a process, and what stops a process is killing its
  tree. The per-command Stop button could always do this; the button that stops
  the TURN could not, so a turn stuck on a dev server ignored it.
- **A permission card**, which is an Event with a five-minute timeout. Already
  handled, now pinned.

**`tools._call_token` is thread-local on purpose** — so parallel chats cannot
see each other's — which is exactly why the GUI thread calling `request_cancel`
cannot read it. The agent keeps its own copy on the instance, set beside the
thread-local and cleared in the same `finally`.

## The shell tool is named after the platform, not after PowerShell

`run_command` execs PowerShell on Windows and bash (falling back to `/bin/sh`)
everywhere else. It was hard-coded to `powershell`, which meant it could not
**start** on macOS or Linux — every command raised "Failed to start
PowerShell" — while `prompts.py` was already telling the model it had a POSIX
shell there, and `run_check_command` eighty lines below had the platform branch
all along.

Two things about that are worth keeping in mind:

- **The name is part of the interface.** The tool description and the
  `Shell:` line in the system prompt are both generated from
  `tools.shell_name()`, so they cannot drift from what actually gets exec'd.
  Telling the model "PowerShell" on a Mac is how you get `Select-String` and
  `$env:` in a command that then fails for reasons the model cannot see. The
  same goes for bash-vs-`sh`: a model told "a POSIX shell" writes `[[ ]]` and
  arrays at some rate, and on Debian `/bin/sh` is dash, which rejects them with
  syntax errors that read as "the command was wrong".
- **`run_powershell` still resolves**, in `TOOL_FUNCTIONS` and in the
  permission engine's `SHELL_TOOL_NAMES`, because saved sessions replay tool
  calls by name and session allowlists are keyed on it. It is accepted from
  history, never offered to the model.

Why this survived so long is the more useful lesson. CI runs `ubuntu-latest`
and was green, because `tests/test_stop_command.py` substitutes a `FakePopen`
for the PowerShell that is "absent on Linux CI" — so the one platform CI
actually exercises was the one where the real tool could not start.
`tests/test_shell_platform.py` now runs a real command through the real tool,
mocking nothing, on whichever platform it finds itself on. **Do not let that
file grow mocks**; every other test of these tools already fakes the process,
and this one exists to be the one that doesn't.

The POSIX shell is started with `start_new_session=True` so its pid is also
its process-group id, and a stop signals the group. `proc.terminate()` reaches
only the shell, so a stopped `npm run dev` would leave the actual server
holding the port — invisibly, since the app believes it stopped it. That is
the same reach `taskkill /T` gives on Windows.

## The phone commits as it edits, so the branch is decided first

There is no filesystem on the phone. `write_file` and `edit_file` are commits,
made the moment the model calls them, to whichever branch the chat is bound to
— and that was always the repository's **default** branch, because opening a
repo is the only way the phone gets one and it opens the default. So work done
from a phone went straight to `main`, unreviewed, and on this repo it deployed
on the way past.

`new_branch` and `open_pull_request` close that. Four things about them are
load-bearing:

- **Switching branches rebuilds the tools, it does not re-point them.**
  `makeTools` keeps a `path -> {text, sha}` cache, and a cache carried across a
  branch switch is the worst available kind of wrong: reads *succeed* and hand
  back the other branch's text. `bindBranch` builds a new `makeGitHub` client
  and a new tool set from it, which is what drops the cache.
- **It is not `connectRepo`.** Connecting a repo resets the chat — messages,
  transcript, the queue of work parked for the desktop. None of that belongs to
  the branch, so the two were split rather than one calling the other with a
  flag.
- **The switch is persisted immediately**, not at the end of the turn. The
  branch is where every subsequent commit goes; a relaunch that came back on
  the old one would carry on writing to it.
- **Sub-agents get neither tool.** One of them moving the chat onto another
  branch underneath the agent that dispatched it is not something that
  conversation could recover from.

The system prompt states the rule the tools exist for — *every edit is
committed immediately, so branch first* — because a tool the model never
thinks to call is not a feature.

## "Did CI pass" is a different question from "do the tests pass"

Both devices can now ask it, for different reasons and by separate routes.

On the phone it closes the loop `new_branch` and `open_pull_request` opened:
nothing runs there, so *did the tests pass* was the one step in **branch → edit
→ commit → pull request** that still had to be answered somewhere else. It is
also the most phone-shaped question in the app — you push, you walk away, and
you want to know ten minutes later without going back to a desk.

On the desktop the shell is not the answer either. It can run the tests
*here*; it cannot run the ones on a **runner**, and those are what gate a
merge. Asked "did CI pass" the agent could only say to go and look — the same
answer it gave before it could push at all.

`glmcode/tools.py:check_ci` and the `check_ci` in `mobile/agent-core.js` are
two implementations of one shape. The rules are the part that must not drift,
because a model that learns them on one device carries them to the other:

- **`check-runs`, not the older `statuses` endpoint.** Actions reports through
  checks, and a repository using both would otherwise show half its answer.
- **`skipped` and `neutral` are passes.** A conditional job that did not need
  to run is not a broken build, and calling it one makes this cry wolf on every
  repository that has one. `tests/test_check_ci.py` pins the two lists against
  each other.
- **A run still going is never reported as a pass.** "1 passed, 0 failed, 1
  still running" is the honest shape of a half-finished answer; collapsing it
  into green is how someone merges on the strength of a job that had not
  started.
- **"Nothing reported" covers both reasons.** "Not started yet" and "this repo
  has no CI configured" look identical from here, and saying only the first
  sends someone off to wait for a run that is never coming.
- **A check's own `output.summary`, and never a log.** A job log is served as a
  redirect to blob storage — tens of megabytes, and unreadable cross-origin
  from a page at all — so promising one would be a tool that fails on exactly
  the case it exists for. The summary is capped: one is written for a web page
  and can run to thousands of lines.

The desktop adds the one thing the phone cannot know: **whether what is on the
runner is what is on this disk.** It counts unpushed commits and says so, since
a green answer about a commit two pushes ago is the most confidently wrong
output this tool could produce.

## What the phone cannot borrow from the desktop, and why

Three tools stayed behind, and the reasons are different in each case. They are
written down so the question is not reopened by inspection of the tool lists.

- **`web_search` and `fetch_url` are blocked by CORS, not by the CSP.** The
  desktop scrapes DuckDuckGo HTML; a page cannot read a response from a host
  that sends no `Access-Control-Allow-Origin`, and no `connect-src` entry
  changes that. Reaching them needs a proxy, which is a server, which this
  project does not have.
- **`why` reads the FILE's history, not the line's.** `git log -L a,b:file` has
  no API equivalent — GitHub filters commits by path and no finer. The phone's
  `why` says so in its own output, naming the line it could not narrow to,
  because implying a precision it does not have would hand back a confident
  wrong reason.
- **`remember` writes somewhere else.** The desktop's memory lives outside the
  repo (`~/.makenomistakes/memory.md`); everything the phone can write is a
  commit. So its notes go into the project's own agent file, which the desktop
  already reads into its system prompt. **Which** file matters:
  `prompts._project_memory` returns the FIRST of `GLM.md` / `AGENTS.md` /
  `CLAUDE.md` that exists, so creating `GLM.md` in a repo that has a `CLAUDE.md`
  would silently shadow it and the project's real instructions would stop being
  read. It appends to whichever is already there, and only invents a name when
  there is none.

Two things that ARE shared and must stay shared: argument names, and the
trigger sentences in tool descriptions. A chat syncs between the devices and
carries the other one's calls in its own history — so a model reading its past
`todo_write(content=...)` against a schema saying `task` will use whichever it
read last, and an argument the phone silently ignores is a read it believes it
did and did not.

## Reading the repo from the phone is a network round trip per file

`grep` and `search_code` scan every file, and each one is its own HTTPS
request. In series that is the difference between a search and a stall: this
repository is 225 scannable files, and at ~150ms each that is over half a
minute before the model sees anything.

They are read in ordered **chunks** now (`scanFiles`, eight at a time). The
same *number* of requests either way — nothing is spent faster against the
GitHub rate limit — but a chunk is not a race, and the difference is two things
that are easy not to notice:

- **Results stay in tree order.** Output that reorders itself between two runs
  of the same search is output nobody can compare.
- **The early stop stops.** `grep` quits at 100 hits; with everything in flight
  it would quit after whatever had already been scheduled, which on a large
  repo is all of it.

`read_file` had the matching problem in the other direction: it took the first
12,000 characters of the joined file and said nothing. The model concluded the
symbol was absent — a wrong answer rather than a missing one — and the cut
landed mid-line, so `edit_file` was handed `old_string` values that never
existed. It cuts on a line boundary now, says how many lines are left, and
names the offset to continue from.

## A write to an existing path needs the sha, and the sha comes back from the write

`makeTools` cached a file's text after a write with `sha: undefined`, and
GitHub refuses to overwrite a path it is not told which blob is being replaced.
So the FIRST edit of a file worked and every one after it went out with no sha
and came back 422 — on the device where every write is a commit and editing the
same file twice is the normal shape of a task.

The new sha is in the response to the previous write. A cache entry that
somehow has none re-reads the file rather than sending nothing.

The fake GitHub in `tests/test_phone_tools.py` enforces that rule. That is the
only reason a test can see this at all — a fake that accepted any PUT would
have passed throughout.

## Reverting a worker has to mean that worker

`revert_worker` called `BackupRepo.revert_to(baseline)`, which is
`reset --hard` plus `clean -fd`. That is the whole work tree, so undoing one
worker also undid **everything done since it started** — another worker's work,
and the user's own edits — and the `clean` additionally deleted untracked files
that were never part of any diff: build output, scratch notes, a half-written
file. The old return string admitted the first half of this, *after* it had
already happened.

The fix needed something the app did not have: knowing which files a
*particular* worker wrote.

- **`_emit_subagent_stream` is where that is knowable.** Every sub-agent event
  funnels through it tagged with the sub-agent's id, which for a background
  worker IS the worker id. A `tool_call` for `write_file`/`edit_file` names a
  path, so the worker's own writes can be recorded as they are announced.
  `replace_in_files` is deliberately not in that map: it takes a pattern, and
  guessing a path would be worse than knowing the tool is not covered.
- **The revert set is the INTERSECTION of two imperfect sets.** `wrote` is
  exact about whose change it was but records *intent* — the event fires before
  the permission engine has had its say, so a denied write is in there.
  `changes` (the baseline diff) is exact about what really changed but contains
  every edit anyone else made meanwhile. A file in both is one this worker asked
  to write and that is genuinely different now. Nothing else is safe to touch on
  its behalf.
- **Nothing attributable means refusing.** Files changed while it ran but none
  are ones it wrote — most likely it edited them by running a command. Reverting
  the tree and explaining afterwards is the behaviour this replaced.
- **A running worker is refused.** The phone's `revert_worker` always did this;
  the desktop did not. Reverting under a worker that is still writing leaves the
  files half one thing and half the other.

`BackupRepo.revert_paths_to` is the per-path instrument. Two things in it are
load-bearing, and the second was found by its own test:

- **`add -A -N` first**, exactly as `changed_files_since` does. Without it a
  file *created* since the baseline is not in the index, and cannot be told
  apart from one the shadow repo excludes entirely.
- **A path the shadow repo does not track is never deleted.** "Not in the
  baseline" has two causes and only one of them means "created since". The other
  is `DEFAULT_EXCLUDES` — `.git/`, `node_modules/`, build output — and there is
  no snapshot of those to restore from. The first version of this deleted the
  project's real `.git/config`, which is why the check lives in `BackupRepo` and
  is not left to the caller.

## "Be honest" is not an instruction; a verdict slot is

Reported: *"often they fail a task but they lie and say they succeed."*

The model does not think it is lying, so a plea for honesty changes nothing.
Three things in the old report formats produced the over-claim, and
`HONEST_REPORT_RULE` answers each:

1. **Every slot presupposed success.** "What you did", "what you
   accomplished" — there was nowhere to put what did *not* happen, so a
   narrative of activity was the only shape available and the reader inferred
   the rest. The report now opens with a verdict: `DONE` / `PARTIAL` /
   `FAILED`, each defined rather than merely named.
2. **Failure was a conditional appendix** — *"if you could not complete the
   mission…"*. A partial success never classifies itself as one, so it rounded
   up. `PARTIAL` is a first-class outcome that has to list what is finished and
   what is not, separately.
3. **Nothing asked how it KNEW.** A sub-agent that wrote a file and ran nothing
   had no reason not to call that done. `DONE` now requires having checked, and
   an unverified claim must carry that *in the same sentence* — a caveat at the
   end reads as covering the whole report rather than the one claim it belongs
   to.

**The load-bearing sentence is the reassurance, not the prohibitions.** The
over-claim comes from believing failure is punished, so the rule says outright
that an accurate `PARTIAL` costs nothing and that overstating is the only
reporting mistake with a real price — because it is the one that gets built on,
and it resurfaces later somewhere it makes no sense.

**What counts as evidence is per-place, and stated per-place.** A sub-agent is
told the two claims the coordinator builds on (tests passing; a file actually
written). The Browser Agent is told that evidence means the page *after* the
action — a click it did not see take effect did not take effect — and that
being blocked by a login or a captcha is ordinary weather, because a model that
reads blocking as failure invents a result instead. The phone is told that
`DONE` is rarely honest there at all: it cannot run anything, so "and you
checked it" is a bar it can almost never clear, and without saying so it would
read the definition, find nothing it could have run, and use `DONE` anyway.

**One rule, four places, generated.** Same reasoning as `UNTRUSTED_INPUT_RULE`:
the desktop's sub-agents, the Browser Agent and the phone's workers are the
same kind of thing answering to the same person, and a rule that holds in three
of them is worth much less than it looks. It is inlined into each prompt at
import rather than at `.format()` time — callers format these with their own
fields, so a placeholder they know nothing about would raise `KeyError` on
every spawn.

## A finished sub-agent is worth more than its report

Asked for: *"the ability to continue subagents after they finish — the main
agent should be able to use some resume tool on them with more prompt."*

The gap was structural. `_run_single_subagent` popped the sub out of
`_active_subagents` in a `finally` and returned its report TEXT, so the moment
it finished the Agent was dropped — its whole conversation, every file it had
read, everything it had worked out. A follow-up meant `spawn_agents` again with
a fresh sub-agent that had to be told all that background a second time, in a
prompt the coordinator had to reconstruct from a report.

`resume_agent(agent, task)` is `steer_worker` one state later: steer while it
runs, resume once it has stopped.

- **The id does not change**, so the sub-agent inspector's thread continues
  where it left off rather than opening a second one, and for a worker
  `revert_worker` still undoes the whole line of work — its baseline is the
  ORIGINAL dispatch, which is what "undo that worker" should mean.
- **A running one is sent to `steer_worker` by name.** "No agent matches" would
  have the model spawn a duplicate of something already doing the work.
- **Only the last `MAX_RESUMABLE` are kept.** Each is holding its entire
  conversation, so this is a memory bound rather than a policy. Asking for one
  that has been dropped says so — silently starting a blank sub-agent under a
  name the coordinator believes it is continuing is the worst version of this.
- **`RESUME_PREAMBLE` is framed like `STEER_NUDGE_TEMPLATE`,** and for the same
  reason: it arrives directly after a final report, which is the one place the
  model has just been told to stop. It leads with the instruction, says the
  earlier report is already delivered, and ends on the new mission.
- **Both devices, one preamble.** It is in the generated block
  (`scripts/gen_mobile_core.py`), because a chat syncs and carries the other
  device's calls in its own history — a paraphrase on one side is a different
  instruction with nothing to say so.

Found while writing it: `spawn_agents`' own description still told the model
its sub-agents were *"effectively read-only in ask mode"*. That stopped being
true when sub-agents gained a permission card, and it is exactly the kind of
stale sentence that quietly stops a feature being used — the model reads it and
declines to delegate anything that writes.

## A worker that did not finish is not a worker that failed

Four places described any non-`done` worker as a failure, and told the user and
the model so.

- `worker_report_note` and the spoken `announce_worker` both said "failed" for a
  worker the user had **stopped on purpose**. Telling the coding agent a
  cancelled job crashed invites it to diagnose and re-run exactly what was just
  cancelled. `_WORKER_OUTCOMES` maps each status to what it actually means, and
  an unrecognised status claims neither success nor failure.
- `check_workers` counted only running/done/error, so a chat whose one worker
  had been stopped opened with "0 running, 0 done, 0 failed" and then listed it.
  The header is now built from the same statuses the rows print.
- A `reverted` worker fell into that summary's `else` branch, whose `error` is
  `None` — so undoing a worker printed **"FAILED — None"**.
- **A crashed worker recorded no changes at all.** `w["changes"]` was set on the
  success path only, so `worker_changes` and `revert_worker` both believed a
  worker that died half way through had touched nothing — and that is the one
  you most want to be able to undo.

`worker_changes` on a *running* worker had the same shape of bug: `changes` is
filled in at the end, so an empty list mid-flight means "not known yet", and
reporting it as "changed nothing" is a wrong answer about work in progress. It
reads the live diff instead and says the worker is still going.

## A spoken worker name is a fragment, not a sentence

`_resolve_worker` matched both directions — `ident in nm` **and `nm in ident`**.
The second is the wrong way round: a worker named `a` or `fix` is contained in
almost anything anyone would say, so "please stop what you are doing" resolved
to whichever worker happened to be first, and stopped it. Only the fragment
direction survives, and ties now go to the **most recent** worker, because "stop
the browser one" means the one just started rather than its namesake from ten
minutes ago.

## The agent must not reshape the user's browser window

`capture()` called `chrome.windows.update(id, {state: "normal"})` before every
screenshot. `state: "normal"` is not "make this visible" — it is Chrome's
**restore**: it un-maximizes a maximized window and drops a fullscreen one back
to a floating rectangle. So the agent resized the browser the user lives in, on
the way past, with no undo — once the state has been changed the previous one is
not readable from anywhere.

`focused: true` alone is what was wanted. It raises a covered window, and for a
minimized one Chrome restores whatever it was before — precisely the value that
would otherwise have to be guessed at. `tests/test_extension_window.py` asserts
that **no** `chrome.windows.update` call in the extension carries a `state`,
not just the one in `capture()`: every one of them is reachable from an ordinary
agent action.

## Steering is a correction, and it must not read as "stop"

Reported: steering a running Browser Agent made it stop and write its report.

**The transport is not what does that**, and `tests/test_browser_steer.py` pins
that so the next person suspecting the plumbing can stop looking there.
`steer_subagent` queues the message, `_inject_steer_messages` appends it after
the tool results, and the loop carries straight on to the next model call.
`wrap_up_requested` is the only thing on that path that ends a turn early, and
steering never touches it.

What did it is the FRAMING. Every clause of the old `STEER_NUDGE_TEMPLATE` was
a prohibition — *"NOT a new task", "do not restart", "do not treat this as",
"do not expand scope"* — and a wall of don'ts arriving mid-task, with nothing
telling the model to keep **acting**, reads as "something is wrong, stop". For
the Browser Agent that is especially sharp: its one instruction for finishing
is *"reply with NO tool calls"*, so stopping and reporting are the same move.

Two things about the replacement:

- **It leads with what to do** and says outright that a steer is not a reason
  to finish, keeping one short scope caution instead of four prohibitions. The
  caution still has to be there — an unframed message mid-turn reads as a
  brand-new top-level instruction with equal weight to the original task, which
  is why the framing was added in the first place.
- **The user's words come first and the instruction last.** Same lesson as *a
  request ends on the turn, not on a note about it*: the last thing read is the
  thing answered. Ending on "keep going" is the whole point of the reordering;
  ending on the user's text invites a reply to the text instead of a
  continuation of the work.

Moving the framing to the back broke something a long way from `prompts.py`,
and the SUITE caught it rather than inspection: `sessions.to_display` unwraps a
steer message to render it as the "You steered" note, and it stripped only the
FRONT of the template. Every steer note in a replayed chat would have carried
the whole instruction block hanging off the end. Both halves now come from one
`partition("{text}")`, so the unwrapping cannot drift from the wrapping again
whichever side the framing sits on. The prefix was already derived rather than
written out twice — the suffix is what the reordering caught out.

## Naming what is missing is not the same as offering it

Reported: *"when the browser agent reads a long page, it gets 'truncated, x
chars total' at the end. Can the agent read the rest somehow? And can it
scroll?"*

No, and yes — and they are answers to **different questions**, which is the
part worth writing down.

`browser_read` stopped at 6000 characters and said `[truncated, N chars
total]`. It named exactly how much was missing while the tool took **no
arguments at all**, so there was no way to reach it even in principle. That
produces something worse than a missing answer: the model reads the first
part, does not find the thing, and reports that the page does not contain it.
`read_file` had the identical defect and was fixed the same way — cut on a line
boundary, say how much is left, and name the offset to continue from.

**Scrolling is not the fix for that, and the prompt now says so in both
directions.** `inner_text("body")` returns the WHOLE document's text whatever
is scrolled into view, so paging is what gets you the rest. A model that
reaches for PageDown to read more is spending turns on nothing.

**Scrolling does matter, for the other thing**, and it already worked: a page
that loads more of itself as you go. It is `browser_key` with PageDown /
PageUp / End / Home, and both backends implement it deliberately — the
extension has to, because a synthetic key event scrolls nothing on its own and
`page.js` calls `scrollBy` for exactly those four. The old description listed
PageDown among *"a key like Enter, Escape, PageDown, Tab"*, which never says
that this IS how you scroll. A capability the model does not know it has is not
a capability.

The extension route needed no change: `ExtensionPage.inner_text` already
fetches up to 200k characters and the paging happens on the Python side, so one
implementation serves both.

## A small model driving a page is a bad model, not a broken feature

"The browser agent is completely incapable — it just clicks and screenshots
random stuff" is the expected outcome of a small model driving a page. The
Browser Agent's own prompt says driving one is the hardest thing a small model
does here, and `_browser_client_and_model` silently inherits the chat's model
when no dedicated browser model is configured — which on a free tier is a small
flash model.

The app said nothing at all. So there was no way to tell a weak model from a
broken feature, and the setting that fixes it (Settings → Browser → Browser
model) is one nobody had a reason to go looking for.

`_say_browser_model` names the model that is about to drive, **once per chat**
— news the first time, noise after that, the same rule the rate-limit fallback
notice follows. A configured model is named without the advice: there is
nothing to fix, so nagging would be wrong.

Worth knowing when this comes up again: the mechanical causes were checked and
are not it. `browser_click` returns a fresh snapshot, an empty snapshot says so
explicitly rather than coming back blank, the element cap is 200, and the step
limit is 200. If the snapshots ARE arriving full and it still flails, that is
the model. If they come back empty, that is a different bug.

## Voice is a dock in the corner, not a screen over the app

Reported as three complaints, which turned out to be one shape seen from three
sides:

> *"there's no reason for that box to cover the whole screen"* — *"there's no
> way to look manually on subagents that started in voice mode"* — *"the chat
> is displayed in the normal chat, and also in the voice pop up"*

The panel was `position: fixed; inset: 0` with a scrim and `aria-modal`, so
starting a voice conversation took the app away. The thing it covered most
expensively was the **sub-agent inspector** — the one panel that could show you
the worker it had just dispatched for you, which is the whole reason you spoke
to it. And it carried a scrolling transcript of a conversation that already
lives in the chat, which is most of why it had to be screen-sized at all.

- **`pointer-events` are on the CARD, not the dock.** The dock spans a column
  of the window so the card can sit in the corner; if the column took clicks,
  the corner of the app would silently stop responding.
- **Below `--z-sheet`, above the chat.** Settings has to open over it — the
  dock's own Settings button depends on that.
- **The worker pills are `<button>`s that open the normal inspector.** A
  worker's id IS its sub-agent id, so there is nothing to build: the pill opens
  the same panel everything else uses. They were `<div>`s, so nothing said they
  could be clicked and no keyboard could reach them.
- **The dock steps aside when the inspector opens.** They occupy the same
  corner, and clicking a pill is what opens the inspector — so they would
  collide on the one interaction that matters most.

**It is four controls and no more**, and it lives at the top of the left rail,
above the work it dispatched — the orb, mute, the listening mode, and
push-to-talk when that is the mode. There is no collapsed state because this
IS the small state, and no fixed position of its own because the rail places
it.

**One button, three modes** (`hands-free` / `push-to-talk` / `wake word`). It
was two independent toggles which between them could express the same listening
mode two different ways, and neither of them named the third. `voice.ptt` and
`voice.gated` are what the audio path actually reads, and `cycleVoiceMode` sets
**both** every time — setting only "the one that changed" is how the old pair
could land in a state neither toggle displayed.

**The permission card stays, and it is the only thing kept beyond that list.**
A gated action has to be approvable and "just say yes" is not always heard.
Hidden at rest, so the dock's resting size is unaffected.

**The transcript is gone from the dock, and the CHAT shows the turn live
instead.** Deleting it was right; what came with it was not, and was reported:
*"before, it was much clearer when the agent actually heard me and thought of
an answer — now I just kinda wait there and then my prompt and the agent's
answer pops up in the chat after a while."*

The exchange was rendered from `voice_chat_turn`, which Python emits at the
END, so listening, thinking and answering were all silence. It now goes into
the chat as it happens — `spokenHeard` as the words are transcribed,
`spokenReplyDelta` as the reply generates — which is what the phone has always
done and what typing already looks like. `voice_chat_turn` then **reconciles**
with what is on screen rather than reprinting it, matched on the heard text
alone, since the streamed reply and the recorded one can differ in whitespace.

- **The reply is accumulated in `spokenTurn`, not read back off the engine.**
  `voice._replyBuf` is per model round-trip and `stream_start` clears it, so a
  turn that looks something up part-way through gets several — rendering from
  it put the second round's text on screen *instead of* the first round's.
  `liveVoice.said` has the opposite shape (per turn), so the one thing both
  engines agree on is the delta.
- **The heard text is updated through its text NODE.** `addUserMessage` puts
  the words in one and appends the "Spoken" note beside them, so assigning
  `textContent` on the bubble takes the note with it.
- **The references are per turn.** Held across one, the second exchange
  overwrites the first instead of following it.

**Historic, kept because it explains the shape**: the transcript was trimmed
rather than deleted first, on the reasoning that a spoken turn only reaches the
chat once the coding agent's turn lock is free. That timing is real for the
model's HISTORY and was never true of the screen — `_record_voice_exchange`
emits `voice_chat_turn` either way.

## The state has to be on screen, and being in voice mode has to be obvious

Two halves of one report: *"it's kinda hard to tell I'm in voice mode, that
should be clearer and more beautiful"*.

**The status text is beside the orb, not in a tooltip.** It was briefly a
tooltip, on the reasoning that the dock is four controls and a status line is a
fifth thing. That was wrong: the orb is ONE animated dot for listening,
thinking and speaking, so the word is what tells them apart — it is not a fifth
control, it is the orb saying what it means. A tooltip is not on screen.

**A live microphone is a state the whole window carries.** `body.voice-on`
draws a soft green ring inside the viewport. The dock is deliberately small and
lives in a margin column, and the Talk button turning green is easy to miss
with your eyes on the chat; a ring frames everything without moving, covering
or recolouring any of it. `pointer-events: none`, because it spans the viewport
and must never take a click, and above the sheets so a modal does not cut a
hole in it. Under `prefers-reduced-motion` it stops breathing but stays — it is
the thing that says the microphone is live.

## Workers belong to a chat

Reported as *"workers seem to be cross-session"*, and they were. `liveWorkers`
was a bare object that nothing ever cleared, so switching chats left the
previous one's workers in the rail — and worker ids are per chat (`wk1`,
`wk2`), so the new chat's first worker overwrote the old chat's entry under the
same key and its pill opened a thread belonging to another conversation.

**Rebuilt from the session payload rather than merely cleared.** A worker still
running in the chat you come back to has to be there, and its events were
missed while you were away: a background chat's events go to
`handleBackgroundEvent` and never reach this store. `_worker_rows` reads the
coding agent's registry, which since `adopt_workers_of` is the whole chat's set
however it was dispatched.


## Actions go where the hands are; state goes in the periphery

Two asks — *"it should be easier to activate and deactivate"* / *"the little
mic icon at the top isn't cutting it"*, and *"we have quite a lot of space on
both sides of the chat"* — and they split along that line.

**Talk is at the composer.** Starting a conversation is an action, and every
other message action lives there; it was a 20px glyph in the titlebar, the
width of the window away from the hands. The same button ends the session, so
there is one control for one session. The titlebar chip stays (it is where the
wake-word "armed" state has always shown) and the two are kept in step by
`setTalkState`.

It is also **labelled**, because "mic" is the same picture for *type this for
me* and *let's talk* — and those were literally the same glyph, 500px apart:
`#mic-btn` dictates into the box, `#voice-chip` opened a conversation.
Dictation's icon carries a pen now.

**The margin shows what is running.** Not more buttons — an ambient view of
work in flight, which did not exist: a dispatched worker was invisible unless
you opened the sub-agent panel or had the voice dock up. Every item is
reachable from the sub-agent panel too, because the rail is the first thing to
go when the window narrows and **nothing may live only in it**.

Three things this turned up, none of them visible by looking:

- **`worker_update` was handled only on the voice route.** The coding agent has
  the same worker tools and its events arrive on the ordinary sid, where
  nothing was listening — so a worker dispatched by TYPING rendered nowhere in
  the app at all. One store (`liveWorkers`), written by both routes, read by
  the dock and the rail.
- **`#chat` is fixed across the FULL width** — its margin is padding, not a gap
  — so at the same z-index it covered the rail and swallowed every click. The
  rail looked perfectly fine and did nothing.
- **The voice dock was covering the composer.** At `bottom: 18px` the card sat
  on top of Send. `#chat` reserves 128px for the composer and the dock now
  clears it.

**The sidebar starts OPEN**, and the first version of the rail hid itself
whenever it was — so the rail was invisible in the app's normal state. The chat
shifts right rather than shrinking, so the margin is still there and the rail
moves with it; the ITEMS only disappear when the window genuinely has no room
(1100px, or 1368px with the sidebar).

**The voice controls are exempt from that, and the exemption is not a
refinement.** "Nothing may live only in the rail" was true of every item and
stopped being true the moment the dock moved in: mute, the mode and
push-to-talk have no second home now that the full-screen overlay is gone. And
the thresholds are 1100px, or 1368px *with the sidebar open* — the default
window is 1280 with the sidebar open, inside both. So this was never a
narrow-window edge case; it was every window, and the symptom was a live
microphone with no way to stop it. `body.voice-on` spares a session, and where
the margin has collapsed the rail stops being a margin column and becomes a
small dock at the bottom-left above the composer, carrying the four controls
alone. The items still go, because the reason they may go is unchanged.

`body.no-session` disables the whole composer, Talk included. That is correct
and not a special case: a spoken turn has nothing to attach to without a chat.

## `$()` returns a stub, so code outlives its element in silence

A missing id must never throw: that would abort the script and kill the
`pywebviewready` listener at the bottom of it, freezing the app on launch with
nothing on screen. So `$()` hands back an inert Proxy. The price is that
anything still addressing a deleted element goes on *appearing* to work, and
replacing the voice overlay with the dock deleted four of them at once.

- **The waveform threw sixty times a second.** `canvas.getContext("2d")` on the
  stub returns `undefined`, and the `requestAnimationFrame` loop raised on
  `clearRect` for the length of a session — two hundred errors inside one
  half-second test. The guard above it (`if (!canvas ...) return`) could not
  help: the stub is truthy, which is the whole point of it.
- **Dead code is deleted, not left as a no-op.** `addVoiceTurn`,
  `voiceReplyEl`, `liveCaptionUser` and `liveEndCaptionTurn` were all painting
  into a `#voice-caption` that no longer exists.
- **But the WORDS may be worth more than the element.** Twenty-six
  `setVoiceStatus` calls were writing into the same stub, and "Listening",
  "Thinking" and "Muted" are the only place the app says which is true — the
  orb is one animated dot for all three. The text moved onto the orb as its
  tooltip and `aria-label` rather than going with the element, and the orb
  stopped being `aria-hidden` because it now carries it.

Worth running when markup is deleted: every `$("…")` id in `app.js` against
every `id=` in `index.html`. The difference is exactly this class of bug, and
nothing else reports it.

## Retargeting a test must not aim it at something the harness cannot reach

The live-engine transcript tests read `#voice-caption`, which the dock removed,
and pointing them at `#chat` instead was wrong in a way that looks right: a
spoken turn reaches the chat from the **Python** side (`_persist_voice_turn`
emits `voice_chat_turn`), so in a harness whose backend is a Proxy stub the
chat is never written and the assertion could only ever be empty.

They assert on the hand-over instead — `liveVoice.heard` / `.said` passed to
`live_voice_turn` on `turnComplete`, which is the entire input to the record —
and that pins the same three properties the caption did: both halves kept, one
turn not carrying the last one's text, and streamed fragments joined into one
record rather than one each.

The same care applies to driving the UI: `test_push_to_talk.py` clicks the mode
button **until it reads the mode wanted**, because one button cycling three
ways means "click once for push-to-talk" is only true from hands-free. A fixed
click count asserts against whichever mode it happens to land in.

## Photograph the UI; do not read it

Every layout bug in this app so far was found by a person using it and
reporting it, which is the expensive way. The desktop harness can boot the
real page with the real CSS and take a picture, and a picture settles in one
look what an hour of reading the stylesheet does not.

Two things shipped and were caught this way, both in the app's **default
window** — 1280 wide with the sidebar open:

- **The voice dock truncated its own labels** to `List…` and `Han…`. The rail
  column is `(100vw - sidebar) / 2 - 430`, which is **76px** there. Four
  controls never fit on one row. The status word moved onto its own line under
  them.
- **The activity rail was invisible.** Its collapse thresholds (1100px / 1368px
  with the sidebar) sat either side of the default window, so the ambient view
  of running work — the whole feature — was hidden for almost everyone almost
  always. It docks at the bottom-left now instead of disappearing, carrying the
  rows as well as the controls. Only below 900px is there genuinely no room,
  and there the rows go (they are reachable from the sub-agent panel) while the
  voice controls stay (they are not).

**The thresholds are the arithmetic solved for a number, not numbers picked by
eye.** A column is worth having at 200px, which needs 1260px without the
sidebar and 1528px with it. The old pair were guesses, and guesses are how they
came to bracket the one window size that matters most.

**Sized to content is not free.** The docked stack was `width: max-content`,
so it grew and shrank as the status line changed — the dock jittered under the
pointer while you read it. Fixed width.

Two more found in the same pass, both measured rather than eyeballed:

- **The composer placeholder needed 272px and had 252px** once the Talk button
  widened for a live session, so it wrapped and the textarea clipped the second
  line mid-word. The hint is shorter and the Talk label no longer grows —
  it says **End**, which is what pressing it does, and the state is said in
  three better places (the dock's line, the orb, the ring).
  `text-overflow` is NOT set on `::placeholder`: Chromium computes it to `clip`
  whatever you ask for, so declaring one would be decoration.
- **The model chip was a blank pill** with no chat open — `boot()` never called
  `populateModelPicker`, and `renderModelChip` put "Model: undefined (via
  undefined)" in the tooltip because the fallback was applied only to the
  label.

A second pass, over the surfaces the first one skipped, found the worst of
them: **with the sub-agent inspector open the composer's textarea is 22px
wide.** `body.subagent-open` gives the panel the right half, which leaves the
composer ~320px at the default window, and six controls eat all of it. Not a
clipped hint -- a box you cannot type in, reached by clicking a sub-agent to
watch it work. Talk is what gets dropped to buy it back: it is the only control
there with a second home (the titlebar chip opens the same session, and
`setTalkState` keeps the two in step). Dictation stays despite costing room,
because it has nowhere else to be -- "nothing may live only here" cuts both
ways.

`scripts/` has no screenshot tool and does not need one: the harness in
`tests/desktop_ui/conftest.py` is thirty lines away from a script that boots
the page and photographs it. Do that before believing a layout is fine.

**Check the mock against the real contract before believing a defect.** That
same pass appeared to find "undefined" filling the whole first-run key sheet.
It was the fake payload using `name`/`base_url` where the app reads
`key`/`label`/`blurb` -- a bug in the script, not the app. A photograph is
evidence of what the page did with what it was given, which is only evidence
about the app when the input was shaped like the real one.

## The app is named for mistakes and kept no record of its own

Every failed tool call was answered into the model's context and then thrown
away. An `edit_file` whose `old_string` never existed, a stale-sha 422, a
sub-agent that came back FAILED — each fell out of the window at the next
compaction, so the same model made the same mistake on the same repo on
Tuesday that it made on Monday. `remember` only ever held what somebody
thought to assert.

`glmcode/ledger.py` is `remember`, earned instead of asserted. Failures
already funnel through one place (`Agent._tool_reply`), so observing them
costs nothing; what turns a pile of errors into something usable is the
**signature** — the error text with paths, numbers and quoted strings replaced
by placeholders, so "old_string not found in `foo.py`" and the same about
`bar.py` are one pattern with a count of two rather than two patterns with a
count of one. A pattern with a count is a rule. A log of errors is not.

- **Per model AND per endpoint.** The fallback chain switches models mid-task
  and the same model name on another provider is a different quota and a
  different failure profile, so a ledger keyed on the name alone hands one
  provider's lessons to another — in the longest prompt in the app.
- **A success decays a pattern and never deletes it.** Confidence is
  failures/(failures+successes), so a tool that works again stops lecturing
  the model while the record of what happened stays readable. Deleting on
  success is the sync-index failure again: an answer that cannot be recovered
  written over one that could. The only thing here that deletes is the
  per-bucket size cap, which is a memory bound, and the user's own `forget`.
- **The block is capped hard** — six rules, ~1,200 characters. It competes
  with the ~12,400 tokens of system prompt and tool schemas re-sent every
  request, and a prefix that grows every session is a prompt cache that never
  hits.
- **Whether the model had already been WARNED is recorded**, which is the only
  way to find out whether these rules work at all. A pattern that keeps firing
  with its own rule sitting in the prompt is not a lesson the model failed to
  learn, it is a rule that does not work, and it says so in its own text
  instead of being repeated more loudly forever.
- **A permission denial is not a mistake the model made.** It reaches
  `_tool_reply` as an error and is not one — the user said no. Recording it
  would teach the model the tool is broken and would put the user's own
  decisions into a block titled "mistakes you have made", so that one path
  passes `learn=False`.
- **A repeat failure carries its count to the model at the moment it reads
  it.** A rule in the system prompt is read once per request; the error is
  read exactly when it applies. The ledger does not know the fix and says so
  rather than inventing one — what it knows first-hand is that retrying has
  not worked any of the previous times.
- **A new rule rebuilds the system prompt in the conversation that earned
  it**, the same reason the `remember` tool does, and only when the active set
  actually changed — rebuilding runs git subprocesses and most failures change
  nothing.

Two things were found by writing the tests rather than by reading the code,
and both are the usual shape:

- **`_POSIXPATH` matched the `/a.py` inside `src/a.py`** and left `src` in the
  signature, so the same mistake about two files stayed two patterns of one
  hit each — the exact counting the module exists to do, not done. A
  lookbehind fixed it and a relative-path pattern was missing entirely.
- **"Never raises" did not include the guard that turns the feature off.**
  `self.cfg` was read outside the `try`, and an Agent built with `__new__` and
  no `__init__` — which is how several tests drive `_handle_tool_calls` — has
  no `cfg`. So the one line whose whole job was to disable this brought the
  turn down instead. A check that only holds because of where the caller got
  its input is not a check; that sentence is elsewhere in this file and was
  not true of this.

**Not done, and deliberately: there is no hard preflight refusal.** The
proposal was that a high-confidence pattern should make the tool refuse
locally before the call runs. It cannot save a request — the model has already
spent one to make the call — so all it buys is a different error, and a false
positive there blocks real work with no way round it. The count is attached to
the real error instead, which is the same information without the veto.

## The instrument was built and wired to nothing

`glmcode/evals.py` has had a case runner, an A/B and a `compare()` table for a
long time. Nothing in the app ever imported it. So the module that exists to
test the README's central claim — that scaffolding is the biggest quality
lever available to a small free model — printed a number to a terminal, the
number scrolled away, and every scaffold setting stayed wherever it happened
to be. Ninety-odd test files prove each feature is WIRED UP; none of them, and
nothing else, measured whether any of it helps.

Two things were missing, and the second is what makes the first honest.

**A run had no price.** The free tiers here are metered in requests per day —
twenty for Gemini 3.6 Flash — and a grid of four configurations over three
cases is a day's allowance, discovered as a 429 somewhere in the middle.
`estimate_requests` says what a run is about to cost BEFORE it starts, and
`Result.requests` measures what each case really cost, read from `usage.py`
rather than counted separately (a second counter beside the one the rate-limit
panel reads is a number that can disagree with it). After the first suite
anybody runs, the estimate stops being a guess: real per-case medians replace
`REQUESTS_PER_CASE_GUESS`.

- **`Budget` is enforced BETWEEN cases, and says so.** A case is a whole
  agentic turn and there is no way to stop one half way that does not also
  throw away the requests already spent on it. So the cap is "do not START
  another case once this much has gone" rather than a ceiling it cannot
  actually be.
- **A case that was not run is `invalid`, never `fail`.** The agent did not
  get it wrong; we stopped asking. Scoring it as a failure would report the
  model as worse than it is, on the run that cost the most to produce — the
  same distinction the suite already draws for a case whose check passed
  before the agent touched it.
- **`Journal` makes a run resumable**, written after every case rather than at
  the end, because the failure it exists for is the run not reaching the end.
  A half-written last line is the NORMAL way that file ends — it is appended
  to during a run that was killed — so an unparseable tail keeps whatever
  parsed and costs one re-run.

**A result changed nothing.** `glmcode/evalprofile.py` stores the winner per
model + endpoint (the same key the mistake ledger uses, for the same reason),
with what it beat and by how much — a winner with no baseline is not a result.

- **Only scaffold knobs can be in a profile** (`PROFILE_FIELDS`). A
  measurement must not be able to change what it was not measuring; an eval
  run that could rewrite `model`, a base URL or a key would be a
  config-editing machine wearing a lab coat. Anything else is dropped, and the
  CLI says which fields it dropped rather than doing it quietly.
- **Types come from the config, not from the stored value.** Same rule as
  `_apply_overrides`: a profile setting a string where an int lives is a
  setting nobody reads, which surfaces later as "the flag made no difference"
  and is believed.
- **A tie goes to the baseline.** A configuration that merely matched the
  defaults has not earned the right to change anybody's settings, and
  preferring the fancier one on a tie is how scaffolding accumulates without
  evidence.
- **The defaults winning is RECORDED**, with empty settings, so it stays
  distinguishable from "nobody has measured this" and the question is not
  reopened every time somebody looks.
- **Nothing is shipped pre-measured, and must not be.** A table of "good
  settings for Flash Lite" that nobody ran is exactly the guesswork this
  replaces, and it would go stale the first time a provider changed a model.

**Applying it is an explicit action, not something a chat does to itself.**
`Api._cfg` is ONE Config shared by every chat and persisted by `save_config`,
so an agent that applied a profile on its own initiative would change other
chats and outlive the session that did it. `CheckupApi.scaffold_profile()`
says what was measured and whether the current settings already match it;
`apply_scaffold_profile()` is the button. Two methods on purpose: one reports,
one changes something.

Not done, and deliberately: **cases are still hand-written.** Harvesting a
failed chat into a fixture is the part that would make the suite grow from
real bugs, and it is blocked on something real rather than on effort — a
fixture is a tiny self-contained repo, and a real project is neither small nor
free of secrets. Copying "just the files the turn touched" solves neither.

## Tests

The mobile keyboard/composer geometry is covered by
`tests/mobile_ui/test_composer_keyboard.py` (Playwright). Run it for any change
to `mobile/app.js` keyboard handling or the `--kb` rules in `mobile/style.css`:

```
python -m pytest tests/mobile_ui/test_composer_keyboard.py -q
```

It runs a desktop Chromium, so it verifies the arithmetic, not iOS. It cannot
see anything specific to an installed iOS PWA — the system form accessory bar,
or the web view being scrolled by the OS. Those need the device, and the
diagnostics panel in Settings is there to report them.

## iOS keyboard: what has already been settled on-device

Do not re-litigate these. Each cost a round trip to a real phone.

- `--kb` has two parts and they are not the same kind of thing. What
  `visualViewport` reports as hidden is measurable: on an iPhone 15 Pro, an
  852px screen, a 59px top inset and a 449px visible viewport give 344px hidden.
  The accessory bar is **not** in that number. It is a native view painted over
  the web view and does not shrink `visualViewport` at all, so room for it has to
  be added on top. Reading 344 as "the keys and the bar together" is what left
  the composer sitting behind the bar.
- Do not check that with `dock bottom == visual h`. That was used to confirm the
  reading above and it confirms the broken state: flush with the bottom of the
  visible area *is* underneath a bar painted across it. The only check that means
  anything is looking at the phone with the keyboard up.
- The bar's height is unmeasurable from the page, so it lives in `localStorage`
  as `mnm.kb.bar` and Settings → "Adjust composer height" puts − and + on the
  composer while the keyboard is up. Change the number there, on the device,
  rather than shipping a fourth guess.
- `window.innerHeight` tracks the *visual* viewport in an installed iOS PWA, so
  it shrinks with the keyboard. Measure against
  `document.documentElement.clientHeight`, or the lift comes out ~0.
- The focus flash — the whole UI translating and returning when a field is
  focused — is iOS scrolling the web view's own enclosing scroll view. It reports
  nothing to any web API (document scroll, visual viewport offset, and `<html>`
  height were all measured at zero while it was plainly visible), and nothing in
  the page can observe or undo it. See
  [ionic-team/capacitor#1366](https://github.com/ionic-team/capacitor/issues/1366),
  closed as not planned. The only fix is native — Capacitor's
  `Keyboard.setScroll({ isDisabled: true })` — and a native build has been ruled
  out for this project.
- Predicting the lift at `focusin` from a remembered keyboard height does not
  work and was reverted. Two reasons, both worth knowing before trying it again:
  a height remembered from every viewport report ends up holding a frame of the
  keyboard *sliding away*, not its settled size; and an already-open keyboard
  never resizes, so `visualViewport` never fires to correct the prediction and
  the composer stays under the accessory bar until blur.

## Skoolie is a provider through a loopback bridge, and it cannot use tools

Skoolie has no API, only a chat page. `glmcode/skoolie.py` drives that page
with Playwright (persistent profile `~/.skoolie-profile`, signed in once by hand
with `python -m glmcode.skoolie --login`) and serves the OpenAI
`/chat/completions` shape on `127.0.0.1:11437`. The `skoolie` preset points at
that address, so no call site knows it is special; `ZaiClient.__init__` starts
the server the first time a client is built for it.

- **It has no tool calling.** The tool schemas are ignored and no `tool_calls`
  come back, so this agent can talk but not edit files or run commands. Say so
  rather than letting it look broken.
- **Failures are 400/401, never 5xx.** The client retries 5xx, and a retry would
  type the same question into the chat a second time.
- **Only the new turns are typed** when a request is the previous history plus
  more; anything else starts a fresh conversation with the whole history. The
  system prompt is never forwarded.
- **One browser, one thread.** The server is deliberately serial; Playwright's
  sync API belongs to the thread that started it.

- **No model-written chat title through Skoolie.** `_generate_title` runs after
  `run_turn` and before the turn is marked done, so for a browser-driven
  provider the turn hung for a whole extra round trip — and the call opened a
  new conversation under the live one. It is skipped; the derived title stays.
- **The bridge binds exclusively on Windows** (`SO_EXCLUSIVEADDRUSE`), for the
  same reason as the extension bridge: `SO_REUSEADDR` there allows a second
  process onto a port in active use.
- **Skoolie's canned decline is never shown.** It is an off-topic filter for a
  study-and-careers bot, and a coding harness trips it for the wrong reason. A
  short framing line is added to every prompt, and a decline is retried twice
  with the task reframed (`RETRY_PREAMBLES`); after that it is an error, not
  the canned line. Detection runs on the *repaired* text (`fix_mojibake`), since
  the decline has been seen arriving as `tyvÃ¤rr`. The repair is per line and
  leaves anything that does not round-trip untouched. This is a nudge, not a
  guarantee — the filter is the site's.

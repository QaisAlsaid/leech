# CHANNELS.md — stable / canary at runtime, and the branch model behind it

answers first, details after:

- **is `dev` my canary?** no — `dev` is one layer *before* canary. keep it
  as the integration branch where feature branches land and things may be
  half-done. `canary` is its own branch, cut from `dev` when a build is
  actually worth dogfooding on the live bot. three tiers:

  ```
  feature branches ──merge──▶ dev ──cut when worth it──▶ canary ──soak──▶ master
  (may be junk)              (may be            (boots, dogfood      (stable,
                              half-done)         this on the bot)     tagged)
  ```

  solo-dev shortcut: if juggling 3 branches feels heavy, drop `dev` and
  merge features straight into `canary`. the bot only ever sees two
  channels — `stable` and `canary` — so `dev` is for YOUR convenience,
  not the bot's.

- **semver:** tag every merge to `master` — `v0.3.2`, `v0.3.3`... patch =
  fixes, minor = new features, and pre-1.0 a minor may carry breaking
  changes (that's what 0.x means). `>channel status` shows the tag via
  `git describe`, so "what's running" is always one command away.

---

## the moving parts

```
bot-hosting panel
   └─ start command:  python deploy/supervisor.py      ← change this once
        ├─ deploy/supervisor.py   (stdlib only, never breaks)
        │     git fetch/checkout/reset · spawns the app · boot probe
        │     · auto-revert to last_good · autopull loop
        ├─ deploy/control.py      (stdlib http sidecar, port 8765)
        │     GET / · POST /switch /pull /revert /autopoll
        └─ python -m leech        (the actual app, restarted by supervisor)
              └─ cogs/channel/cog.py   (>channel ... — talks to the sidecar)
```

why a supervisor at all: the panel restart is the only recovery you have
today, and it needs a human. with this layer, a bad pull is a 75-second
hiccup: the probe fails → auto-revert → the bot comes back on last_good
and pings a webhook. and because the sidecar lives in the supervisor (not
the app), `curl -X POST http://127.0.0.1:8765/revert` works even when
discord commands don't.

the two doors in:

1. **discord** — `>channel canary` / `>channel stable` / `>channel pull`
   / `>channel autopoll off` / `>channel revert` / `>channel status`.
   owner-gated by `LEECH_OWNER_IDS` (fail closed when unset).
2. **curl** — for when the app is dead:

   ```bash
   curl -s http://127.0.0.1:8765/ | python3 -m json.tool
   curl -s -X POST http://127.0.0.1:8765/revert \
        -H 'x-leech-token: <SUPERVISOR_TOKEN>' -d '{}'
   curl -s -X POST http://127.0.0.1:8765/switch \
        -H 'x-leech-token: <SUPERVISOR_TOKEN>' -d '{"channel": "stable"}'
   ```

   if the panel exposes extra ports you can also hit it through
   `https://jlvn19o9dp.apps.bot-hosting.cloud:8765` — but don't expose it
   without the token set. localhost + the cog covers the normal cases.

## deploy (once)

1. commit `deploy/supervisor.py`, `deploy/control.py` and
   `cogs/channel/cog.py` to **both** `master` and `canary` (and `dev`).
   gitignore `persistent/` if it isn't already — the channel state lives
   at `persistent/v1/channel.json` and must survive checkouts.
2. panel env:

   ```
   SUPERVISOR_TOKEN=<random string>        # mutations need it
   LEECH_OWNER_IDS=<your discord id>       # unlocks >channel
   LEECH_SUPERVISOR_TOKEN=<same string>    # the cog's copy of it
   # optional:
   # LEECH_START_CMD=python -m leech
   # LEECH_CHANNEL_BRANCHES=stable:master,canary:canary
   # AUTOPULL_SECS=300  SUPERVISOR_PROBE_SECS=75
   # DISCORD_WEBHOOK_URL=<a webhook>       # revert alerts
   ```

3. change the panel's start command from `python -m leech` (or whatever
   it is) to `python deploy/supervisor.py` and restart once.
4. smoke: `curl -s http://127.0.0.1:8765/` from the panel shell →
   json with `"channel": "stable"`. then in discord: `>channel status`,
   `>channel canary` (watch it switch + probe), `>channel revert`.

## how a switch actually goes

`>channel canary` → sidecar → supervisor: `git fetch`, checkout
`origin/canary`, `reset --hard`, kill the app, spawn it fresh with
`LEECH_GIT_*` env, watch for 75s. survives → that commit is the new
`last_good`. dies → commit lands in `bad_commits`, back to `last_good`,
webhook pings, cog tells you which commit to blame. autopull skips
known-bad heads (a later good commit un-sticks it), and everything is
recorded in `persistent/v1/channel.json`.

branch protection tip: make `master` accept merges only from `canary`
(github branch rules, no reviews needed for a solo repo — just the
restriction). that one rule is what makes "stable" mean something.

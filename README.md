# Seeress — your personal browser agent

Local brains. Your browser. No credits, no cloud, no telemetry.

**Status: a working prototype with foundations under it. Not production grade.** The
agent loop, flows and the activity logger work. Under them: a lock file, 522 unit and
contract tests, 7 tests on a real browser, strict types, lint, three import contracts and
a CI workflow. They pass on the machine this was built on (macOS, arm64) and in CI on
GitHub's macOS runner; the first green run is
[45566464574](https://github.com/AgenticTony/Seeress/actions/runs/45566464574).

**Not verified.** The personal door's acceptance was last proven on a throwaway Chromium
that stands where your Chrome does, not on your Chrome.

**Verified end-to-end 2026-11-20**: nemotron (Ollama) drove a real browser task
start-to-finish through the client (local-only) profile.

## What it is

A code-writing agent in your own browser:

    brain (Ollama nemotron today; optional GLM cloud brain; a 125B slotstream
           tier exists as a model but is not wired into Seeress)
      → writes Python (code-writing agent pattern)
        → browser-use daemon executes it in a real browser via CDP
          → result feeds back → loop until DONE/BLOCKED

Built on open-source parts: browser-use for the browser, Ollama for the model,
`websockets` for the DevTools connection, and Python's standard library for the rest.

## Two doors, never a switch

| Door | Model | Browser |
|---|---|---|
| `bin/seeress-personal "task"` | GLM (cloud) if `ZAI_API_KEY` is set, else Ollama on this machine | your everyday Chrome, through its debugging port |
| `bin/seeress-client "task"` | Ollama on this machine; no cloud endpoint is listed for it | a Chromium that Seeress starts and owns |

Both scripts run one command line, `python -m seeress.cli --profile personal|client`.
The profile decides three things, and each is closed on its own:

- **Which model may answer.** Before each model request Seeress reads the hostname of the
  open page. On a domain in `client_domains`, or when the browser cannot be asked, only a
  model on this machine may answer. If none can, Seeress stops before anything is sent.
- **Which browser is driven.** A door drives only the browser it can prove is its own. The
  client door's browser carries a marker that only it has; a browser without the right one
  is refused before any model request. The personal door refuses any browser that carries
  that marker.
- **Where the browser can go.** The browser of the client door has no way out except this
  machine and the client's own sites. Left to itself, a fresh browser talks to its vendor
  at once: 26 connections to 14 Google hosts in its first 12 seconds, measured here.

## What holds, and what proves it

| Control | Proof |
|---|---|
| Domain guard fails closed | `tests/unit/test_brain.py` and `test_agent.py`: a client domain and its subdomains remove every cloud endpoint; a page that cannot be read counts as a client page |
| Door isolation | 51 tests in `tests/unit/test_bodies.py`, and 4 on a real Chromium in `tests/e2e/test_bodies.py` |
| Allow-listed environments | `tests/unit/test_env.py`; the egress check, which starts Seeress with 17 planted settings and requires that none reaches a child |
| Model-written code | `seeress/browser_api.py`: the prompt is built from the library's real helper list, and a step may use those helpers and plain builtins only |
| Browser with no route out | `tests/e2e/test_route.py`, and the egress check, which asks for a page from an address that is not exempt and requires a refusal |
| Nothing leaves the machine | `tools/egress_check.py`, a step in CI |
| A failure is a failure | `tests/unit/test_cli.py`: every command exits 1 and prints the reason |

Nothing in the browser tool's own code was changed to get here. Its switches are set from
outside, and what Seeress relies on is pinned to its source by contract tests in
`tests/contract/`.

## Settings

Settings that differ per machine live in `~/.seeress/config.toml`. `SEERESS_HOME` moves
the folder.

```toml
[routing]
client_domains = ["portal.corp.example.com"]   # never sent to a cloud model

[[endpoints]]                                  # tried before the built-in ones
name = "tenant"
base_url = "https://models.tenant.example/v1"
model = "tenant-large"                         # optional
api_key_env = "TENANT_KEY"                     # optional
```

A file Seeress cannot understand stops it. No setting is ever skipped: an ignored one
could be the one that keeps client pages away from a cloud model. The same
`client_domains` list names the sites the client door's browser may reach.

## Usage

Talk to it (chat with history — this is the main door):

    ./bin/seeress-personal                     # chat in your real Chrome
    ./bin/seeress-client                      # chat, local-only, isolated browser
    ./bin/seeress-personal chat --companion companions/researcher.md

Workflows — the agent writes them, you replay them free:

    ./bin/seeress-personal create-flow check-hemnet "open hemnet.se, search 'Malmö villa', print the first 5 listing prices"
    ./bin/seeress-client run check-hemnet     # deterministic replay: no LLM, ~seconds, 0 cost
    ./bin/seeress-personal flows              # list saved flows

Automations — schedule any flow (writes a LaunchAgent; activate when ready):

    ./bin/seeress-personal schedule check-hemnet --daily 08:00
    ./bin/seeress-personal schedule check-hemnet --every 30

One-shot (still works as before):

    ./bin/seeress-client "Open the IANA example domains page and report the h1"
    ./bin/seeress-personal "check example.com"

Agent-written flows are verified to parse before saving. The flow-writing prompt
enforces bare `js()` expressions and load-verification with one retry (learned live:
example.com intermittently errors from the daemon browser; iana.org is the reliable
smoke-test target).

## Development

```bash
uv sync --frozen
uv run pytest                               # unit and contract tests: no browser, no network
uv run pytest -m e2e                        # a headless browser on a throwaway profile
uv run python tools/egress_check.py         # what Seeress contacts, through both doors
uv run ruff check && uv run ruff format --check
uv run mypy && uv run lint-imports
```

Three rules, each learned the hard way on 2026-11-24:

- **A unit test cannot start a process or open a socket.** `tests/conftest.py` blocks both
  unless the test is marked `e2e`, or `git` for a test that may start git and nothing
  else. Before that guard existed, a unit test patched the wrong function: the real
  browser tool ran, and the library's daemon attached to the user's Chrome.
- **Never run the browser tool without an explicit browser address.** Given none, the
  library looks for a Chrome on this machine and drives it. On 2026-11-24 that attached a
  daemon to the user's own Chrome; a minute later remote debugging was switched off in that
  Chrome, and the activity logger fell silent.
- **Never delete or rebuild `.venv` in place while the activity logger runs.** The logger
  starts from it. Build beside it, then swap.

### Publishing

The public repository holds the code. The planning and decision documents behind it are
kept out of it, so the complete record's own history is not for publication, and a plain
`git push` from it is refused by a hook. The tool below carries each new commit over
without the documents. A commit message is published as written.

```bash
uv run python tools/publish_public.py        # say what would be published
uv run python tools/publish_public.py --push
```

## Practical notes (learned building it)

- **First LLM call after idle ≈ 2–3 min** (19GB model cold-loads from
  ExternalSSD). Keep-alive keeps it warm ~5 min between steps.
- **Reboot before long sessions while swap is full** (`sysctl vm.swapusage` —
  macOS never shrinks swap; see the blackout runbook).
- **Real-Chrome attach (personal door), solved 2026-11-20:** tick "Allow remote
  debugging" once at `chrome://inspect/#remote-debugging`. Chrome 158 then hides
  auto-discovery and shows an approval sheet the browser-use `mac-approve` flow cannot
  pass, but it publishes its live WebSocket address in
  `~/Library/Application Support/Google/Chrome/DevToolsActivePort`. Seeress reads that
  file on every call and passes the address to the browser tool.
- **Helpers:** the prompt is generated from `seeress/browser_api.py`, which a contract
  test keeps equal to the library's own list and real signatures. The model is offered 17
  helpers plus `json`, `time` and `math`. Withheld from it, each with a reason: raw `cdp`,
  `capture_screenshot`, `upload_file`, `http_get`, the recording helpers, and the
  library's update and cloud-browser functions.
- **Telemetry is forced off in code**, together with both of the library's update checks,
  in the environment of every child process (`seeress/env.py`). It is never left to a
  command run by hand.
- After navigation, always `wait_for_load()` before reading (baked into the agent's
  system prompt).

## Known limits

- **Model-written code still runs with your privileges**, in a fresh process. It now gets
  an allow-listed environment and may use only the listed helpers and plain builtins.
  That is hardening, not a sandbox. Company mode will not run model-written code at all.
- **The browser tool looks for a local browser when it starts its daemon.** It asks
  `127.0.0.1:9222`, then `:9223`, for a browser's version before it reads the address Seeress
  gave it. If your Chrome has remote debugging on, it is asked. It is a read of a version
  string on this machine; the answer only decides whether a cloud browser would be offered,
  and Seeress never allows one. The egress check reports it under every door.
- **The client door's route is a default, not a decision.** A company that lists no sites
  has a browser with no route at all.
- **A browser body's own storage** (cookies, history) is protected by the OS account and
  disk encryption, not yet by a vault.
- **Attach mode is for personal and consultant use.** Chrome and Edge have an administrator
  policy that switches remote debugging off, and an open debugging port lets any local
  program drive a signed-in session. Company deployments need a browser driven from inside
  the app, with no port exposed.
- **The library is pinned at `browser-use` 1.0.6.** Version 1.0.8 is current on PyPI.
  Every control here was written against 1.0.6's source; an upgrade means reading it again.

## Roadmap

1. Foundations and security fixes
2. Capture and miner
3. Core restructure, routing by data class, encrypted vault
4. Electron shell
5. Enterprise readiness (Microsoft 365 connectors, Intune, marketplace)

The plans behind this, and the decisions under them, are not published.

## Layout

    seeress/cli.py          the command line; no logic of its own
    seeress/flows.py        create, list, run, schedule flows
    seeress/agent.py        the loop: model ⇄ python ⇄ browser
    seeress/bodies.py       the browsers behind the two doors, and the marker between them
    seeress/mine.py          the work of `mine`: the record read, mined and reported
    seeress/brain.py        OpenAI-compatible chat client (stdlib only); endpoint choice
    seeress/browser_api.py  the helper table, the generated prompt, the name allow-list
    seeress/runner.py       runs one step of the browser tool and reports what came back
    seeress/env.py          the environment every child process gets
    seeress/egress.py       judges the record of what Seeress's processes contacted
    seeress/egress_hook.py  writes that record; installed only by the egress check
    seeress/config.py       profiles, settings file, domain matching
    seeress/miner.py        finds what repeats in the record: sittings, sequences of sites, gaps
    seeress/proposals.py    the flow and the proposal: what is suggested, what is compiled
    seeress/activity.py     reads the activity record into events, and nothing more of it
    seeress/errors.py       typed errors; each one exits 1
    seeress/watcher.py      activity logger (spike) and analysis
    contracts/              schemas of what crosses a boundary
    tools/egress_check.py   what Seeress contacts, through both doors
    tools/publish_public.py carries the code over to the public repository
    tests/unit, tests/contract, tests/e2e
    bin/seeress-personal / bin/seeress-client   the two doors

## The activity logger (a spike)

    ./bin/seeress-client watch-start            # start logging (LaunchAgent, survives reboot)
    ./bin/seeress-client watch-stop             # stop
    ./bin/seeress-client watch-analyze          # mine: time/domain, repeated sequences + local-brain flow proposals
    ./bin/seeress-client watch-analyze --hours 24 --no-llm

Records tab events (CDP WebSocket) + focus stream (AppleScript) to
`~/.seeress/watch/spike-*.jsonl` — everything local, nothing leaves the machine.
It records only while Chrome's remote debugging is on.

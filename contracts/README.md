# Contracts

Every message that crosses a boundary today has a schema here, and the schema is the only
place its shape is written down. The rows of the phases to come name a message and have no
schema yet. A boundary is a process, a language or a trust zone:

| Boundary | Examples | Phase |
|---|---|---|
| Process: Seeress to the browser tool | [Browser body probe](browser-body-probe.schema.json) | 0 |
| Process: the capture to `seeress capture-status` | [Capture status](capture-status.schema.json) | 1 |
| Trust zone: the activity record to what reads it | [Activity event](activity-event.schema.json) | 1 |
| Process: the miner to the compiler | [Flow](flow.schema.json) | 1 |
| Trust zone: the miner to the person who decides | [Flow proposal](flow-proposal.schema.json) | 1 |
| Process: shell to core | JSON-RPC 2.0 over stdio, one object per line | 2 |
| Trust zone: device to tenant | The action log, a published flow | 2 |
| Model output | An action, a compiled flow | 1, 2 |

Schemas are [JSON Schema](https://json-schema.org/draft/2020-12/schema), draft 2020-12.

## Rules

- A message is validated where it is read, never only where it is written.
- A field is never removed and its meaning never changes. Add a field, or add a new
  optional member to an enumeration. Anything else needs a new schema with a new `$id`.
- `tests/contract/` checks that each schema and the code that reads its message agree:
  whatever one accepts, the other accepts, with the same fields. It does so on an instance
  written for each field and on generated ones. That is a check on those instances, not a
  proof of the two as a whole: on 2026-11-26 a review found a value the schema took and the
  reader refused, after every instance had agreed.
- A pattern is written in the ECMA-262 dialect, which every language that reads the schema
  can read. One that must end at the end of a string ends in `(?![\s\S])` and not in `$`: in
  Python, `$` also holds before a final newline.
- Code is generated from these schemas from Phase 2 on, when the sidecar interface exists.
  Until then there are a few messages, and reading them by hand is the smaller risk.

## Messages

### Browser body probe

Printed by the browser tool on behalf of `seeress/bodies.py` and read by `Probe.parse`.
It carries no page content and no personal data: a daemon name, a kind, a random marker
and an error name.

### Activity event

Written by `write_event` in `seeress/activity.py` for the capture, and made by `read_events`
in the same file from the lines the spike logger writes. `Event.parse` reads one back. It is
one thing a person did in their browser: a `navigate`, `focus`, `click`, `input`, `submit` or
`select`, on one site and one path, in one tab or in none, at one second of the machine's
local time.

It carries the host name in lower case without a leading `www.`, and the path with its query
and its fragment removed. A tab is named by the first eight characters of its identifier as
the browser writes them, which is hexadecimal in upper case. It never carries a page title,
typed text, a query, a fragment or a full URL. What reaches a model is the site, the hour
and counts: a path is shown to the person and never sent.

For a `click`, an `input`, a `submit` and a `select`, and for no other kind, an event may
carry an `action`: what was done in the page. It holds the element's tag, a fingerprint that
tells the element apart on its page and, for an `input`, the kind of the field. A password, a
one-time code and a card are named by their kind. A listener in the page reports it through a
binding, from a world of the page that the page's own scripts cannot reach, and it never
reads what a field holds. The rules that tie an action to its event are in the schema, with
`if` and `then`, and in the reader: an action is there only for those four kinds, its `type`
is the event's `kind`, and an `input` has a `field` while no other action has one.

### Flow

What the compiler is given. A command of Seeress writes it, from a proposal the user has
approved, and `Flow.parse` in `seeress/proposals.py` reads it. It is a name, a goal and one
to fifty sites in the order the task visits them. Once someone has written what is done on
each site, it has a step per site whose `intent` says so in a person's words. Nothing else
of the activity record goes into it: no path, no count, no time.

### Flow proposal

What the miner suggests. The miner writes it, and `Proposal.parse` in the same file reads
it. It is a flow with the evidence for it: on how many days and in how many sessions the
pattern was seen, when it was first and last seen, and at what hour it usually starts. The
evidence is counted, and never written by a model.

Two fields are not counted. The goal is a model's one sentence on what the task may be, until
the user writes the goal in their own words, which approves the proposal. The confidence is
the model's, from 1 to 5, or null when no model was asked or when its sentence was not kept.

Two fields say what kind of proposal it is. `order` is `fixed` when the sites recur in this
order, and `varies` when they recur as a set, in any order, most often seen first. `status`
is where it stands: `proposed`, `approved` by the user, `compiled` into a flow, or
`dismissed` by the user.

The free text a proposal carries is the sentence of its goal and, once a person has written
them, the intents of its steps. Otherwise it carries sites, an hour, days and counts. It
never carries a path, a page title, typed text, a query, a fragment or a full URL.

### Capture status

Written by `write_status` in `seeress/capture.py` for the capture, whole, in place of what
was there, and read back by `_report` in the same file for `seeress capture-status`. It says
whether the capture is recording: the process, the state, the reason when it is down or has
failed, since when, how many events were written and when the last was.

It holds no page content, and nothing of any event but the time of the last one. A reader
that meets a state or a field it does not know leaves it as it is. The schema judges the
shape of a time, not whether the moment exists; the reader judges both.

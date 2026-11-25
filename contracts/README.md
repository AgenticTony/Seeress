# Contracts

Everything that crosses a boundary has a schema here, and the schema is the only place the
shape is written down. A boundary is a process, a language or a trust zone:

| Boundary | Examples | Phase |
|---|---|---|
| Process: Seeress to the browser tool | [Browser body probe](browser-body-probe.schema.json) | 0 |
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
- `tests/contract/` proves that each schema and the code that reads its message agree:
  whatever one accepts, the other accepts, with the same fields.
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

Made by `read_events` in `seeress/activity.py` from the lines the activity logger writes, and
read back by `Event.parse` in the same file. It is one thing a person did in their browser:
a `navigate`, `focus`, `click`, `input`, `submit` or `select`, on one site and one path, in
one tab or in none, at one second of the machine's local time.

It carries the host name in lower case without a leading `www.`, and the path with its query
and its fragment removed. It never carries a page title, typed text, a query, a fragment or
a full URL. What may reach a model is the site, the path, the hour and counts.

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

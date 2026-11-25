# Contracts

Everything that crosses a boundary has a schema here, and the schema is the only place the
shape is written down. A boundary is a process, a language or a trust zone:

| Boundary | Examples | Phase |
|---|---|---|
| Process: Seeress to the browser tool | [Browser body probe](browser-body-probe.schema.json) | 0 |
| Process: shell to core | JSON-RPC 2.0 over stdio, one object per line | 2 |
| Trust zone: device to tenant | The action log, a published flow | 2 |
| Model output | An action, a compiled flow | 1, 2 |

Schemas are [JSON Schema](https://json-schema.org/draft/2020-12/schema), draft 2020-12.

## Rules

- A message is validated where it is read, never only where it is written.
- A field is never removed and its meaning never changes. Add a field, or add a new
  optional member to an enumeration. Anything else needs a new schema with a new `$id`.
- `tests/contract/test_contracts.py` proves that each schema and the code that reads its
  message agree: whatever one accepts, the other accepts, with the same fields.
- Code is generated from these schemas from Phase 2 on, when the sidecar interface exists.
  Until then there is one message, and reading it by hand is the smaller risk.

## Messages

### Browser body probe

Printed by the browser tool on behalf of `seeress/bodies.py` and read by `Probe.parse`.
It carries no page content and no personal data: a daemon name, a kind, a random marker
and an error name.

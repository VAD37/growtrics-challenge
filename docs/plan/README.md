# Plan

Design round 2. Backend first, generation last. Input context is `../notes.md`.

Review round 2b has been folded into the docs below. `11-triage.md` records what it confirmed
and what it changed, so the reasoning behind a moved decision is not lost in the diff.

This directory is design, not scope. It exists to make the boundaries and the data crossing
them concrete enough to review, then cut. The cut has happened: `../demo.md` is the approved
build order and it moved out of here on purpose.

In a hurry: `../demo.md` first, then `12-data-control.md` for the system in one diagram,
`13-mvp.md` for the frozen schema and contract the demo subsets, and `14-api-schema.md` for the
types a client touches.

## Reading order

| Doc | Question it answers |
|-----|---------------------|
| `01-domain-model.md` | What are the things, what are they called, what may change them |
| `02-context-map.md` | Which subsystems exist, and what payload crosses each seam |
| `03-module-layout.md` | Where that lives as Python packages, and which way imports point |
| `04-api-design.md` | What the client sends, polls, and receives |
| `05-workflow-engine.md` | How a run survives a crash, a timeout, or a bad step |
| `06-trust-boundary.md` | How user text and agent output are contained |
| `07-distribution.md` | Queues, leases, delivery guarantees, failure domains |
| `08-observability.md` | Events, metrics, and what the user can see of their own job |
| `09-generation-worker.md` | The deferred agent service, as an interface only |
| `10-scope-matrix.md` | Build, stub, or ignore, per subsystem |
| `11-triage.md` | Review round 2b against the plan: what it confirmed, what it changed |
| `12-data-control.md` | Who owns which data, who may see it, and the stripped-down API-to-output picture |
| `13-mvp.md` | The frozen SQL schema and API contract, and the staged build checklist |
| `14-api-schema.md` | The edge types themselves: request schema, job document, and what a deliverable is |

The picked scope is not in this directory. `../demo.md` is the approved subset that gets built:
six endpoints, six tables, four stages (D085, D100).

## Conventions used in these docs

- `@TODO` marks work deliberately left for later. `@audit` marks a place where a reviewer
  should challenge the design or where a security assumption is unproven. Both carry into
  the code as comments so the gaps stay visible.
- Requirement ids (`R1`, `N3`, `Q2`) refer to `../challenges/00-requirements.md`.
- Names in `CamelCase` are domain types. Names in `SCREAMING_CASE` are enum members.
- Payload sketches are illustrative shapes, not final schemas.

# Uptime Monitor Organ — Contract

A **pure decider** for the per-surface uptime down-detector. Given one
monitored surface's prior state and the outcome of a single fresh HTTP
probe, it decides health, the updated failure counters, whether a
**transition** fired this tick, and which **layer** is at fault.

Extracted from discovery-engine's `app/services/uptime_monitor.py`
(`_is_healthy` + the per-surface transition logic inside
`run_uptime_checks` + `classify_surface_failure`).

## What the organ does (pure core)

- `_is_healthy` — reachable AND (status == expected OR any 2xx).
- transition logic — increments `consecutive_failures`, flips
  `currently_down` when the threshold is crossed, and reports exactly one
  of `down` / `recovered` / `none`.
- `_classify` — names the failing layer (`ok` / `edge` / `origin` /
  `unreachable` / `client`) with an operator advice line, so the spine
  never says "restart the origin" for an edge/cert fault.

## What stays in the spine (impure)

- Issuing the HTTP probe (`requests.get`) — the organ is *handed* the
  outcome in `state`.
- Loading + persisting the `MonitoredSurface` row (DB).
- Stamping `down_since` / clearing it — needs the wall clock; the organ
  only reports the transition.
- Inserting the `surface_down` / `surface_recovered` PendingWidgetAction +
  Telegram/WhatsApp escalation — the organ only *advises* via `transition`.
- Reading the down-threshold from `UPTIME_DOWN_THRESHOLD` — passed in
  `context`.

## Ports (see `ports.json`)

### INPUT `state`

| name                   | type    | required | meaning                                         |
|------------------------|---------|----------|-------------------------------------------------|
| `reachable`            | boolean | yes      | did the HTTP probe connect at all?              |
| `status_code`          | integer | no       | HTTP status (null/absent when unreachable)      |
| `expected_status`      | integer | no       | surface's expected status (default 200)         |
| `consecutive_failures` | integer | no       | prior non-healthy streak (default 0)            |
| `currently_down`       | boolean | no       | prior down flag — the per-surface dedup state   |

### INPUT `context` (optional knobs — NOT ports)

| key              | default | meaning                                       |
|------------------|---------|-----------------------------------------------|
| `down_threshold` | 2       | consecutive failures before a `down` transition (floored to 1) |

### OUTPUT (under `output`)

| name                   | type    | meaning                                        |
|------------------------|---------|------------------------------------------------|
| `healthy`              | boolean | reachable AND acceptable status                |
| `consecutive_failures` | integer | updated streak (0 when healthy)                |
| `currently_down`       | boolean | updated down flag                              |
| `transition`           | string  | `down` / `recovered` / `none`                  |
| `layer`                | string  | `ok` / `edge` / `origin` / `unreachable` / `client` / `unknown` |
| `label`                | string  | human label, e.g. `edge 526 (cert)`            |
| `advice`               | string  | one-line operator hint (empty when healthy)    |

The envelope also carries `rationale` (string) and `self_metric`
(`confidence`, `decision_path`, `transition`, `layer`) — diagnostic, not
ports.

## Guarantees

- **Pure**: all inputs via JSON, no DB / network / clock calls,
  deterministic, stdlib-only.
- **Fail-safe**: malformed / empty `state` returns a `transition: "none"`,
  `layer: "unknown"` verdict — never a confident-wrong `down` page.
- **Faithful**: the health, transition, and classification logic mirror
  the source service line-for-line; only the threshold (env → context)
  and the I/O / clock (→ spine) moved.

## Connection standard

`ports.json` declares the typed inputs/outputs per the orchestrator's
connection standard (`CONNECTORS.md`). `types.json` is **vendored** — the
canonical `Data-Flow-Advisory/orchestrator@feat/drift-gate/types.json`
returned 404 at build time (2026-06-11), so the JSON-primitive subset this
organ uses is vendored to keep conformance self-contained. Reconcile when
the orchestrator ref is reachable. `check_ports.py` (run in CI) asserts
`ports.json` parses, every type is in the vocabulary, and `decide()` reads
/ writes exactly the declared port names.

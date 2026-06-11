# organ-uptime-monitor

Pure decision organ for the external-surface uptime down-detector.

Given one monitored surface's prior state plus a single fresh HTTP probe
outcome, `decide(state, context)` returns whether the surface is healthy,
the updated failure counters, the transition that fired (`down` /
`recovered` / `none`), and the failing layer (`edge` / `origin` /
`unreachable` / `client` / `ok`).

This is the pure core of discovery-engine's
`app/services/uptime_monitor.py`. The probe, the DB, the wall clock, and
the alert/escalation are the spine's job — the organ only decides.

## Run

```bash
# on a sample
ORGAN_INPUT=samples/down_threshold_crossed.json python3 organ.py

# or pipe a payload
echo '{"state": {"reachable": true, "status_code": 526}}' | python3 organ.py
```

## Test

```bash
python3 check_ports.py    # ports connection-standard conformance
python3 -m pytest -v      # unit tests
```

See `CONTRACT.md` for the full ports table and guarantees.

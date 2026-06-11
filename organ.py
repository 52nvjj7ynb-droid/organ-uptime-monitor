#!/usr/bin/env python3
"""
Uptime Monitor Organ — extracted decision logic from discovery-engine.

A pure decider that takes ONE monitored surface's prior state plus the
outcome of a single fresh HTTP probe and decides:

  * is this probe *healthy* (reachable AND an acceptable status code),
  * the new ``consecutive_failures`` count + ``currently_down`` flag,
  * whether a *transition* fired this tick (``down`` / ``recovered`` /
    ``none``) — the actionable signal the spine alerts on,
  * which *layer* is at fault (ok / edge / origin / unreachable / client)
    so the operator is never told "restart the origin" for an edge/cert
    fault.

This is the pure core of discovery-engine's
``app/services/uptime_monitor.py`` (``_is_healthy`` + the per-surface
transition logic inside ``run_uptime_checks`` + ``classify_surface_failure``).
The impure parts are the spine's job:
  * issuing the HTTP probe (``_probe`` — requests.get) — the organ is
    *handed* the probe outcome in ``state``, it never makes the call,
  * loading + persisting the ``MonitoredSurface`` row (DB reads/writes) —
    the organ returns the new counters; the spine writes them,
  * stamping ``down_since`` / clearing it (needs the wall clock) — the
    organ only reports the *transition*; the spine timestamps it,
  * inserting the ``surface_down`` / ``surface_recovered``
    PendingWidgetAction + Telegram/WhatsApp escalation — the organ only
    *advises* via ``transition``,
  * reading the down-threshold from ``UPTIME_DOWN_THRESHOLD`` — passed in
    ``context``.

Contract (see CONTRACT.md):
  INPUT state (the prior surface state + this probe's outcome): {
    "reachable": true,            # did the HTTP probe connect at all?
    "status_code": 200,           # HTTP status (null when unreachable)
    "expected_status": 200,       # surface's expected status (default 200)
    "consecutive_failures": 0,    # prior non-healthy streak
    "currently_down": false       # prior down flag (dedup state)
  }

  INPUT context (all optional — organ works with context absent): {
    "down_threshold": 2           # consecutive failures before "down"
  }

  OUTPUT: {
    "output": {
      "healthy": bool,                # reachable AND acceptable status
      "consecutive_failures": int,    # updated streak (0 when healthy)
      "currently_down": bool,         # updated down flag
      "transition": str,              # down / recovered / none
      "layer": str,                   # ok / edge / origin / unreachable / client / unknown
      "label": str,                   # human label e.g. "edge 526 (cert)"
      "advice": str                   # one-line operator hint
    },
    "rationale": str,
    "self_metric": { "confidence": float, "decision_path": str,
                     "transition": str, "layer": str }
  }

The organ is pure: all inputs via JSON, no DB/network/clock calls,
deterministic, stdlib-only, fail-safe to a *no-transition* "unknown"
verdict (never a confident-wrong "down" page) on malformed / empty
``state``.
"""
from __future__ import annotations

import json
import os
import sys

# Cloudflare's edge-origin error range (520-527). 525/526 are TLS/cert
# handshake failures between the edge and the origin; 521-524 are
# origin-down/timeout signals surfaced by the edge. All are EDGE-layer:
# the origin app process itself may be perfectly healthy. Ported verbatim
# from app/services/uptime_monitor.py::_CLOUDFLARE_EDGE_CODES.
_CLOUDFLARE_EDGE_CODES = frozenset(range(520, 528))

_DEFAULT_DOWN_THRESHOLD = 2


def _int(value, default: int, floor: int = 0) -> int:
    """Coerce a value to a floored int, falling back to ``default`` on
    None/garbage. Mirrors the try/except int() pattern in the source's
    ``_down_threshold`` reader."""
    try:
        if value is None:
            return int(default)
        return max(floor, int(value))
    except (TypeError, ValueError):
        return int(default)


def _is_healthy(reachable: bool, status_code, expected_status: int) -> bool:
    """A surface is healthy iff reachable AND returns its expected status
    (default 200). Any other 2xx is also treated as healthy so a root that
    204/200s doesn't false-alarm — but a 5xx/4xx is down. Faithful port of
    ``app/services/uptime_monitor.py::_is_healthy``."""
    if not reachable:
        return False
    if status_code == expected_status:
        return True
    return 200 <= (status_code or 0) < 300


def _classify(status_code, reachable: bool) -> dict:
    """Name the failing layer for one probe outcome. Pure — no I/O.
    Faithful port of ``classify_surface_failure``. Returns
    ``{"layer", "label", "advice"}``."""
    if not reachable or status_code is None:
        return {
            "layer": "unreachable",
            "label": "unreachable",
            "advice": "DNS / TLS / connection failed — check edge + DNS, not the app",
        }
    if status_code in _CLOUDFLARE_EDGE_CODES:
        is_cert = status_code in (525, 526)
        return {
            "layer": "edge",
            "label": f"edge {status_code}" + (" (cert)" if is_cert else ""),
            "advice": (
                "Cloudflare edge/cert error — origin may be fine; check the "
                "SSL cert / edge, do NOT restart the origin"
                if is_cert else
                "Cloudflare edge reports origin unreachable/timeout — check "
                "the origin connection, not necessarily a crash"
            ),
        }
    if 500 <= status_code <= 599:
        return {
            "layer": "origin",
            "label": f"origin {status_code}",
            "advice": "Origin 5xx — the app process errored; check logs / restart origin",
        }
    if 400 <= status_code <= 499:
        return {
            "layer": "client",
            "label": f"http {status_code}",
            "advice": "4xx — likely a config/auth/route issue, not a crash",
        }
    return {"layer": "ok", "label": f"http {status_code}", "advice": ""}


def _result(
    *,
    healthy: bool,
    consecutive_failures: int,
    currently_down: bool,
    transition: str,
    classification: dict,
    confidence: float,
    decision_path: str,
    rationale: str,
) -> dict:
    """Build the standard envelope. ``output`` always carries EXACTLY the
    seven declared output ports so the ports-conformance writes-exactly
    check holds on every path (including skip / error)."""
    return {
        "output": {
            "healthy": healthy,
            "consecutive_failures": consecutive_failures,
            "currently_down": currently_down,
            "transition": transition,
            "layer": classification["layer"],
            "label": classification["label"],
            "advice": classification["advice"],
        },
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "decision_path": decision_path,
            "transition": transition,
            "layer": classification["layer"],
        },
    }


def decide(state: dict, context: dict | None = None) -> dict:
    """Evaluate one surface's fresh probe against its prior state and decide
    the new down-detector state + transition. Pure, deterministic, fail-safe
    to a no-transition ``unknown`` verdict (never a confident-wrong page)."""
    context = context or {}
    try:
        threshold = _int(
            context.get("down_threshold"), _DEFAULT_DOWN_THRESHOLD, floor=1
        )

        if not isinstance(state, dict) or not state:
            return _result(
                healthy=False,
                consecutive_failures=0,
                currently_down=False,
                transition="none",
                classification={"layer": "unknown", "label": "unknown", "advice": ""},
                confidence=0.3,
                decision_path="empty_state",
                rationale=(
                    "No state supplied — no probe outcome to evaluate; "
                    "no transition (fail-safe, never a false 'down')."
                ),
            )

        reachable = bool(state.get("reachable"))
        status_code = state.get("status_code")
        expected_status = _int(state.get("expected_status"), 200, floor=1)
        prior_failures = _int(state.get("consecutive_failures"), 0)
        prior_down = bool(state.get("currently_down"))

        healthy = _is_healthy(reachable, status_code, expected_status)
        classification = _classify(status_code, reachable)

        if healthy:
            new_failures = 0
            new_down = False
            if prior_down:
                transition = "recovered"
                rationale = (
                    f"Surface healthy (HTTP {status_code}) after being down — "
                    "recovery transition; spine fires ONE recovered alert + "
                    "clears down state."
                )
                path = "recovered"
            else:
                transition = "none"
                rationale = (
                    f"Surface healthy (HTTP {status_code}); no transition, "
                    "failure streak reset to 0."
                )
                path = "healthy"
        else:
            new_failures = prior_failures + 1
            new_down = prior_down
            transition = "none"
            path = "failing"
            if new_failures >= threshold and not prior_down:
                new_down = True
                transition = "down"
                path = "down"
                rationale = (
                    f"Surface {classification['label']} for {new_failures} "
                    f"consecutive check(s) (threshold {threshold}) — DOWN "
                    "transition; spine fires ONE down alert. "
                    + (classification["advice"] or "")
                ).strip()
            elif prior_down:
                rationale = (
                    f"Surface still {classification['label']} ({new_failures} "
                    "consecutive); already flagged down, no re-alert."
                )
            else:
                rationale = (
                    f"Surface {classification['label']} ({new_failures}/"
                    f"{threshold} toward down); riding out the blip, no alert "
                    "yet."
                )

        return _result(
            healthy=healthy,
            consecutive_failures=new_failures,
            currently_down=new_down,
            transition=transition,
            classification=classification,
            confidence=1.0,
            decision_path=path,
            rationale=rationale,
        )
    except Exception as e:  # noqa: BLE001 — fail-safe to the conservative verdict
        return _result(
            healthy=False,
            consecutive_failures=0,
            currently_down=False,
            transition="none",
            classification={"layer": "unknown", "label": "unknown", "advice": ""},
            confidence=0.0,
            decision_path="decision_error",
            rationale=f"Organ error ({e}) — failing safe to no transition (no page).",
        )


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

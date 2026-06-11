"""Tests for the uptime-monitor organ + its ports-conformance check."""
import json
import os
import subprocess
import sys

import pytest

import organ

HERE = os.path.dirname(os.path.abspath(__file__))


# --- envelope shape ---------------------------------------------------

def _assert_envelope(result):
    assert set(result) == {"output", "rationale", "self_metric"}
    out = result["output"]
    assert set(out) == {
        "healthy", "consecutive_failures", "currently_down",
        "transition", "layer", "label", "advice",
    }
    assert isinstance(out["healthy"], bool)
    assert isinstance(out["consecutive_failures"], int)
    assert isinstance(out["currently_down"], bool)
    assert out["transition"] in {"down", "recovered", "none"}
    assert isinstance(out["layer"], str)
    assert isinstance(out["label"], str)
    assert isinstance(out["advice"], str)
    assert isinstance(result["rationale"], str) and result["rationale"]
    sm = result["self_metric"]
    assert 0.0 <= sm["confidence"] <= 1.0


def test_envelope_on_every_path():
    for state in (
        None,
        {},
        {"reachable": True, "status_code": 200},
        {"reachable": False, "status_code": None, "consecutive_failures": 1},
    ):
        _assert_envelope(organ.decide(state))


# --- healthy / transition behaviour ----------------------------------

def test_healthy_200_no_transition():
    r = organ.decide({"reachable": True, "status_code": 200,
                       "expected_status": 200, "consecutive_failures": 0,
                       "currently_down": False})
    out = r["output"]
    assert out["healthy"] is True
    assert out["transition"] == "none"
    assert out["consecutive_failures"] == 0
    assert out["currently_down"] is False
    assert out["layer"] == "ok"


def test_non_200_expected_is_accepted_when_matching():
    # A surface that expects 204 is healthy at 204.
    r = organ.decide({"reachable": True, "status_code": 204,
                      "expected_status": 204})
    assert r["output"]["healthy"] is True


def test_other_2xx_is_healthy():
    r = organ.decide({"reachable": True, "status_code": 201,
                      "expected_status": 200})
    assert r["output"]["healthy"] is True
    assert r["output"]["layer"] == "ok"


def test_down_transition_fires_at_threshold():
    # prior 1 failure + this failing check = 2 >= threshold 2 -> down.
    r = organ.decide(
        {"reachable": True, "status_code": 500, "consecutive_failures": 1,
         "currently_down": False},
        {"down_threshold": 2},
    )
    out = r["output"]
    assert out["healthy"] is False
    assert out["consecutive_failures"] == 2
    assert out["currently_down"] is True
    assert out["transition"] == "down"
    assert out["layer"] == "origin"


def test_blip_below_threshold_no_transition():
    r = organ.decide(
        {"reachable": True, "status_code": 500, "consecutive_failures": 0,
         "currently_down": False},
        {"down_threshold": 2},
    )
    out = r["output"]
    assert out["consecutive_failures"] == 1
    assert out["currently_down"] is False
    assert out["transition"] == "none"


def test_already_down_no_realert():
    r = organ.decide(
        {"reachable": True, "status_code": 500, "consecutive_failures": 5,
         "currently_down": True},
        {"down_threshold": 2},
    )
    out = r["output"]
    assert out["consecutive_failures"] == 6
    assert out["currently_down"] is True
    assert out["transition"] == "none"  # not re-fired


def test_recovered_transition():
    r = organ.decide({"reachable": True, "status_code": 200,
                      "consecutive_failures": 3, "currently_down": True})
    out = r["output"]
    assert out["healthy"] is True
    assert out["consecutive_failures"] == 0
    assert out["currently_down"] is False
    assert out["transition"] == "recovered"


# --- layer classification --------------------------------------------

def test_edge_cert_526():
    r = organ.decide({"reachable": True, "status_code": 526})
    out = r["output"]
    assert out["layer"] == "edge"
    assert "(cert)" in out["label"]
    assert "do NOT restart the origin" in out["advice"]


def test_edge_521_not_cert():
    r = organ.decide({"reachable": True, "status_code": 521})
    out = r["output"]
    assert out["layer"] == "edge"
    assert "(cert)" not in out["label"]


def test_unreachable():
    r = organ.decide({"reachable": False, "status_code": None})
    out = r["output"]
    assert out["healthy"] is False
    assert out["layer"] == "unreachable"


def test_client_4xx():
    r = organ.decide({"reachable": True, "status_code": 404})
    assert r["output"]["layer"] == "client"


# --- fail-safe --------------------------------------------------------

def test_empty_state_no_false_down():
    for state in (None, {}):
        out = organ.decide(state)["output"]
        assert out["transition"] == "none"
        assert out["currently_down"] is False
        assert out["layer"] == "unknown"


def test_threshold_floor_is_one():
    # A garbage / zero threshold must not let a single fail trip down on a
    # never-failed surface unless failures actually reach >=1.
    r = organ.decide(
        {"reachable": False, "status_code": None, "consecutive_failures": 0,
         "currently_down": False},
        {"down_threshold": 0},
    )
    # floored to 1 -> first failure (1) >= 1 -> down.
    assert r["output"]["transition"] == "down"


# --- CLI / ports conformance -----------------------------------------

def test_cli_runs_on_samples():
    sample_dir = os.path.join(HERE, "samples")
    for s in sorted(os.listdir(sample_dir)):
        if not s.endswith(".json"):
            continue
        env = dict(os.environ, ORGAN_INPUT=os.path.join(sample_dir, s))
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "organ.py")],
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        parsed = json.loads(proc.stdout)
        assert set(parsed) == {"output", "rationale", "self_metric"}


def test_ports_conformance_passes():
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "check_ports.py")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout

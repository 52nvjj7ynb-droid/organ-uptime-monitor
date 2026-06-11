#!/usr/bin/env python3
"""
Ports conformance check for the connection standard.

Asserts:
  1. ports.json parses and has the {inputs:[{name,type,required}],
     outputs:[{name,type}]} shape.
  2. every port type exists in the (vendored) types.json vocabulary.
  3. decide() READS exactly the declared input names from ``state``
     (static AST scan of ``state.get("<key>")`` / ``state["<key>"]``).
  4. decide() WRITES exactly the declared output names under ``output``
     (runtime: union of output-dict keys across the committed samples +
     the empty-state / skip paths must equal the declared output set).

Single-op organ, so input/output coverage is exact-match (no per-call
union needed). Run standalone: ``python3 check_ports.py``. Exit non-zero
on any violation.
"""
from __future__ import annotations

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


def _fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    # 1. ports.json shape -------------------------------------------------
    ports = _load("ports.json")
    if not isinstance(ports, dict):
        _fail("ports.json is not a JSON object")
    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        _fail("ports.json must have list-valued 'inputs' and 'outputs'")

    declared_inputs = set()
    for p in inputs:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"input port missing name/type: {p!r}")
        if "required" not in p or not isinstance(p["required"], bool):
            _fail(f"input port {p.get('name')!r} must declare boolean 'required'")
        declared_inputs.add(p["name"])

    declared_outputs = set()
    for p in outputs:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"output port missing name/type: {p!r}")
        declared_outputs.add(p["name"])

    # 2. every type in the vocabulary ------------------------------------
    vocab = _load("types.json").get("types", {})
    if not isinstance(vocab, dict) or not vocab:
        _fail("types.json has no 'types' vocabulary")
    for p in inputs + outputs:
        if p["type"] not in vocab:
            _fail(
                f"port {p['name']!r} type {p['type']!r} not in types.json "
                f"vocabulary {sorted(vocab)}"
            )

    # 3. decide reads exactly the declared input names -------------------
    with open(os.path.join(HERE, "organ.py")) as f:
        tree = ast.parse(f.read())

    read_keys = set()
    for node in ast.walk(tree):
        # state.get("<key>"...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "state"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            read_keys.add(node.args[0].value)
        # state["<key>"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "state"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            read_keys.add(node.slice.value)

    if read_keys != declared_inputs:
        _fail(
            f"decide() reads state keys {sorted(read_keys)} but ports.json "
            f"declares inputs {sorted(declared_inputs)}"
        )

    # 4. decide writes exactly the declared output names -----------------
    import organ  # noqa: E402

    produced = set()
    sample_dir = os.path.join(HERE, "samples")
    cases = []
    for s in sorted(os.listdir(sample_dir)):
        if s.endswith(".json"):
            payload = _load(os.path.join("samples", s))
            cases.append((payload.get("state"), payload.get("context")))
    # also exercise the skip / error paths so every code path is covered
    cases.append(({}, None))
    cases.append(({"reachable": True, "status_code": 200}, None))
    cases.append((None, None))

    for state, context in cases:
        result = organ.decide(state, context)
        out = result.get("output")
        if not isinstance(out, dict):
            _fail(f"decide() output is not a dict for state={state!r}")
        produced |= set(out.keys())

    if produced != declared_outputs:
        missing = declared_outputs - produced
        extra = produced - declared_outputs
        _fail(
            "decide() output keys do not match ports.json outputs exactly: "
            f"declared-but-never-produced={sorted(missing)}, "
            f"produced-but-undeclared={sorted(extra)}"
        )

    print(
        f"OK: ports.json conforms — {len(declared_inputs)} input(s), "
        f"{len(declared_outputs)} output(s), all types in vocabulary, "
        "reads + writes match decide()."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

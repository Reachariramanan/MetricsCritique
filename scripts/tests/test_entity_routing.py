#!/usr/bin/env python3
"""Offline tests for the Gemma routing / entity helpers (no network, no DuckDB).

Run:  .venv/bin/python -m pytest scripts/tests/test_entity_routing.py
  or: .venv/bin/python scripts/tests/test_entity_routing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import build_entity_schema as B  # noqa: E402
from lib import entity_routing as R  # noqa: E402
from lib.gemma_client import parse_json_object  # noqa: E402


def test_schema_matches_markdown():
    # The committed JSON must equal what the markdown produces right now.
    assert B.OUT.read_text(encoding="utf-8") == __import__("json").dumps(
        B.build(), indent=2, ensure_ascii=False) + "\n", "run scripts/build_entity_schema.py"


def test_schema_shape():
    s = R.load_schema()
    assert s["domain_ids"] == [f"D0{i}" for i in range(1, 10)]
    assert {"LOC_POINT", "ACT_INITIATOR", "EVT_MODE"} <= {t["type"] for t in s["l1"]}
    assert all(s["l2"][d]["types"] for d in s["domain_ids"])
    assert {s["l2"][d]["bucket"] for d in s["domain_ids"]} == {
        "Political-Civil", "Economic", "Cyber", "Terrorism/Breach"}


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1,}\n```') == {"a": 1}
    assert parse_json_object('sure! {"a": {"b": "}"}} done') == {"a": {"b": "}"}}
    assert parse_json_object("no json") is None
    assert parse_json_object('{"a": ') is None


def test_validate_route():
    ok = {"domain_1": "D01", "confidence_1": 0.8, "domain_2": "D09", "event_mode": "factual"}
    assert R.validate_route(ok) is None
    assert R.validate_route({**ok, "domain_1": "D99"})
    assert R.validate_route({**ok, "domain_2": "D01"})           # runner-up == top
    assert R.validate_route({**ok, "domain_2": "none"}) is None
    assert R.validate_route({**ok, "confidence_1": 1.4})
    assert R.validate_route({**ok, "event_mode": "maybe"})


def test_allowed_types_levels():
    a = R.allowed_types(["D07"])
    assert a["LOC_POINT"]["level"] == "L1"
    assert any(m["level"] == "L2" and m["domain"] == "D07" for m in a.values())
    assert R.allowed_types(["none"]).keys() == R.allowed_types([]).keys()   # none -> L1 only


def test_validate_entities():
    good = {"entities": [{"type": "LOC_POINT", "name": "Kyiv", "confidence": 0.9}]}
    assert R.validate_entities(good, ["D01"]) is None
    assert R.validate_entities({"entities": []}, []) is None
    assert R.validate_entities({"entities": [{"type": "NOPE", "name": "x"}]}, ["D01"])
    assert R.validate_entities({"entities": [{"type": "LOC_POINT", "name": " "}]}, [])
    assert R.validate_entities({"entities": "x"}, [])


def test_evidence_and_keys():
    text = "Президент  Путин  заявил о ПЕРЕМИРИИ в Киеве."
    assert R.evidence_in_text("путин заявил", text)
    assert not R.evidence_in_text("", text)
    assert not R.evidence_in_text("Zelensky", text)
    assert R.name_key("德 国 救 生") == R.name_key("德国救生")
    assert R.name_key("  Xi   JINPING ") == "xi jinping"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)} passed")

"""Checks for the parameter-fault dataset.

Fast checks run on the saved instances. Simulation checks need ngspice + SKY130:
  pytest                      # fast checks + originals pass
  RESIM=1 pytest              # also re-simulate every faulted netlist
"""
import json
import os
from pathlib import Path

import pytest

from pipeline.netlist import format_value, parse_value, strip_comments
from pipeline.simulate import DEFAULT_LIB, simulate
from pipeline.verify import all_pass, check, load_spec

ROOT = Path(__file__).resolve().parents[1]
CIRCUITS = sorted(p.parent for p in (ROOT / "circuits").glob("*/spec.yaml"))
INSTANCES = sorted(p.parent for p in (ROOT / "dataset/parameter").glob("*/*/fault.json"))
needs_sim = pytest.mark.skipif(not DEFAULT_LIB.exists(), reason="SKY130 models not installed")


@pytest.mark.parametrize("text,value", [("2.2k", 2200), ("3p", 3e-12), ("750k", 7.5e5),
                                        ("72", 72), ("1.0", 1.0), ("10meg", 1e7)])
def test_parse_value(text, value):
    assert parse_value(text) == pytest.approx(value)


def test_format_keeps_suffix():
    assert format_value(1100.0, "2.2k") == "1.1k"
    assert format_value(36.0, "72") == "36"


@needs_sim
@pytest.mark.parametrize("circuit", CIRCUITS, ids=lambda p: p.name)
def test_original_passes(circuit):
    spec = load_spec(circuit / "spec.yaml")
    clean = strip_comments((circuit / "circuit.spi").read_text())
    r = simulate(clean, circuit / spec["testbench"], expected=[s["metric"] for s in spec["specs"]])
    assert r.ok, r.error
    assert all_pass(check(r.metrics, spec))


@pytest.mark.parametrize("inst", INSTANCES, ids=lambda p: p.name)
def test_instance_is_reversible(inst):
    fault = json.loads((inst / "fault.json").read_text())
    orig = (inst / "original.spi").read_text().splitlines()
    faulted = (inst / "faulted.spi").read_text().splitlines()
    assert len(orig) == len(faulted)
    diff = [i for i, (a, b) in enumerate(zip(orig, faulted)) if a != b]
    assert [i + 1 for i in diff] == [e["line"] for e in fault["edits"]]
    restored = list(faulted)
    for p in fault["inverse_patch"]:
        restored[p["line"] - 1] = p["replace_with"]
    assert [l.split() for l in restored] == [l.split() for l in orig]


@pytest.mark.parametrize("inst", INSTANCES, ids=lambda p: p.name)
def test_instance_fails_clearly_and_leaks_nothing(inst):
    fault = json.loads((inst / "fault.json").read_text())
    assert fault["clear_failures"], "saved instance has no clear spec failure"
    text = (inst / "faulted.spi").read_text()
    assert not any(l.strip().startswith("*") for l in text.splitlines()), "comments leak design intent"


@needs_sim
@pytest.mark.skipif(not os.environ.get("RESIM"), reason="set RESIM=1 to re-simulate instances")
@pytest.mark.parametrize("inst", INSTANCES, ids=lambda p: p.name)
def test_instance_resimulates_as_failing(inst):
    spec = load_spec(inst / "spec.yaml")
    r = simulate((inst / "faulted.spi").read_text(), inst / "testbench.spi",
                 expected=[s["metric"] for s in spec["specs"]])
    assert r.ok, r.error
    assert any(c.clear_fail for c in check(r.metrics, spec))

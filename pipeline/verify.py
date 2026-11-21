"""Check simulated metrics against a circuit's spec.yaml.

spec.yaml format::

    circuit: bandgap_ref
    subckt: bandgap_reference
    testbench: testbench.spi
    specs:
      - metric: vref_v
        min: 1.18          # optional
        max: 1.26          # optional
        margin: 0.02       # how far past a limit counts as a "clear" failure
        kind: static       # static (op/dc) or dynamic (ac/tran)
        desc: Reference voltage at tt / 1.8 V / 27 C
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml


@dataclass
class SpecCheck:
    metric: str
    value: float
    min: float | None
    max: float | None
    margin: float
    kind: str
    passed: bool
    excess: float          # how far outside the limits (0 if inside), in metric units
    clear_fail: bool       # excess >= margin

    def to_dict(self) -> dict:
        return asdict(self)


def load_spec(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def check(metrics: dict[str, float], spec: dict) -> list[SpecCheck]:
    out = []
    for s in spec["specs"]:
        v = metrics.get(s["metric"], math.nan)
        lo, hi = s.get("min"), s.get("max")
        if math.isnan(v):
            excess = math.inf
        else:
            excess = max(0.0, (lo - v) if lo is not None else 0.0, (v - hi) if hi is not None else 0.0)
        out.append(SpecCheck(
            metric=s["metric"], value=v, min=lo, max=hi, margin=s["margin"],
            kind=s.get("kind", "static"), passed=excess == 0.0, excess=excess,
            clear_fail=excess >= s["margin"],
        ))
    return out


def all_pass(checks: list[SpecCheck]) -> bool:
    return all(c.passed for c in checks)


def summary(checks: list[SpecCheck]) -> str:
    rows = []
    for c in checks:
        lim = f"[{'' if c.min is None else c.min}, {'' if c.max is None else c.max}]"
        flag = "PASS" if c.passed else ("FAIL" if c.clear_fail else "fail(marginal)")
        rows.append(f"  {c.metric:<16} {c.value:>12.5g}  {lim:<16} {flag}")
    return "\n".join(rows)

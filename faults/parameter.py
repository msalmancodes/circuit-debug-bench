"""Parameter-fault injection.

For each base circuit:
  1. Strip comments from the reference netlist (they quote original values) and
     check that the clean netlist passes every spec.
  2. Enumerate every editable value: MOSFET W/L/m, resistor length, MIM-cap width,
     BJT multiplier, ideal R/C/L value. Scale each by a set of factors.
  3. Simulate every candidate. Keep it only if it still simulates and fails at
     least one spec by its clear-failure margin. Drop duplicates (same symptoms).
  4. Label difficulty and pick a balanced, seeded subset. Write one instance
     folder per fault with the original/faulted netlists and fault.json.

Usage:
  python -m faults.parameter --circuit circuits/bandgap_ref --circuit circuits/capless_ldo
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.netlist import Device, format_value, parse_devices, parse_value, set_param, strip_comments
from pipeline.simulate import simulate
from pipeline.verify import all_pass, check, load_spec, summary

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACTORS = [0.1, 0.25, 0.5, 0.8, 1.25, 2.0, 4.0, 10.0]

# SKY130 layout limits; candidates below these are not realistic devices.
MIN_FET_L = 0.15
MIN_FET_W_PER_FINGER = 0.42
MIN_MIM_W = 2.0


@dataclass
class Site:
    device: Device
    param: str
    old_token: str
    old_value: float
    integer: bool
    implicit: bool = False   # param absent from the line (e.g. m=1 by default); edit inserts it


@dataclass
class Candidate:
    site: Site
    factor: float
    new_token: str
    faulted_text: str
    sim_ok: bool = False
    sim_error: str | None = None
    metrics: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    log: str = ""

    @property
    def failing(self):
        return [c for c in self.checks if not c.passed]

    @property
    def clear(self):
        return [c for c in self.checks if c.clear_fail]

    @property
    def valid(self) -> bool:
        return self.sim_ok and bool(self.clear)

    @property
    def severity(self) -> float:
        return max((c.excess / c.margin for c in self.clear), default=0.0)

    @property
    def status(self) -> str:
        if not self.sim_ok:
            return "sim_failed"
        if self.clear:
            return "kept"
        return "marginal" if self.failing else "silent"

    @property
    def difficulty(self) -> str:
        """hard:   only dynamic (AC/transient) specs fail; DC behaviour looks normal.
        medium: the value is off by at most 2x (subtle edit), static spec fails.
        easy:   the value is off by 4x or more (large edit), static spec fails."""
        if all(c.kind == "dynamic" for c in self.failing):
            return "hard"
        return "medium" if 0.5 <= self.factor <= 2.0 else "easy"


def editable_sites(devices: list[Device]) -> list[Site]:
    """Every value a parameter fault may change.

    Identical parallel devices (same nodes, model and parameters, e.g. the eight
    unit BJTs of a bandgap) are one site: changing any one of them is the same fault.
    Note: on SKY130 subcircuits `mult=` only scales Monte Carlo mismatch; the device
    count is `m=`, so BJT faults edit (or insert) `m`.
    """
    sites, seen_shapes = [], set()
    for d in devices:
        shape = tuple(d.line.split()[1:])
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        implicit = set()
        if d.kind == "X" and d.model:
            m = d.model.lower()
            if "fet" in m:
                names = ["w", "l"] + (["m"] if "m" in d.params else [])
            elif "res_" in m:
                names = ["l"]
            elif "cap_" in m:
                names = ["w"]
            elif "npn" in m or "pnp" in m:
                names = ["m"]
                if "m" not in d.params:
                    implicit.add("m")
            else:
                names = []
        elif d.kind in "RCL":
            names = ["value"]
        else:
            names = []
        for p in names:
            tok = "1" if p in implicit else d.params.get(p)
            val = parse_value(tok) if tok is not None else None
            if val is None or val <= 0:
                continue
            sites.append(Site(d, p, tok, val, integer=p in ("m", "mult", "nf"), implicit=p in implicit))
    return sites


def candidate_values(site: Site, factors: list[float]) -> list[tuple[float, str]]:
    out, seen = [], {site.old_value}
    d, model = site.device, (site.device.model or "").lower()
    for f in factors:
        new = site.old_value * f
        if site.integer:
            new = float(round(new))
            if new < 1:
                continue
        if new in seen:
            continue
        if "fet" in model:
            nf = parse_value(d.params.get("nf", "1")) or 1
            if site.param == "l" and new < MIN_FET_L:
                continue
            if site.param == "w" and new / nf < MIN_FET_W_PER_FINGER:
                continue
        if "cap_" in model and site.param == "w" and new < MIN_MIM_W:
            continue
        seen.add(new)
        token = str(int(new)) if site.integer else format_value(new, site.old_token)
        out.append((new / site.old_value, token))
    return out


def build_candidates(clean_text: str, factors: list[float]) -> list[Candidate]:
    lines = clean_text.splitlines()
    cands = []
    for site in editable_sites(parse_devices(clean_text)):
        for f, token in candidate_values(site, factors):
            new_lines = list(lines)
            new_lines[site.device.line_no] = set_param(site.device.line, site.param, token,
                                                       insert=site.implicit)
            cands.append(Candidate(site, f, token, "\n".join(new_lines) + "\n"))
    return cands


def signature(c: Candidate) -> tuple:
    """Candidates whose metrics agree to 3 significant figures look identical to a debugger
    (e.g. scaling either half of a matched pair), so only one is kept."""
    return tuple(sorted((k, float(f"{v:.3g}")) for k, v in c.metrics.items()))


def select(cands: list[Candidate], n: int, rng: random.Random) -> list[Candidate]:
    buckets = {"easy": [], "medium": [], "hard": []}
    for c in cands:
        buckets[c.difficulty].append(c)
    for b in buckets.values():
        rng.shuffle(b)
        # prefer spreading over devices: one per device first, repeats after
        seen, first, rest = set(), [], []
        for c in b:
            (rest if c.site.device.name in seen else first).append(c)
            seen.add(c.site.device.name)
        b[:] = first + rest
    quota = {k: n // 3 for k in buckets}
    for k in list(buckets)[: n % 3]:
        quota[k] += 1
    chosen, used_devices = [], set()
    # fill quotas, prefer devices not used yet in any bucket
    for k in buckets:
        take = []
        for c in buckets[k]:
            if len(take) >= quota[k]:
                break
            if c.site.device.name not in used_devices:
                take.append(c)
                used_devices.add(c.site.device.name)
        for c in buckets[k]:
            if len(take) >= quota[k]:
                break
            if c not in take:
                take.append(c)
        chosen += take
    # top up from whatever is left if a bucket was short
    leftovers = [c for k in buckets for c in buckets[k] if c not in chosen]
    chosen += leftovers[: max(0, n - len(chosen))]
    return chosen


def write_instance(out_dir: Path, inst_id: str, circuit_dir: Path, spec: dict, clean_text: str,
                   c: Candidate, orig_metrics: dict, seed: int) -> None:
    d = out_dir / inst_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "original.spi").write_text(clean_text)
    (d / "faulted.spi").write_text(c.faulted_text)
    shutil.copy(circuit_dir / "spec.yaml", d / "spec.yaml")
    shutil.copy(circuit_dir / spec["testbench"], d / "testbench.spi")
    (d / "sim.log").write_text(c.log)
    s = c.site
    faulted_line = c.faulted_text.splitlines()[s.device.line_no]
    fault = {
        "instance_id": inst_id,
        "circuit": spec["circuit"],
        "category": "parameter",
        "difficulty": c.difficulty,
        "seed": seed,
        "edits": [{
            "line": s.device.line_no + 1,
            "device": s.device.name,
            "model": s.device.model,
            "param": s.param,
            "old": s.old_token + (" (implicit default)" if s.implicit else ""),
            "new": c.new_token,
            "factor": round(c.factor, 4),
            "original_line": s.device.line.strip(),
            "faulted_line": faulted_line,
        }],
        # apply in order to faulted.spi to recover original.spi exactly
        "inverse_patch": [{"line": s.device.line_no + 1, "replace_with": s.device.line.strip()}],
        "failing_specs": [ch.metric for ch in c.failing],
        "clear_failures": [ch.metric for ch in c.clear],
        "severity": round(c.severity, 3),
        "symptom": "dynamic_only" if c.difficulty == "hard" else "static",
        "original_metrics": orig_metrics,
        "faulted_metrics": c.metrics,
        "checks": [ch.to_dict() for ch in c.checks],
    }
    (d / "fault.json").write_text(json.dumps(fault, indent=2))


def run_circuit(circuit_dir: Path, out_root: Path, n: int, seed: int, jobs: int,
                factors: list[float]) -> None:
    spec = load_spec(circuit_dir / "spec.yaml")
    tb = circuit_dir / spec["testbench"]
    expected = [s["metric"] for s in spec["specs"]]
    clean = strip_comments((circuit_dir / "circuit.spi").read_text())

    print(f"\n=== {spec['circuit']}: checking the original")
    base = simulate(clean, tb, expected=expected)
    base_checks = check(base.metrics, spec)
    print(summary(base_checks))
    if not (base.ok and all_pass(base_checks)):
        sys.exit(f"original {spec['circuit']} does not pass its spec ({base.error}); refusing to inject faults")

    cands = build_candidates(clean, factors)
    n_sites = len({(c.site.device.name, c.site.param) for c in cands})
    print(f"{len(cands)} candidates over {n_sites} parameters; simulating with {jobs} workers")

    # Cache simulation results by (netlist, testbench) hash so re-labelling or
    # re-selecting does not need another full simulation pass.
    cache_dir = REPO_ROOT / ".cache" / "sim"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tb_text = tb.read_text()

    def run(c: Candidate) -> Candidate:
        key = hashlib.sha1((c.faulted_text + "\0" + tb_text).encode()).hexdigest()
        f = cache_dir / f"{key}.json"
        if f.exists():
            r = json.loads(f.read_text())
        else:
            res = simulate(c.faulted_text, tb, expected=expected)
            log = "\n".join(l for l in res.log.splitlines() if "Model issue on line" not in l)
            r = {"ok": res.ok, "error": res.error, "metrics": res.metrics, "log": log}
            f.write_text(json.dumps(r))
        c.sim_ok, c.sim_error, c.metrics, c.log = r["ok"], r["error"], r["metrics"], r["log"]
        c.checks = check(c.metrics, spec)
        return c

    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for _ in pool.map(run, cands):
            done += 1
            if done % 25 == 0 or done == len(cands):
                print(f"  {done}/{len(cands)}", flush=True)

    out_dir = out_root / spec["circuit"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "candidates.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["device", "model", "param", "old", "new", "factor", "status", "difficulty",
                    "severity", "clear_failures", "sim_error"])
        for c in cands:
            w.writerow([c.site.device.name, c.site.device.model or c.site.device.kind, c.site.param,
                        c.site.old_token, c.new_token, round(c.factor, 4), c.status,
                        c.difficulty if c.valid else "", round(c.severity, 3),
                        ";".join(ch.metric for ch in c.clear), c.sim_error or ""])

    valid, seen = [], set()
    for c in cands:
        if c.valid and (sig := signature(c)) not in seen:
            seen.add(sig)
            valid.append(c)
    counts = {s: sum(c.status == s for c in cands) for s in ("kept", "marginal", "silent", "sim_failed")}
    by_diff = {k: sum(c.difficulty == k for c in valid) for k in ("easy", "medium", "hard")}
    print(f"results: {counts}; after de-duplication {len(valid)} usable {by_diff}")

    chosen = select(valid, n, random.Random(seed))
    for old in out_dir.glob(f"{spec['circuit']}-param-*"):
        shutil.rmtree(old)
    for k, c in enumerate(chosen):
        inst = f"{spec['circuit']}-param-{k:03d}"
        write_instance(out_dir, inst, circuit_dir, spec, clean, c, base.metrics, seed)
        print(f"  {inst}: {c.site.device.name}.{c.site.param} {c.site.old_token} -> {c.new_token} "
              f"[{c.difficulty}] fails {', '.join(ch.metric for ch in c.clear)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--circuit", action="append", required=True, help="circuit folder (repeatable)")
    ap.add_argument("--out", default="dataset/parameter", help="output root")
    ap.add_argument("--n", type=int, default=15, help="instances to keep per circuit")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=8, help="parallel ngspice processes")
    ap.add_argument("--factors", type=float, nargs="+", default=DEFAULT_FACTORS)
    a = ap.parse_args(argv)
    for c in a.circuit:
        run_circuit(Path(c), Path(a.out), a.n, a.seed, a.jobs, a.factors)


if __name__ == "__main__":
    main()

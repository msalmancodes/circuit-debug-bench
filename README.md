# Circuit Debugging and Repair with LLM Agents

Benchmark of faulted analog netlists (SKY130, ngspice) for evaluating how well LLM agents
find and repair faults. Part 1 builds the dataset; Part 2 evaluates frontier models.

## Layout

```
circuits/<name>/          base circuits that pass their spec
  circuit.spi             reference netlist (verbatim from the source benchmark)
  testbench.spi           reduced testbench; prints METRIC lines
  spec.yaml               metrics, limits, clear-failure margins
  upstream/               the source task's instruction, results and full benches
faults/                   one fault-injection script per fault category
  parameter.py            parameter faults (W/L, R, C, multipliers)
pipeline/
  simulate.py             run ngspice on a netlist + testbench, collect metrics
  verify.py               check metrics against spec.yaml
  netlist.py              netlist parsing / editing / comment stripping
dataset/<category>/<circuit>/<instance>/
  original.spi            comment-stripped original (passes)
  faulted.spi             one fault injected (fails)
  fault.json              edits, inverse patch, difficulty, metrics before/after
  spec.yaml, testbench.spi, sim.log
  ../candidates.csv       every candidate tried, with its outcome
tests/                    pytest checks (originals pass, faults fail, faults reversible)
```

## Setup (macOS)

```bash
brew install ngspice                    # ngspice >= 42
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/volare enable --pdk sky130 -l sky130_fd_pr c6d73a35f524070e85faff4a6a9eef49553ebc2b
```

The SKY130 models land in `~/.volare` (about 200 MB). Set `PDK_ROOT` to use another location.
Run commands from the repo root so ngspice picks up `.spiceinit`.

## Generate parameter faults

```bash
.venv/bin/python -m faults.parameter --circuit circuits/bandgap_ref --circuit circuits/capless_ldo --n 15 --seed 0
```

For each circuit the script checks that the original passes, scales every editable value by
0.1x to 10x, simulates each candidate, keeps the ones that still simulate and fail a spec by
its margin, removes duplicates and picks a seeded, difficulty-balanced subset.

Difficulty labels: **easy** = static spec fails by more than 5x its margin; **medium** = static
spec fails but closer to the limit; **hard** = only dynamic (AC/transient) specs fail, so DC
behaviour looks normal.

## Tests

```bash
.venv/bin/python -m pytest              # fast checks + originals pass
RESIM=1 .venv/bin/python -m pytest      # also re-simulate every faulted netlist
```

## Circuits

| Circuit | Source | Spec subset |
|---|---|---|
| `bandgap_ref` | Analog Design Bench `sky130-bandgap-reference-pvt` | tt / 1.8 V / 27 C, temp sweep, line sweep, PSRR, startup |
| `capless_ldo` | Analog Design Bench `sky130-capless-ldo-1v0-20ma-pvt` | tt / 27 C: DC window, Iq, PSR, load step, startup |

Analog Design Bench content is CC BY-NC 4.0. Specs are reduced to the nominal corner to keep
simulations to about 10 s each; the upstream signoff covers full PVT and Monte Carlo.

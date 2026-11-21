"""Run an ngspice testbench on a netlist and collect METRIC lines.

Testbenches are templates containing two placeholders:
  __LIB__      -> path to the SKY130 ngspice model library
  __CIRCUIT__  -> path to the netlist under test

Each deck prints lines of the form ``METRIC <name> <value>``.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIB = Path(
    os.environ.get("PDK_ROOT", Path.home() / ".volare")
) / "sky130A/libs.tech/ngspice/sky130.lib.spice"

_METRIC_RE = re.compile(r"^METRIC\s+(\S+)\s+(\S+)", re.MULTILINE)


@dataclass
class SimResult:
    ok: bool                      # ngspice ran and produced every expected metric
    metrics: dict[str, float]
    log: str
    error: str | None = None
    missing: list[str] = field(default_factory=list)


def _parse_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return math.nan


def simulate(netlist_text: str, testbench_path: Path, *, lib: Path = DEFAULT_LIB,
             expected: list[str] | None = None, timeout: int = 300) -> SimResult:
    """Simulate ``netlist_text`` with the given testbench template.

    Runs in a fresh temp dir (no spaces in the path, which ngspice dislikes),
    with the repo's .spiceinit copied in.
    """
    if shutil.which("ngspice") is None:
        raise RuntimeError("ngspice not found on PATH (brew install ngspice)")
    if not lib.exists():
        raise RuntimeError(f"SKY130 model library not found: {lib} (see README: volare enable)")

    template = Path(testbench_path).read_text()
    with tempfile.TemporaryDirectory(prefix="cdb_") as tmp:
        tmp = Path(tmp)
        (tmp / "circuit.spi").write_text(netlist_text)
        shutil.copy(REPO_ROOT / ".spiceinit", tmp / ".spiceinit")
        deck = template.replace("__LIB__", str(lib)).replace("__CIRCUIT__", str(tmp / "circuit.spi"))
        (tmp / "tb.spi").write_text(deck)
        try:
            proc = subprocess.run(["ngspice", "-b", "tb.spi"], cwd=tmp, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return SimResult(False, {}, log, error=f"timeout after {timeout}s")

    log = proc.stdout + "\n" + proc.stderr
    metrics = {k: _parse_float(v) for k, v in _METRIC_RE.findall(proc.stdout)}
    missing = [m for m in (expected or []) if m not in metrics or math.isnan(metrics[m])]
    error = None
    if missing:
        error = "missing/NaN metrics: " + ", ".join(missing)
    if re.search(r"(?i)singular matrix|timestep too small|no convergence|fatal", log):
        error = (error + "; " if error else "") + "convergence error in log"
    return SimResult(error is None, metrics, log, error=error, missing=missing)

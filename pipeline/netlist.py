"""Minimal SPICE netlist helpers shared by the fault scripts.

Handles the flat subcircuit netlists used in this repo: one device per line,
`key=value` parameters on X-instances, and plain values on ideal R/C/L lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SUFFIX = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6,
           "n": 1e-9, "p": 1e-12, "f": 1e-15}
_NUM_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpf])?([a-z]*)$", re.I)


def parse_value(text: str) -> float | None:
    """'2.2k' -> 2200.0, '3p' -> 3e-12, '72' -> 72.0; None if not numeric."""
    m = _NUM_RE.match(text.strip())
    if not m:
        return None
    base = float(m.group(1))
    suf = (m.group(2) or "").lower()
    return base * _SUFFIX.get(suf, 1.0)


def format_value(x: float, like: str) -> str:
    """Format x in the same style as the original token (SI suffix or plain number)."""
    m = _NUM_RE.match(like.strip())
    suf = (m.group(2) or "") if m else ""
    if suf:
        return f"{x / _SUFFIX[suf.lower()]:.4g}{suf}"
    return f"{x:.4g}"


def strip_comments(text: str) -> str:
    """Remove full-line `*` comments, inline `;`/`$ ` comments and blank lines.

    Upstream reference netlists explain their design in comments, often quoting
    the original component values. Faulted netlists must not leak those.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("*"):
            continue
        s = re.split(r"\s;|\s\$\s", s)[0].rstrip()
        out.append(s)
    return "\n".join(out) + "\n"


@dataclass
class Device:
    line_no: int            # 0-based index into netlist lines
    name: str               # instance name, e.g. XM1, R1
    kind: str               # 'X', 'R', 'C', 'L', ...
    model: str | None       # subckt/model name for X instances
    nodes: list[str]
    params: dict[str, str]  # raw parameter tokens (X instances), or {'value': ...} for R/C/L
    line: str


def parse_devices(text: str) -> list[Device]:
    devices = []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not s or s.startswith(("*", ".", "+")):
            continue
        toks = s.split()
        name, kind = toks[0], toks[0][0].upper()
        if kind == "X":
            pos = [t for t in toks[1:] if "=" not in t]
            params = dict(t.split("=", 1) for t in toks[1:] if "=" in t)
            devices.append(Device(i, name, kind, pos[-1], pos[:-1], params, line))
        elif kind in "RCL" and len(toks) >= 4:
            devices.append(Device(i, name, kind, None, toks[1:3], {"value": toks[3]}, line))
        else:
            devices.append(Device(i, name, kind, None, toks[1:], {}, line))
    return devices


def set_param(line: str, param: str, new_token: str, *, insert: bool = False) -> str:
    """Return `line` with one parameter replaced (`param=...`, or the value of an R/C/L).

    With insert=True a missing `param=` is appended (e.g. adding `m=2` to an instance).
    """
    toks = line.split()
    if param == "value":
        toks[3] = new_token
    else:
        for j, t in enumerate(toks):
            if t.split("=", 1)[0] == param and "=" in t:
                toks[j] = f"{param}={new_token}"
                break
        else:
            if not insert:
                raise KeyError(f"{param} not on line: {line}")
            toks.append(f"{param}={new_token}")
    return " ".join(toks)

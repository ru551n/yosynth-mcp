"""Building yosys synthesis scripts and parsing yosys output.

A synthesis run is one yosys process: ``yosys -m <ghdl.so> -p '<script>'``.
The script always ends with the target flow (``synth[_<chip>] -top <top>``),
``stat`` and ``write_json`` so the result can be summarized from the netlist:

- VHDL top level:
  ``ghdl [--std=XX] [-g name=value ...] <files> -e <top> <arch>; <flow>; ...``
  (GHDL 7.0 takes the architecture as a second positional after ``-e``;
  generic names are matched case-insensitively, values verbatim.)
- Verilog top level:
  ``[ghdl -read <vhdl files>;] read_verilog [-sv] <verilog files>;
  [chparam -set name value <top> ...]; <flow>; ...``
(VHDL units imported with ``-read`` appear as ``<entity>_B<arch>``
   modules — yosys's escape of the qualified name ``entity.arch`` — and
   may be instantiated from the Verilog top; the other direction, a VHDL
   top instantiating Verilog, is not supported by the plugin.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

VHDL_EXTENSIONS = {".vhd", ".vhdl"}
VERILOG_EXTENSIONS = {".v", ".sv"}

# ---------------------------------------------------------------------------
# chip / flow model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChipSpec:
    """How one user-facing chip maps onto a yosys synthesis flow.

    ``families`` are the *known* architectures/device families of this yosys
    version (newer yosys may add more — the yosynth_targets tool reports
    what the installed binary actually provides).
    """

    flow: str
    description: str
    option: str | None = None  # "-family" / "-device" / "-tech", or None
    families: tuple[str, ...] = ()
    default_family: str | None = None
    family_required: bool = False


CHIPS: Mapping[str, ChipSpec] = {
    "generic": ChipSpec("synth", "vendor-independent synthesis (yosys 'synth')"),
    "ice40": ChipSpec(
        "synth_ice40",
        "Lattice iCE40 FPGAs (iCEstick, icezum, ...)",
        option="-device",
        families=("hx", "lp", "u"),
        default_family="hx",
    ),
    "ecp5": ChipSpec("synth_ecp5", "Efinix ECP5 FPGAs (Versa, ...)"),
    "gowin": ChipSpec("synth_gowin", "Gowin FPGAs"),
    "xilinx": ChipSpec(
        "synth_xilinx",
        "Xilinx/AMD FPGAs (7-series, UltraScale and older)",
        option="-family",
        families=(
            "xcup", "xcu", "xc7", "xc6v", "xc5v", "xc6s",
            "xc4v", "xc3sda", "xc3sa", "xc3se", "xc3s", "xc2vp", "xc2v",
            "xcve", "xcv",
        ),
        default_family="xc7",
    ),
    "sf2": ChipSpec("synth_sf2", "Lattice SmartFusion2 / IGLOO2"),
    "quicklogic": ChipSpec(
        "synth_quicklogic",
        "QuickLogic FPGAs",
        option="-family",
        families=("pp3", "qlf_k6n10f"),
        default_family="pp3",
    ),
    "efinix": ChipSpec("synth_efinix", "Efinix FPGAs"),
    "intel": ChipSpec(
        "synth_intel",
        "Intel/Altera FPGAs (legacy flow)",
        option="-family",
        families=("max10", "cyclone10lp", "cycloneiv", "cycloneive"),
        default_family="max10",
    ),
    "intel_alm": ChipSpec(
        "synth_intel_alm",
        "Intel/Altera ALM-based FPGAs",
        option="-family",
        families=("cyclonev",),
        default_family="cyclonev",
    ),
    "achronix": ChipSpec("synth_achronix", "Achronix Speedster22i"),
    "anlogic": ChipSpec("synth_anlogic", "Anlogic FPGAs"),
    "analogdevices": ChipSpec(
        "synth_analogdevices",
        "Analog Devices FPGAs",
        option="-tech",
        families=("t16ffc", "t40lp"),
        default_family="t16ffc",
    ),
    "lattice": ChipSpec(
        "synth_lattice",
        "Lattice FPGAs: Trellis (ecp5, xo2, xo3, xo3d) and Nexus (lifcl, lfd2nx)",
        option="-family",
        families=("ecp5", "xo2", "xo3", "xo3d", "lifcl", "lfd2nx"),
        family_required=True,
    ),
}


class SynthError(Exception):
    """The synthesis request is invalid; the message is user-actionable."""


def chip_spec(chip: str) -> ChipSpec:
    """Look up the chip spec; SynthError for unknown chips."""
    c = chip.strip().lower()
    try:
        return CHIPS[c]
    except KeyError:
        known = ", ".join(sorted(CHIPS))
        raise SynthError(f"unknown chip {chip!r}; supported: {known}") from None


def resolve_flow(chip: str, family: str | None) -> tuple[str, list[str]]:
    """Map (chip, family) to (yosys flow pass, extra flow args).

    Raises:
        SynthError: for unknown chips/families or a missing required family.
    """
    spec = chip_spec(chip)
    if spec.option is None:
        if family is not None:
            raise SynthError(
                f"chip {chip!r} does not take a family (architecture); "
                f"pass family only for chips listed by yosynth_targets"
            )
        return spec.flow, []
    if family is None:
        if spec.family_required:
            raise SynthError(
                f"chip {chip!r} needs a family: one of "
                f"{', '.join(spec.families)}"
            )
        family = spec.default_family
        assert family is not None
    if family not in spec.families:
        raise SynthError(
            f"unknown family {family!r} for chip {chip!r}; known families: "
            f"{', '.join(spec.families)} (newer yosys versions may support "
            "more — see yosynth_targets)"
        )
    return spec.flow, [spec.option, family]


@dataclass(frozen=True)
class PortInfo:
    name: str
    direction: str  # input | output | inout
    width: int


@dataclass
class ModuleStats:
    """Per-module counts from yosys's ``stat`` pass (local, excl. submodules)."""

    wires: int = 0
    wire_bits: int = 0
    ports: int = 0
    port_bits: int = 0
    memories: int = 0
    memory_bits: int = 0
    cells: int = 0
    cell_types: dict[str, int] = field(default_factory=dict)
    submodules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunOutcome:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


# ---------------------------------------------------------------------------
# script building
# ---------------------------------------------------------------------------


def _q(token: str) -> str:
    """Quote a token for a yosys -p script when it contains whitespace."""
    if token and not re.search(r"[\s\"]", token):
        return token
    return '"' + token.replace('"', '\\"') + '"'


def _param_value(value: str) -> str:
    """Quote a generic/parameter value that is not a plain integer."""
    if re.fullmatch(r"[+-]?[0-9]+", value):
        return value
    return _q(value)


def classify_sources(sources: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split source paths into (vhdl, verilog) by file extension."""
    vhdl: list[str] = []
    verilog: list[str] = []
    for src in sources:
        ext = Path(src).suffix.lower()
        if ext in VHDL_EXTENSIONS:
            vhdl.append(src)
        elif ext in VERILOG_EXTENSIONS:
            verilog.append(src)
        else:
            raise SynthError(
                f"unsupported file {src!r}: only .vhd/.vhdl and .v/.sv are "
                "accepted"
            )
    if not vhdl and not verilog:
        raise SynthError("no sources given")
    return vhdl, verilog


def detect_top_language(top: str, sources: Sequence[str]) -> str:
    """Decide whether ``top`` is a VHDL entity or a Verilog module.

    Scans the given sources for ``entity <top>`` / ``module <top>``.

    Raises:
        SynthError: when the top cannot be found, or in both languages.
    """
    vhdl_re = re.compile(rf"^\s*entity\s+{re.escape(top)}\b", re.I | re.M)
    verilog_re = re.compile(rf"^\s*module\s+{re.escape(top)}\b", re.I | re.M)
    found: set[str] = set()
    for src in sources:
        try:
            text = Path(src).read_text(errors="replace")
        except OSError as exc:
            raise SynthError(f"cannot read source {src!r}: {exc.strerror}") from exc
        if vhdl_re.search(text):
            found.add("vhdl")
        if verilog_re.search(text):
            found.add("verilog")
    if found == {"vhdl", "verilog"}:
        raise SynthError(
            f"top {top!r} is declared as both a VHDL entity and a Verilog "
            "module; pass sources that define it only once"
        )
    if found == {"vhdl"}:
        return "vhdl"
    if found == {"verilog"}:
        return "verilog"
    raise SynthError(
        f"could not find top {top!r} in the given sources (looked for "
        f"'entity {top}' and 'module {top}'); check the top name and that "
        "the file defining it is in `sources`"
    )


def build_script(
    top: str,
    sources: Sequence[str],
    architecture: str | None,
    chip: str,
    family: str | None,
    generics: Mapping[str, str],
    std: str | None,
    systemverilog: bool,
    json_path: str,
) -> str:
    """Build the yosys -p script for one synthesis run.

    Raises:
        SynthError: for invalid combinations (see module docstring).
    """
    if not top or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top):
        raise SynthError(f"invalid top level name {top!r}")
    vhdl, verilog = classify_sources(sources)
    language = detect_top_language(top, sources)
    flow, flow_args = resolve_flow(chip, family)
    steps: list[str] = []

    if language == "vhdl":
        if verilog:
            raise SynthError(
                f"VHDL top {top!r} cannot instantiate Verilog "
                f"({', '.join(map(str, verilog))}): GHDL synthesis "
                "elaborates VHDL only. Make the top level a Verilog module "
                "(it may then instantiate the VHDL units)."
            )
        if not architecture:
            raise SynthError(
                f"VHDL top {top!r} needs an architecture; pass "
                "architecture='rtl' (find candidates with e.g. "
                f"`grep -rn 'architecture .* of {top}'`)"
            )
        ghdl: list[str] = ["ghdl"]
        if std:
            # GHDL's CLI parser only accepts the joined "--std=CODE" form;
            # a space-separated "--std CODE" is rejected as an unknown
            # command option.
            ghdl += [f"--std={std}"]
        for name, value in generics.items():
            ghdl.append(f"-g{name.lower()}={_param_value(value)}")
        ghdl += [_q(s) for s in vhdl]
        ghdl += [f"-e {top} {architecture}"]
        steps.append(" ".join(ghdl))
    else:  # verilog top
        if architecture is not None:
            raise SynthError(
                f"architecture {architecture!r} does not apply: {top!r} is "
                "a Verilog top level (Verilog has no architectures)"
            )
        if vhdl:
            steps.append(" ".join(["ghdl", "-read", *(_q(s) for s in vhdl)]))
        readv = ["read_verilog"]
        if systemverilog:
            readv.append("-sv")
        steps.append(" ".join(readv + [_q(s) for s in verilog]))
        for name, value in generics.items():
            steps.append(
                " ".join(["chparam", "-set", _q(name), _param_value(value), top])
            )

    steps.append(" ".join([flow, *flow_args, "-top", top]))
    steps.append("stat")
    steps.append(f"write_json {_q(json_path)}")
    return "; ".join(steps)

# ---------------------------------------------------------------------------
# running yosys
# ---------------------------------------------------------------------------


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


async def run_yosys(
    config: Config,
    script: str,
    json_path: str,
    timeout: float | None = None,
) -> RunOutcome:
    """Run yosys with the ghdl plugin and the given -p script."""
    env = dict(os.environ)
    if config.ghdl_prefix:
        env["GHDL_PREFIX"] = config.ghdl_prefix
    argv = [config.yosys, "-m", str(config.plugin), "-p", script]
    limit = timeout if timeout is not None else config.timeout
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        await proc.wait()
        raise SynthError(
            f"yosys timed out after {limit:.0f}s (raise YOSYNTH_MCP_TIMEOUT "
            "or pass a larger `timeout`)"
        ) from None
    return RunOutcome(
        returncode=proc.returncode or 0,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
        elapsed=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# netlist parsing (from the flow's write_json output)
# ---------------------------------------------------------------------------


def load_netlist(json_text: str) -> dict[str, Any]:
    """Parse a write_json netlist."""
    data = json.loads(json_text)
    if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
        raise SynthError(
            "netlist JSON has no 'modules' section; synthesis likely failed "
            "before write_json ran"
        )
    return data


def top_module(netlist: dict[str, Any], top: str) -> dict[str, Any] | None:
    mod = netlist.get("modules", {}).get(top)
    return mod if isinstance(mod, dict) else None


def ports_of(netlist: dict[str, Any], top: str) -> list[PortInfo]:
    """Top module ports (name, direction, bit width) from the netlist."""
    mod = top_module(netlist, top)
    ports = (mod or {}).get("ports")
    if not isinstance(ports, dict):
        return []
    result: list[PortInfo] = []
    for name in sorted(ports):
        info = ports[name] or {}
        bits = info.get("bits") or []
        result.append(
            PortInfo(
                name=name,
                direction=info.get("direction", "unknown"),
                width=len(bits),
            )
        )
    return result


def parse_stat(output: str, top: str) -> ModuleStats | None:
    """Top module resource counts from yosys's ``stat`` pass.

    ``stat`` prints one ``=== <module> ===`` section per module; the last
    section for ``top`` is the post-flow one and holds the resource counts
    (cells by type, wire bits, memories, submodules). Returns None when the
    section is absent (e.g. the flow never ran).
    """
    headers = list(re.finditer(r"^===\s*(\S+)\s*===", output, re.M))
    section: str | None = None
    for i, m in enumerate(headers):
        if m.group(1) == top:
            end = headers[i + 1].start() if i + 1 < len(headers) else len(output)
            section = output[m.end() : end]
    if section is None:
        return None

    # Count lines: "<n> <lowercase label>". Entry lines: "<count><2+ spaces>
    # <name>" — labels never contain _/$, so the two are unambiguous.
    count_re = re.compile(r"^\s*(-|\d+)\s+([a-z ]+)\s*$")
    entry_re = re.compile(r"^\s*(\d+)\s{2,}(\S+)\s*$")
    stats = ModuleStats()
    mode: str | None = None
    for line in section.splitlines():
        count = count_re.match(line)
        if count:
            label = count.group(2)
            value = 0 if count.group(1) == "-" else int(count.group(1))
            if label == "wires":
                stats.wires = value
            elif label == "wire bits":
                stats.wire_bits = value
            elif label == "ports":
                stats.ports = value
            elif label == "port bits":
                stats.port_bits = value
            elif label == "memories":
                stats.memories = value
            elif label == "memory bits":
                stats.memory_bits = value
            elif label == "cells":
                stats.cells = value
                mode = "cells"
            elif label == "submodules":
                mode = "submodules" if value else None
            else:
                mode = None
            continue
        entry = entry_re.match(line)
        if entry and mode == "cells":
            stats.cell_types[entry.group(2)] = int(entry.group(1))
        elif entry and mode == "submodules":
            stats.submodules.append(entry.group(2))
    return stats


# ---------------------------------------------------------------------------
# error extraction
# ---------------------------------------------------------------------------

_ERROR_RE = re.compile(r"\bERROR\b|\berror\b|:\s*error:|^\s*WARNING\b|warning:")


def extract_errors(output: str) -> list[str]:
    """Error/warning lines of a yosys run, de-duplicated in order (max 10)."""
    seen: set[str] = set()
    lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if len(stripped) < 5 or not _ERROR_RE.search(stripped):
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        lines.append(stripped)
        if len(lines) == 10:
            break
    return lines


_HINTS: tuple[tuple[str, str], ...] = (
    (
        "no architecture",
        "pass architecture='...' (the VHDL architecture of the top entity); "
        "list candidates with `grep -rn 'architecture' <sources>`",
    ),
    (
        "not found in library",
        "a unit referenced by the design is missing from `sources` (or its "
        "library does not match)",
    ),
    (
        "no declaration for",
        "missing VHDL library clause (e.g. `use ieee.std_logic_1164.all;`)",
    ),
    (
        "is not part of the design",
        "a module is instantiated but was never imported; check the module "
        "name and that its file is in `sources`",
    ),
    (
        "no generic",
        "GHDL matches generic names case-insensitively; use the exact "
        "generic name from the entity declaration",
    ),
    (
        "bad unit name",
        "the -e unit syntax is `<top> <architecture>` (architecture as a "
        "separate word, no slash)",
    ),
)


def hint_for(output: str) -> str | None:
    lowered = output.lower()
    for needle, hint in _HINTS:
        if needle.lower() in lowered:
            return hint
    return None


# ---------------------------------------------------------------------------
# result rendering (short: relevant resources only)
# ---------------------------------------------------------------------------

_MAX_PORT_LINES = 12
_MAX_CELL_TYPES = 8


def _fmt_ports(ports: Sequence[PortInfo]) -> str:
    lines = [f"Ports ({len(ports)}):"]
    shown = ports[:_MAX_PORT_LINES]
    name_w = max((len(p.name) for p in shown), default=0)
    for p in shown:
        lines.append(f"  {p.name:<{name_w}}  {p.direction:<6}  {p.width} bit(s)")
    if len(ports) > len(shown):
        lines.append(f"  ... +{len(ports) - len(shown)} more")
    return "\n".join(lines)


def _fmt_cells(stats: ModuleStats) -> str:
    shown = sorted(stats.cell_types.items(), key=lambda kv: (-kv[1], kv[0]))
    top_types = ", ".join(f"{count} {name}" for name, count in shown[:_MAX_CELL_TYPES])
    extra = len(stats.cell_types) - _MAX_CELL_TYPES
    more = f" (+{extra} more types)" if extra > 0 else ""
    return f"cells: {stats.cells} - {top_types}{more}"


def build_success(
    top: str,
    chip: str,
    flow: str,
    stats: ModuleStats | None,
    ports: Sequence[PortInfo],
    elapsed: float,
) -> str:
    """Render the short success summary (relevant resources only)."""
    lines = [f"Synthesis OK: top `{top}` -> {chip} (flow: {flow}) in {elapsed:.1f}s."]
    if ports:
        lines.append("")
        lines.append(_fmt_ports(ports))
    if stats is not None:
        resources = [
            _fmt_cells(stats) if stats.cells else None,
            f"wire bits: {stats.wire_bits}" if stats.wire_bits else None,
            f"memories: {stats.memories}" if stats.memories else None,
            f"submodules: {', '.join(stats.submodules)}" if stats.submodules else None,
        ]
        rendered = [r for r in resources if r]
        if rendered:
            lines.append("")
            lines.append("Resources:")
            lines.extend(f"  {r}" for r in rendered)
    return "\n".join(lines)


def build_failure(returncode: int, output: str, elapsed: float) -> str:
    """Render the failure summary: the actual diagnostics + one hint."""
    lines = [f"Synthesis FAILED (yosys exit {returncode}, {elapsed:.1f}s)."]
    errors = extract_errors(output)
    if errors:
        lines.append("")
        lines.append("Diagnostics:")
        lines.extend(f"  {e}" for e in errors)
    hint = hint_for(output)
    if hint:
        lines.append(f"Hint: {hint}")
    lines.append("Adjust top / architecture / generics / chip / sources and retry.")
    return "\n".join(lines)

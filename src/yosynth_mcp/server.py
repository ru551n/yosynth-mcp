"""yosynth-mcp: MCP server for synthesizing VHDL and Verilog with GHDL + Yosys.

The server shells out to one ``yosys -m ghdl.so -p '<script>'`` process per
synthesis. The script selects the right frontend by top-level language
(VHDL: the ghdl frontend with ``-e <top> <architecture>``; Verilog:
``read_verilog`` + ``chparam``, optionally importing VHDL units via
``ghdl -read``), runs the chip flow, ``stat`` and ``write_json``, and the
server summarizes the relevant resources (ports, cells by type, wire bits,
memories, submodules).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .capabilities import Capabilities, render_targets
from .config import Config, ConfigError, load_config
from .inspect import inspect_sources, render_inspection
from .synth import (
    SynthError,
    build_failure,
    build_script,
    build_success,
    load_netlist,
    parse_stat,
    ports_of,
    resolve_flow,
    run_yosys,
)

mcp = FastMCP(
    "yosynth_mcp",
    instructions=(
        "Synthesize VHDL and Verilog designs with GHDL + Yosys and get a "
        "short netlist summary (ports + resources). Workflow: "
        "yosynth_inspect the sources to discover top levels, VHDL "
        "architectures and generics/parameters; yosynth_targets to see "
        "which chips/families this yosys supports; then "
        "yosynth_synthesize with top, chip (and family where needed), "
        "architecture (VHDL tops only) and generic overrides. Ask the "
        "user when the top, architecture, chip or generic value cannot "
        "be inferred."
    ),
)

_config: Config | None = None
_capabilities: Capabilities | None = None


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _get_capabilities(config: Config) -> Capabilities:
    global _capabilities
    if _capabilities is None or _capabilities.yosys != config.yosys:
        _capabilities = Capabilities(config.yosys)
    return _capabilities


def _err(exc: Exception) -> str:
    if isinstance(exc, ConfigError):
        return f"Configuration error: {exc}"
    return f"Error: {exc}"


class SynthesizeInput(BaseModel):
    """Input for yosynth_synthesize."""

    model_config = ConfigDict(str_strip_whitespace=True)

    sources: list[str] = Field(
        description=(
            "HDL source files (.vhd/.vhdl and/or .v/.sv) of the design, in "
            "any order. All units the top level needs must be covered by "
            "these files (GHDL has no work library state between runs)."
        ),
        min_length=1,
    )
    top: str = Field(
        description=(
            "Top level name: the VHDL entity or Verilog module that is "
            "synthesized (no library prefix)."
        ),
        min_length=1,
    )
    architecture: str | None = Field(
        default=None,
        description=(
            "VHDL architecture of the top entity (e.g. 'rtl'). REQUIRED "
            "when the top is a VHDL entity; MUST be omitted when the top "
            "is a Verilog module."
        ),
    )
    chip: str = Field(
        default="generic",
        description=(
            "Target chip / synthesis vendor: generic (vendor-independent, "
            "default), ice40, ecp5, gowin, xilinx, sf2, quicklogic, efinix, "
            "intel, intel_alm, achronix, anlogic, analogdevices, lattice. "
            "Use yosynth_targets to see which are available in the "
            "installed yosys."
        ),
    )
    family: str | None = Field(
        default=None,
        description=(
            "Device family / architecture of the chip, where the flow "
            "supports it (e.g. 'xc7' for xilinx, 'lifcl' for lattice, "
            "'hx' for ice40, 'pp3' for quicklogic). REQUIRED for "
            "lattice; optional with a default for the others. See "
            "yosynth_targets for the known values."
        ),
    )
    generics: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Generic (VHDL) / parameter (Verilog) overrides, name -> "
            "value, e.g. {'WIDTH': '8'}. VHDL generic names are matched "
            "case-insensitively by GHDL; Verilog parameter names are exact."
        ),
    )
    std: Literal["93", "08", "19"] | None = Field(
        default=None,
        description=(
            "VHDL standard for the ghdl frontend (default: GHDL's own "
            "default). Ignored for Verilog top levels."
        ),
    )
    systemverilog: bool = Field(
        default=True,
        description=(
            "Pass -sv to read_verilog (enables the small SystemVerilog "
            "subset; harmless for plain Verilog-2005)."
        ),
    )
    timeout: float | None = Field(
        default=None,
        ge=1,
        le=3600,
        description="Max seconds for this run (default: YOSYNTH_MCP_TIMEOUT).",
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
async def yosynth_synthesize(input: SynthesizeInput) -> str:
    """Synthesize a VHDL or Verilog design and return a short summary.

    Builds the yosys script for the given top level (detecting whether it
    is a VHDL entity or a Verilog module), runs the chip flow with
    generic/parameter overrides, and returns the top-level ports plus
    resource counts (cells by type, wire bits, memories, submodules) from
    the post-flow stat. On failure returns the diagnostics plus a hint."""
    try:
        config = _get_config()
        caps = _get_capabilities(config)
        await caps.ensure()
        flow, _flow_args = resolve_flow(input.chip, input.family)
        if not caps.has_flow(flow):
            return (
                f"Error: yosys ({caps.version}) does not provide the "
                f"'{flow}' flow for chip {input.chip!r}. Call yosynth_targets "
                "to see which chips are available, or install a yosys "
                "built with that techlib."
                + (f" (probe error: {caps.probe_error})" if caps.probe_error else "")
            )
        json_dir = tempfile.mkdtemp(prefix="yosynth-")
        json_path = str(Path(json_dir) / "netlist.json")
        netlist_text: str | None = None
        try:
            script = build_script(
                top=input.top,
                sources=input.sources,
                architecture=input.architecture,
                chip=input.chip,
                family=input.family,
                generics=input.generics,
                std=input.std,
                systemverilog=input.systemverilog,
                json_path=json_path,
            )
            outcome = await run_yosys(
                config, script, json_path, timeout=input.timeout
            )
            if outcome.returncode == 0:
                # Read the netlist before the tempdir is cleaned up below.
                netlist_text = Path(json_path).read_text()
        finally:
            shutil.rmtree(json_dir, ignore_errors=True)
    except (ConfigError, SynthError) as exc:
        return f"Error: {exc}"

    # yosys logs resource stats to stdout but GHDL/plugin errors to stderr;
    # combine both so the summary and the diagnostics are complete.
    output = outcome.stdout + outcome.stderr

    if outcome.returncode == 0 and netlist_text is not None:
        try:
            netlist = load_netlist(netlist_text)
        except (ValueError, SynthError):
            netlist = None
        if netlist is not None and input.top in netlist.get("modules", {}):
            stats = parse_stat(output, input.top)
            return build_success(
                top=input.top,
                chip=input.chip,
                flow=flow,
                stats=stats,
                ports=ports_of(netlist, input.top),
                elapsed=outcome.elapsed,
            )
    return build_failure(outcome.returncode, output, outcome.elapsed)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def yosynth_status() -> str:
    """Report the synthesis setup: yosys version, available synthesis
    flows, ghdl plugin path, GHDL library prefix and default timeout.
    Call this first when a synthesis fails with configuration-looking
    errors."""
    try:
        config = _get_config()
    except ConfigError as exc:
        return _err(exc)
    caps = _get_capabilities(config)
    await caps.ensure()
    lines = [
        f"yosynth-mcp {__version__}",
        f"yosys: {config.yosys} ({caps.version})",
        f"synthesis flows: {', '.join(sorted(caps.flows)) or '(none found)'}",
        f"ghdl plugin: {config.plugin}"
        + (" (exists)" if config.plugin.is_file() else " (MISSING)"),
        f"GHDL_PREFIX: {config.ghdl_prefix or 'unset (yosys inherits environment)'}",
        f"default timeout: {config.timeout:.0f}s",
    ]
    if caps.probe_error:
        lines.append(f"warning: {caps.probe_error}")
    return "\n".join(lines)


class InspectInput(BaseModel):
    """Input for yosynth_inspect."""

    model_config = ConfigDict(str_strip_whitespace=True)

    sources: list[str] = Field(
        description=(
            "HDL source files (.vhd/.vhdl and/or .v/.sv) to scan. The "
            "files only need to be readable; nothing is compiled."
        ),
        min_length=1,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def yosynth_inspect(input: InspectInput) -> str:
    """List the synthesizable units in the given sources: VHDL entities
    with their architectures and generics (name, type, default), Verilog
    modules with their parameters (name, default). Use this before
    yosynth_synthesize to pick the top level and to find out which
    architecture or generic values exist; if there are several
    architectures for the top entity or no obvious chip/family, ask the
    user."""
    try:
        _get_config()
    except ConfigError as exc:
        return _err(exc)
    return render_inspection(inspect_sources(input.sources))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def yosynth_targets() -> str:
    """List the chip targets this server can synthesize for and the
    device families/architectures each flow knows: the yosys
    synthesis flow (synth, synth_ice40, ...) of each chip, whether that
    flow exists in the installed yosys, and the known family values
    (e.g. xilinx: xc7, xcup, ...; lattice: lifcl, ecp5, ...; ice40:
    hx/lp/u). REQUIRED for 'lattice'; optional with a default for the
    others. Use it to answer 'which chip/family can I synthesize for?'
    before asking the user."""
    try:
        config = _get_config()
    except ConfigError as exc:
        return _err(exc)
    caps = _get_capabilities(config)
    await caps.ensure()
    if caps.probe_error:
        return f"Error: {caps.probe_error}"
    return render_targets(caps)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

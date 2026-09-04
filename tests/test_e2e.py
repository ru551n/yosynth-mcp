"""End-to-end tests: real yosys + ghdl plugin + GHDL std/ieee libraries.

Skipped (not failed) on machines without the full synthesis setup — see
the ``e2e`` fixture in conftest. The designs under tests/designs cover all
five frontend shapes:

- VHDL top (counter), with and without generic overrides
- Verilog top (vtop) instantiating a VHDL unit (vsub) — mixed language
- VHDL top (wrapper) instantiating a Verilog unit (vand) — mixed language,
  the other direction
- SystemVerilog-only top (svtop) with -sv
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import yosynth_mcp.server as server
from yosynth_mcp.synth import SynthError, build_script

DESIGNS = Path(__file__).parent / "designs"
COUNTER = str(DESIGNS / "counter.vhd")
VSUB = str(DESIGNS / "vsub.vhd")
VTOP = str(DESIGNS / "vtop.v")
SVTOP = str(DESIGNS / "svtop.sv")
WRAPPER = str(DESIGNS / "wrapper.vhd")
VAND = str(DESIGNS / "vand.v")


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setattr(server, "_config", None)
    monkeypatch.setattr(server, "_capabilities", None)


@pytest.fixture(autouse=True)
def _inject_test_env(monkeypatch, config_env):
    """The server reads os.environ; point it at the test yosys/plugin/libs."""
    for key in (
        "YOSYNTH_MCP_YOSYS",
        "YOSYNTH_MCP_GHDL_PLUGIN",
        "YOSYNTH_MCP_GHDL_PREFIX",
        "YOSYNTH_MCP_TIMEOUT",
    ):
        if key in config_env:
            monkeypatch.setenv(key, config_env[key])


async def test_vhdl_top_generic_override(e2e):
    """VHDL top with a generic override: outc must come out 8 bits wide."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[COUNTER],
            top="counter",
            architecture="rtl",
            chip="generic",
            generics={"WIDTH": "8"},
        )
    )
    assert result.startswith("Synthesis OK"), result
    assert "outc  output  8 bit(s)" in result
    assert "cells:" in result
    assert "Resources:" in result


async def test_vhdl_top_default_generic(e2e):
    """VHDL top without overrides: outc keeps its 4-bit default width."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[COUNTER],
            top="counter",
            architecture="rtl",
            chip="ice40",
        )
    )
    assert result.startswith("Synthesis OK"), result
    assert "ice40" in result
    assert re.search(r"outc\s+output\s+4 bit\(s\)", result)


async def test_vhdl_top_with_std(e2e):
    """std is forwarded as --std=<code>; GHDL rejects the split form."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[COUNTER],
            top="counter",
            architecture="rtl",
            chip="generic",
            std="08",
        )
    )
    assert result.startswith("Synthesis OK"), result


async def test_vhdl_top_requires_architecture(e2e):
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(sources=[COUNTER], top="counter", chip="generic")
    )
    assert result.startswith("Error:")
    assert "needs an architecture" in result


async def test_vhdl_top_with_verilog_submodule(e2e):
    """Mixed language: VHDL top, Verilog submodule (the other direction)."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[WRAPPER, VAND],
            top="wrapper",
            architecture="rtl",
            chip="generic",
        )
    )
    assert result.startswith("Synthesis OK"), result
    assert re.search(r"y\s+output\s+1 bit\(s\)", result)
    assert re.search(r"a\s+input\s+1 bit\(s\)", result)


async def test_verilog_top_with_vhdl_submodule(e2e):
    """Mixed language: Verilog top, VHDL submodule imported via -read."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[VSUB, VTOP],
            top="vtop",
            chip="generic",
            generics={"WIDTH": "4"},
        )
    )
    assert result.startswith("Synthesis OK"), result
    # vsub_rtl is imported from VHDL and instantiated by the Verilog top;
    # yosys names the module <entity>_B<arch> (escape of "vsub.rtl").
    assert "vsub_Brtl" in result
    # Column widths depend on the longest port name, so match flexibly.
    assert re.search(r"y\s+output\s+1 bit\(s\)", result)
    assert re.search(r"d\s+input\s+4 bit\(s\)", result)


async def test_systemverilog_top(e2e):
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(sources=[SVTOP], top="svtop", chip="generic")
    )
    assert result.startswith("Synthesis OK"), result
    assert re.search(r"clk\s+input\s+1 bit\(s\)", result)
    assert re.search(r"d\s+input\s+3 bit\(s\)", result)
    assert re.search(r"y\s+output\s+1 bit\(s\)", result)


async def test_unknown_chip_rejected(e2e):
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[COUNTER], top="counter", architecture="rtl", chip="fpga"
        )
    )
    assert result.startswith("Error:")
    assert "unknown chip" in result


async def test_synthesis_failure_reports_diagnostics(e2e):
    """A bad architecture name must surface as FAILED with diagnostics."""
    result = await server.yosynth_synthesize(
        server.SynthesizeInput(
            sources=[COUNTER], top="counter", architecture="bogus", chip="generic"
        )
    )
    assert result.startswith("Synthesis FAILED"), result
    assert "Diagnostics:" in result


async def test_status_tool(e2e):
    result = await server.yosynth_status()
    assert "yosynth-mcp" in result
    assert "(exists)" in result
    assert "synthesis flows:" in result


async def test_targets_tool(e2e):
    result = await server.yosynth_targets()
    assert "Yosys" in result
    assert "synth_ice40" in result
    assert "generic" in result
    assert "families" in result  # xilinx/ice40/etc. list known families


async def test_inspect_tool(e2e):
    result = await server.yosynth_inspect(
        server.InspectInput(sources=[COUNTER, VTOP])
    )
    assert "VHDL entity counter" in result
    assert "architectures: rtl" in result
    assert "generic: WIDTH" in result
    assert "Verilog module vtop" in result
    assert "parameter: WIDTH = 4" in result


async def test_script_uses_stat_and_write_json(e2e):
    """The flow always ends with stat (resources) and write_json (netlist)."""
    script = build_script(
        top="counter",
        sources=[COUNTER],
        architecture="rtl",
        chip="generic",
        family=None,
        generics={},
        std=None,
        systemverilog=True,
        json_path="/tmp/out.json",
    )
    assert script.endswith("; stat; write_json /tmp/out.json")
    # lattice requires a family: family=None must be rejected.
    with pytest.raises(SynthError):
        build_script(
            top="counter",
            sources=[COUNTER],
            architecture="rtl",
            chip="lattice",
            family=None,
            generics={},
            std=None,
            systemverilog=True,
            json_path="/tmp/out.json",
        )

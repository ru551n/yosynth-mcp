"""Unit tests for stat/netlist parsing, error extraction and rendering."""

from __future__ import annotations

from yosynth_mcp.synth import (
    ModuleStats,
    build_failure,
    build_success,
    extract_errors,
    hint_for,
    parse_stat,
    ports_of,
)

# Two `=== gen ===` sections (pre-flow and post-flow) as yosys prints them;
# the post-flow counts are the ones that matter.
STAT_OUTPUT = """\
=== gen ===

        +----------Local Count, excluding submodules.
        |
       3 wires
       5 wire bits
       2 ports
       5 port bits
       - memories
       - memory bits
       - processes
       0 cells

=== gen ===

        +----------Local Count, excluding submodules.
        |
       7 wires
      19 wire bits
       3 public wires
       9 public wire bits
       2 ports
       5 port bits
       - memories
       - memory bits
       - processes
      10 cells
       1   $_AND_
       4   $_DFF_P_
       1   $_NAND_
       1   $_NOT_
       1   $_XNOR_
       2   $_XOR_

=== vsub_rtl ===

        +----------Local Count, excluding submodules.
        |
       1 wires
       1 wire bits
       3 ports
       3 port bits
       - memories
       - memory bits
       - processes
       1 cells
       1   $_AND_
"""

NETLIST = {
    "modules": {
        "vtop": {
            "ports": {
                "clk": {"direction": "input", "bits": [0]},
                "d": {"direction": "input", "bits": [0, 1, 2, 3]},
                "y": {"direction": "output", "bits": [0]},
            }
        }
    }
}


class TestParseStat:
    def test_picks_last_section(self):
        stats = parse_stat(STAT_OUTPUT, "gen")
        assert stats is not None
        assert stats.wires == 7
        assert stats.wire_bits == 19
        assert stats.ports == 2
        assert stats.port_bits == 5
        assert stats.memories == 0
        assert stats.cells == 10

    def test_cell_types(self):
        stats = parse_stat(STAT_OUTPUT, "gen")
        assert stats is not None
        assert stats.cell_types == {
            "$_AND_": 1,
            "$_DFF_P_": 4,
            "$_NAND_": 1,
            "$_NOT_": 1,
            "$_XNOR_": 1,
            "$_XOR_": 2,
        }

    def test_other_module_section(self):
        stats = parse_stat(STAT_OUTPUT, "vsub_rtl")
        assert stats is not None
        assert stats.cells == 1
        assert stats.cell_types == {"$_AND_": 1}

    def test_missing_section(self):
        assert parse_stat(STAT_OUTPUT, "absent") is None

    def test_submodules(self):
        output = (
            "=== top ===\n\n      2 ports\n       2 port bits\n"
            "       3 cells\n       2   $_AND_\n       1 submodules\n"
            "       1   vsub_rtl\n"
        )
        stats = parse_stat(output, "top")
        assert stats is not None
        assert stats.submodules == ["vsub_rtl"]

    def test_zero_submodules(self):
        output = (
            "=== top ===\n\n       1 cells\n       1   $_AND_\n       - submodules\n"
        )
        stats = parse_stat(output, "top")
        assert stats is not None
        assert stats.submodules == []


class TestPorts:
    def test_ports_of(self):
        ports = ports_of(NETLIST, "vtop")
        assert [p.name for p in ports] == ["clk", "d", "y"]
        assert ports[0].direction == "input"
        assert ports[0].width == 1
        assert ports[1].width == 4
        assert ports[2].direction == "output"

    def test_missing_module(self):
        assert ports_of(NETLIST, "nope") == []


class TestErrors:
    def test_extract(self):
        output = (
            "ERROR: vhdl import failed.\n"
            "ghdl:warning: ieee library directory 'x' not found\n"
            "ghdl:error: cannot find \"std\" library\n"
            "ghdl:error: cannot find \"std\" library\n"
            "-- Running command `synth'\n"
        )
        errors = extract_errors(output)
        assert errors[0] == "ERROR: vhdl import failed."
        assert any('cannot find "std" library' in e for e in errors)
        assert len(errors) == 3  # de-duplicated

    def test_hint_no_architecture(self):
        assert (
            hint_for('error: entity "leds" has no architecture in library "work"')
            is not None
        )

    def test_hint_not_found(self):
        assert hint_for('top.vhd:9:19:error: unit "sub" not found in library "work"')

    def test_hint_none(self):
        assert hint_for("all good") is None


class TestRender:
    def test_success(self):
        stats = ModuleStats(
            wires=7,
            wire_bits=19,
            cells=10,
            cell_types={"$_DFF_P_": 4, "$_AND_": 6},
            memories=1,
            submodules=["vsub_rtl"],
        )
        text = build_success(
            top="gen",
            chip="ice40",
            flow="synth_ice40",
            stats=stats,
            ports=[],
            elapsed=1.25,
        )
        assert text.startswith(
            "Synthesis OK: top `gen` -> ice40 (flow: synth_ice40) in 1.2s."
        )
        assert "Resources:" in text
        assert "cells: 10" in text
        assert "4 $_DFF_P_" in text
        assert "wire bits: 19" in text
        assert "memories: 1" in text
        assert "submodules: vsub_rtl" in text
        assert "Ports" not in text  # no ports -> no port section

    def test_success_with_many_ports_truncates(self):
        ports = [
            type("P", (), {"name": f"p{i}", "direction": "input", "width": 1})
            for i in range(20)
        ]
        text = build_success("t", "generic", "synth", None, ports, 0.1)
        assert "... +8 more" in text

    def test_failure(self):
        text = build_failure(
            1,
            (
                'ERROR: vhdl import failed.\n'
                'error: entity "leds" has no architecture in library "work"'
            ),
            0.4,
        )
        assert text.startswith("Synthesis FAILED (yosys exit 1,")
        assert "ERROR: vhdl import failed." in text
        assert "Hint:" in text
        assert "Adjust" in text

"""Unit tests for build_script / flow / source classification (no yosys)."""

from __future__ import annotations

from pathlib import Path

import pytest

from yosynth_mcp.synth import (
    SynthError,
    build_script,
    classify_sources,
    resolve_flow,
)

DESIGNS = Path(__file__).parent / "designs"
COUNTER = str(DESIGNS / "counter.vhd")
VSUB = str(DESIGNS / "vsub.vhd")
VTOP = str(DESIGNS / "vtop.v")


def _script(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "top": "counter",
        "sources": [COUNTER],
        "architecture": "rtl",
        "chip": "generic",
        "family": None,
        "generics": {},
        "std": None,
        "systemverilog": True,
        "json_path": "/tmp/out.json",
    }
    kwargs.update(overrides)
    return build_script(**kwargs)  # type: ignore[arg-type]


class TestFlow:
    def test_generic_flow(self):
        assert resolve_flow("generic", None) == ("synth", [])

    def test_chip_flows(self):
        assert resolve_flow("ice40", None) == ("synth_ice40", ["-device", "hx"])
        assert resolve_flow("ecp5", None) == ("synth_ecp5", [])

    def test_ice40_device(self):
        assert resolve_flow("ice40", "hx") == ("synth_ice40", ["-device", "hx"])
        with pytest.raises(SynthError, match="unknown family"):
            resolve_flow("ice40", "zzz")

    def test_xilinx_default_family(self):
        assert resolve_flow("xilinx", None) == ("synth_xilinx", ["-family", "xc7"])
        assert resolve_flow("xilinx", "xcup") == ("synth_xilinx", ["-family", "xcup"])
        with pytest.raises(SynthError, match="unknown family"):
            resolve_flow("xilinx", "nope")

    def test_lattice_requires_family(self):
        assert resolve_flow("lattice", "lifcl") == (
            "synth_lattice",
            ["-family", "lifcl"],
        )
        with pytest.raises(SynthError, match="needs a family"):
            resolve_flow("lattice", None)
        with pytest.raises(SynthError, match="unknown family"):
            resolve_flow("lattice", "nexus")

    def test_family_rejected_for_familyless_chip(self):
        with pytest.raises(SynthError, match="does not take a family"):
            resolve_flow("generic", "xc7")

    def test_unknown_chip_lists_support(self):
        with pytest.raises(SynthError, match=r"unknown chip 'fpga'") as excinfo:
            resolve_flow("fpga", None)
        assert "ice40" in str(excinfo.value)


class TestClassify:
    def test_split(self):
        vhdl, verilog = classify_sources([COUNTER, VTOP, VSUB])
        assert vhdl == [COUNTER, VSUB]
        assert verilog == [VTOP]

    def test_unsupported_extension(self):
        with pytest.raises(SynthError, match="unsupported file"):
            classify_sources(["foo.txt"])

    def test_no_sources(self):
        with pytest.raises(SynthError, match="no sources"):
            classify_sources([])


class TestBuildScript:
    def test_vhdl_top_script(self):
        script = _script(chip="ice40")
        assert script == (
            "ghdl " + COUNTER + " -e counter rtl; "
            "synth_ice40 -device hx -top counter; stat; write_json /tmp/out.json"
        )

    def test_chip_family_in_script(self):
        script = _script(chip="xilinx", family="xcup")
        assert "synth_xilinx -family xcup -top counter" in script

    def test_vhdl_top_with_std_and_generics(self):
        script = _script(generics={"WIDTH": "8"}, std="08")
        assert script.startswith(
            "ghdl --std=08 -gwidth=8 " + COUNTER + " -e counter rtl; "
        )
        assert script.endswith("; stat; write_json /tmp/out.json")

    def test_vhdl_top_requires_architecture(self):
        with pytest.raises(SynthError, match="needs an architecture"):
            _script(architecture=None)

    def test_vhdl_top_rejects_verilog_sources(self):
        with pytest.raises(SynthError, match="cannot instantiate Verilog"):
            _script(sources=[COUNTER, VTOP])

    def test_verilog_top_with_vhdl_submodule(self):
        script = _script(
            top="vtop",
            sources=[VSUB, VTOP],
            architecture=None,
            systemverilog=False,
        )
        assert script == (
            "ghdl -read " + VSUB + "; read_verilog " + VTOP + "; "
            "synth -top vtop; stat; write_json /tmp/out.json"
        )

    def test_verilog_top_rejects_architecture(self):
        with pytest.raises(SynthError, match="does not apply"):
            _script(top="vtop", sources=[VTOP])

    def test_verilog_top_generics_via_chparam(self):
        script = _script(
            top="vtop",
            sources=[VTOP],
            architecture=None,
            generics={"WIDTH": "8"},
        )
        assert "chparam -set WIDTH 8 vtop" in script

    def test_systemverilog_flag(self):
        script = _script(
            top="svtop", sources=[str(DESIGNS / "svtop.sv")], architecture=None
        )
        assert script.startswith("read_verilog -sv ")

    def test_missing_top_rejected(self):
        with pytest.raises(SynthError, match="could not find top"):
            _script(top="nope")

    def test_path_with_space_is_quoted(self):
        with_space = str(DESIGNS / "counter copy.vhd")
        Path(with_space).write_text(Path(COUNTER).read_text())
        try:
            script = _script(sources=[with_space])
            assert f'"{with_space}"' in script
        finally:
            Path(with_space).unlink()

    def test_invalid_top_name(self):
        with pytest.raises(SynthError, match="invalid top"):
            _script(top="9bad")

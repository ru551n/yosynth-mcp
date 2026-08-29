---
name: yosynth-mcp
description: Synthesize VHDL/Verilog designs and check resource usage through the yosynth-mcp MCP server (yosynth_status, yosynth_inspect, yosynth_targets, yosynth_synthesize). Use when the user asks to synthesize an entity or module, check LUT/FF/gate/resource counts, see netlist ports, or find out which chips/families the installed yosys can target; the server runs yosys with the GHDL plugin per synthesis and returns a short ports+resources summary, and never guesses top, architecture, chip or generic values — it asks the user.
---

# Yosynth MCP

## Overview
Use this skill whenever the user asks to **synthesize** or **check the
resources of** a VHDL or Verilog design with the `yosynth-mcp` MCP server:
"synthesize this entity", "how many LUTs/FFs does this use", "does this
design build for ice40/xilinx/...", "what are the ports of the netlist",
"which chips can this yosys target". The server runs one
`yosys -m ghdl.so -p '<script>'` process per synthesis (GHDL frontend for
VHDL, `read_verilog` for Verilog/SystemVerilog) and returns a **short
summary of the relevant resources**: top-level ports plus post-flow
resource counts (cells by type, wire bits, memories, submodules).

Triggers on: synthesize, synthesis, netlist, resource usage, LUT/FF count,
gate count, synthesize for <chip>, build the FPGA netlist, yosys,
does this design elaborate and synthesize.

If a synthesis fails with a configuration-looking error (missing plugin,
missing GHDL libraries, no flows found), call `yosynth_status` first — it
reports yosys version, available flows, plugin path, `GHDL_PREFIX` and
timeout.

## Hard rule: never infer — ask
The server does not guess anything, and neither should you. Before calling
`yosynth_synthesize`, every one of these must be **known** (stated by the
user, or unambiguously discoverable from `yosynth_inspect` output):

| Parameter | Known when | Otherwise |
| --- | --- | --- |
| `top` | the user named it, or `yosynth_inspect` shows exactly one synthesizable candidate | **ask the user** which top level to synthesize (list the candidates) |
| `architecture` (VHDL tops only) | the user named it, or the top entity has exactly one architecture | **ask the user** which architecture (list them) |
| `chip` / `family` | the user named the target | **ask the user** which chip to target (list the ones `yosynth_targets` reports; mention `generic` = vendor-independent RTL check) |
| `generics` / parameter values | the user gave values, or the top has no generics/parameters | **ask the user**: list each generic/parameter with its type and default, and ask for values or "use defaults" |

"Ambiguous" is defined by the inspection's own `Notes:` section (multiple
architectures, a unit declared in both languages, ...) — whenever a note
points at a choice, that choice is a question to the user, not a guess.
Do not pick a "likely" top, a "default" architecture, a "reasonable" chip,
or default generic values silently. The cost of one question is far less
than synthesizing the wrong thing.

## Tools

| Tool | What it does | Cost |
| --- | --- | --- |
| `yosynth_status` | Server config: yosys version, available synthesis flows, ghdl plugin path (exists/missing), `GHDL_PREFIX`, timeout. Diagnose configuration problems here. | free |
| `yosynth_inspect` | Static scan of the given sources: VHDL entities with architectures + generics (name, type, default), Verilog modules with parameters (name, default), plus a `Notes:` list of ambiguities (multiple architectures, both-language units, ...) and per-file read errors. Nothing is compiled. | free |
| `yosynth_targets` | Chip targets this server can synthesize for: per chip — the yosys flow (`synth`, `synth_ice40`, ...), whether that flow exists in the installed yosys, and the known device families (e.g. xilinx: `xc7`, `xcup`, ...; lattice: `lifcl`, `ecp5`, ...; ice40: `hx`/`lp`/`u`). Use before asking the user which chip/family to target. | free |
| `yosynth_synthesize` | Runs the synthesis and returns the short summary (ports + resources) or `Synthesis FAILED` with diagnostics + a hint. | one yosys run |

## `yosynth_synthesize` inputs
- `sources` — HDL files (`.vhd`/`.vhdl` and/or `.v`/`.sv`) of the design,
  any order. **All units the top needs must be covered by these files** —
  GHDL has no work-library state between runs, so a missing file means
  "not found in library" at synthesis time.
- `top` — the VHDL entity or Verilog module that is synthesized (no
  library prefix).
- `architecture` — the VHDL architecture of the top entity (e.g. `rtl`).
  Required for VHDL tops, must be omitted for Verilog tops (the server
  detects the top's language from the sources).
- `chip` — `generic` (vendor-independent, the server default), `ice40`,
  `ecp5`, `gowin`, `xilinx`, `sf2`, `quicklogic`, `efinix`, `intel`,
  `intel_alm`, `achronix`, `anlogic`, `analogdevices`, `lattice`. Use
  `yosynth_targets` for the list this yosys actually provides.
- `family` — device family for the chip where the flow supports it (e.g.
  `xc7` for xilinx, `lifcl` for lattice, `hx` for ice40). **Required for
  `lattice`**; optional with a default for the others.
- `generics` — generic (VHDL) / parameter (Verilog) overrides, name →
  value, e.g. `{"WIDTH": "8"}`. VHDL names are matched case-insensitively
  by GHDL; Verilog parameter names are exact.
- `std` — VHDL standard for the ghdl frontend (`"93"`, `"08"`, `"19"`;
  default: GHDL's own). Ignored for Verilog tops.
- `systemverilog` — pass `-sv` to `read_verilog` (default true; enables
  the small SystemVerilog subset, harmless for plain Verilog-2005).
- `timeout` — max seconds for this run (default `YOSYNTH_MCP_TIMEOUT`).

## Output shape

Success (relevant resources only — no raw yosys dump):
```
Synthesis OK: top `counter` -> generic (flow: synth) in 0.4s.

Ports (3):
  clk   input   1 bit(s)
  rst   input   1 bit(s)
  outc  output  4 bit(s)

Resources:
  cells: 19 - 4 $_DFF_NN0_, 4 $_NOT_, 4 $_AND_, ...
  wire bits: 9
  submodules: half_adder
```
More than 12 ports or 8 cell types are collapsed (`... +N more`).

Failure:
```
Synthesis FAILED (yosys exit 1, 0.2s).

Diagnostics:
  ERROR: ...

Hint: a unit referenced by the design is missing from `sources` ...
Adjust top / architecture / generics / chip / sources and retry.
```
The `Hint:` is a pointer at the most common cause; read the Diagnostics
first.

## Workflows (user request → tool calls)

**"Synthesize <design> [for <chip>]"**
1. `yosynth_inspect(sources=...)` — see the units, architectures,
   generics/parameters, and the `Notes:` ambiguities.
2. Resolve the unknowns **by asking the user**, per the hard rule above:
   top, architecture (VHDL tops), chip/family (`yosynth_targets` for the
   candidate list), and generic/parameter values (with their defaults).
   Only proceed once every parameter is known.
3. `yosynth_synthesize(sources, top, architecture?, chip, family?,
   generics?)`. Report the ports + resources summary; offer to change a
   generic/parameter or retarget another chip.

**"Does this design build?" / "any errors?"**
→ Same workflow; the answer is the FAILED diagnostics or the OK summary.
Use `chip="generic"` only when the user accepts a vendor-independent
check (say so).

**"What chips/families can I synthesize for?"**
→ `yosynth_targets`. Availability is probed from the installed yosys at
runtime — never assume a flow exists just because a chip is listed.

**"What are the ports / resource usage of <top>? (already known params)"**
→ Skip straight to `yosynth_synthesize` with the parameters the user gave.

**Mixed-language design (Verilog top + VHDL units, or vice versa)**
- A **Verilog top may instantiate VHDL units**: pass both files in
  `sources`; the server imports the VHDL units with `ghdl -read`. In the
  netlist (and the `submodules:` line) a VHDL unit appears as
  `<entity>_B<arch>` — yosys's escape of the qualified name
  `entity.arch` (e.g. entity `vsub`, arch `rtl` → `vsub_Brtl`).
- A **VHDL top cannot instantiate Verilog** (the plugin does not support
  it); the server rejects such a design with an explanatory error.

**Synthesis failed**
→ Read the `Diagnostics:` (the actual yosys/GHDL errors) and the `Hint:`.
  Typical fixes: add the missing file to `sources`, use the exact
  `architecture` name, fix a `library`/`use` clause, or name a generic
  correctly (VHDL is case-insensitive, Verilog is exact). If the error
  looks like a server configuration problem (plugin missing, no flows),
  call `yosynth_status`.

**Anything looks wrong (config, version, missing flows)**
→ `yosynth_status`, fix the environment (see below), retry.

## Rules of thumb
- **Inspect first, synthesize second.** `yosynth_inspect` is free and is
  the source of truth for top/architecture/generic candidates; its
  `Notes:` section is a checklist of questions for the user.
- **Never synthesize with a guessed top, architecture, chip or generic
  value.** If you are not certain, one question is cheaper than a wrong
  netlist.
- `architecture` is only for VHDL tops — the server errors if it is set
  for a Verilog top or missing for a VHDL top.
- `family` is **required** for `lattice`; for the other chips it defaults
  unless the user named a family.
- Port widths in the summary reflect the applied generic/parameter
  overrides (a `WIDTH=8` override shows up in the port table).
- A synthesis writes a netlist JSON to a temp dir only to read ports back;
  nothing is persisted, and the tool is safe to re-run.
- Resource counts are **per the top module** (submodule internals are
  counted by the flow, not itemized here).

## Configuration (env vars at server start)
- `YOSYNTH_MCP_YOSYS` — yosys binary (default `yosys` on `PATH`).
- `YOSYNTH_MCP_GHDL_PLUGIN` — path to the ghdl yosys plugin (`.so`);
  auto-found via `YOSYS_PLUGIN_PATH` / the yosys share dir when unset.
- `YOSYNTH_MCP_GHDL_PREFIX` — directory containing GHDL's compiled
  `std/`, `ieee/`, `src/` libraries (the `GHDL_PREFIX` env var; required
  for the ghdl frontend to find the std/ieee work libraries).
- `YOSYNTH_MCP_TIMEOUT` — default max seconds per synthesis (default 300).

Run with uvx straight from this repository:
`uvx yosynth-mcp` (or `uvx --from git+https://github.com/ru551n/yosynth-mcp yosynth-mcp`).
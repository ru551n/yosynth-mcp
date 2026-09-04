# yosynth-mcp

> **Archived.** This project is superseded by
> [`tsfpga-mcp`](https://github.com/ru551n/tsfpga-mcp), which provides the
> same MCP tool surface (synthesize/status/inspect/targets) but drives
> [`tsfpga`](https://github.com/ru551n/tsfpga)'s Yosys netlist-build
> classes instead of scripting `yosys`/`ghdl` directly. No further changes
> will be made here — please use `tsfpga-mcp` going forward.

MCP (stdio) server that lets an LLM/agent **synthesize VHDL and Verilog
designs with GHDL + Yosys** and get back a *short summary of the relevant
resources* — the top-level ports and the post-flow resource counts (cells
by type, wire bits, memories, submodules) — instead of a raw yosys dump.

Every synthesis is one `yosys -m ghdl.so -p '<script>'` process. The
script picks the frontend by top-level language:

- **VHDL top**: the ghdl frontend elaborates the top entity and its
  architecture (`ghdl <files> -e <top> <arch>`), with generic overrides
  (`-g name=value`).
- **Verilog / SystemVerilog top**: `read_verilog [-sv]` + `chparam`
  parameter overrides. VHDL units may be imported for the Verilog top to
  instantiate (`ghdl -read <files>`). The other direction also works: a
  VHDL top may instantiate Verilog units (`read_verilog` runs first, then
  `ghdl -e`) — mixed-language synthesis is a documented GHDL+Yosys feature.

The script then runs the chip's synthesis flow (`synth`, `synth_ice40`,
`synth_xilinx`, ...), `stat` and `write_json`; the server parses the port
list out of the netlist and the resource counts out of `stat`, and
reports only that. On failure it reports the actual diagnostics plus a
hint at the most common cause.

The server is **not pinned to a yosys version**: the available synthesis
flows and device families are probed from the installed binary at startup
(`yosynth_status` / `yosynth_targets`).

## Requirements

Three external pieces, on the `PATH` of the interpreter that runs the
server:

1. **yosys** — with the chip techlibs you want to target (stock builds
   ship the common ones: `synth`, `synth_ice40`, `synth_ecp5`,
   `synth_gowin`, `synth_xilinx`, `synth_lattice`, ...).
2. **the ghdl yosys plugin** (`ghdl.so`) — from
   [ghdl-yosys-plugin](https://github.com/ghdl/ghdl-yosys-plugin); set
   `YOSYS_PLUGIN_PATH` or `YOSYNTH_MCP_GHDL_PLUGIN` so it can be found.
3. **compiled GHDL std/ieee libraries** — GHDL's `make libs.vhdl.mcode`
   (or equivalent) output, pointed at by `GHDL_PREFIX` /
   `YOSYNTH_MCP_GHDL_PREFIX` (the directory containing `std/`, `ieee/`,
   `src/`).

The [`ru551n/hdl-docker`](https://github.com/ru551n/hdl-docker) Docker
image provides all three out of the box — this repo's CI runs in it.

## Setup

Run with `uvx`, straight from git or a local checkout:

```json
{
  "mcpServers": {
    "yosynth": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/ru551n/yosynth-mcp.git",
        "yosynth-mcp"
      ],
      "env": {
        "YOSYNTH_MCP_GHDL_PREFIX": "/path/to/ghdl/libs"
      }
    }
  }
}
```

`--from` also accepts a local checkout path (installed by content hash, so
edits are picked up automatically; `uvx --refresh` forces a re-resolve).

Or with MCP Inspector for manual testing:

```bash
npx @modelcontextprotocol/inspector \
  uvx --from git+https://github.com/ru551n/yosynth-mcp.git yosynth-mcp
```

## Configuration (env vars)

| Variable | Meaning | Default |
|---|---|---|
| `YOSYNTH_MCP_YOSYS` | yosys binary | `yosys` (on `PATH`) |
| `YOSYNTH_MCP_GHDL_PLUGIN` | path to the ghdl plugin (`ghdl.so`) | auto-found via `YOSYS_PLUGIN_PATH` / the yosys share dir |
| `YOSYNTH_MCP_GHDL_PREFIX` | dir containing GHDL's compiled `std/`, `ieee/`, `src/` libraries (the `GHDL_PREFIX` env var) | unset (yosys inherits `GHDL_PREFIX` from the environment) |
| `YOSYNTH_MCP_TIMEOUT` | max seconds per synthesis | `300` |

## Tools

| Tool | What it does |
|---|---|
| `yosynth_synthesize` | Synthesizes the given top level (VHDL entity or Verilog module) for a chip/family with optional generic/parameter overrides, and returns the short summary (ports + resources) or the failure diagnostics + hint. |
| `yosynth_inspect` | Static scan of the sources: VHDL entities with architectures and generics (name/type/default), Verilog modules with parameters, plus a list of ambiguities (multiple architectures, a unit declared in both languages, ...). Nothing is compiled. |
| `yosynth_targets` | The chip targets this server can synthesize for: per chip, the yosys flow, whether that flow exists in the installed yosys, and the known device families (xilinx `xc7`/`xcup`/..., lattice `lifcl`/`ecp5`/..., ice40 `hx`/`lp`/`u`, ...). |
| `yosynth_status` | Server config: yosys version, available flows, plugin path (exists/missing), `GHDL_PREFIX`, timeout. Call it first when things look misconfigured. |

### Example

`yosynth_synthesize(sources=["counter.vhd"], top="counter",
architecture="rtl", chip="generic", generics={"WIDTH": "8"})`:

```
Synthesis OK: top `counter` -> generic (flow: synth) in 0.4s.

Ports (3):
  clk   input   1 bit(s)
  rst   input   1 bit(s)
  outc  output  8 bit(s)

Resources:
  cells: 26 - 8 $_DFF_NN0_, 4 $_NOT_, 4 $_AND_, ...
  wire bits: 10
```

### Notes

- **Mixed language**: a Verilog top may instantiate VHDL units (pass both
  files in `sources`; the VHDL units appear in the netlist as
  `<entity>_B<arch>`, e.g. `vsub_Brtl`). A VHDL top may instantiate
  Verilog units too — pass both files in `sources`.
- **`sources` must cover everything the top needs** — GHDL has no
  work-library state between runs, so a missing file shows up as
  "not found in library" at synthesis time.
- **`family`** is required for `lattice` (`lifcl`, `ecp5`, `cnx`, ...),
  optional with a default for the other chips.
- **Generic names** are matched case-insensitively by GHDL for VHDL tops
  and exactly for Verilog parameters.

## Skill

This repo ships an agent skill, `skills/yosynth-mcp/SKILL.md`, that tells
the LLM *when* and *how* to use the tools — including the hard rule that
the top level, architecture, chip/family and generic values must be known
or **asked from the user**, never inferred. Install it next to the server
so the agent picks it up automatically.

### Claude Code

```bash
# personal — available in every project
ln -s /path/to/yosynth-mcp/skills/yosynth-mcp ~/.claude/skills/yosynth-mcp

# or project-local — available only in that project
mkdir -p <your-project>/.claude/skills
ln -s /path/to/yosynth-mcp/skills/yosynth-mcp <your-project>/.claude/skills/yosynth-mcp
```

## Development

```bash
uv sync            # install the package + dev tools (mypy, pytest, ruff)
uv run pytest      # unit + end-to-end tests (e2e skipped without a full setup)
uv run ruff check .
uv run mypy
```

The end-to-end tests synthesize real designs (VHDL top, Verilog top with a
VHDL submodule, VHDL top with a Verilog submodule, SystemVerilog top)
through the real yosys + ghdl plugin and are skipped — not failed — on
machines without the full setup. CI runs the full suite inside the
`ru551n/hdl-docker` image.

## License

[MIT](LICENSE)
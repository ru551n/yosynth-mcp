"""Server configuration from environment variables.

The server shells out to a ``yosys`` binary with the ghdl plugin loaded
(``yosys -m <ghdl.so> -p '<script>'``). Where to find them — plus the GHDL
library prefix and a per-run timeout — comes from the environment:

===========================  =============================================
``YOSYNTH_MCP_YOSYS``        yosys binary (default: ``yosys`` on PATH)
``YOSYNTH_MCP_GHDL_PLUGIN``  path to ``ghdl.so`` (the ghdl-yosys-plugin);
                             falls back to ``ghdl.so`` in each
                             ``YOSYS_PLUGIN_PATH`` entry
``YOSYNTH_MCP_GHDL_PREFIX``  exported as ``GHDL_PREFIX`` in the yosys
                             process (where ghdl finds std/ieee libraries);
                             unset = inherit the caller's environment
``YOSYNTH_MCP_TIMEOUT``      max seconds for one synthesis (default: 300)
===========================  =============================================
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 300.0
DEFAULT_YOSYS = "yosys"
PLUGIN_BASENAME = "ghdl.so"


class ConfigError(Exception):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Resolved server configuration."""

    yosys: str = DEFAULT_YOSYS
    plugin: Path | None = None
    ghdl_prefix: str | None = None
    timeout: float = DEFAULT_TIMEOUT


def _find_plugin(env: Mapping[str, str]) -> Path | None:
    raw = env.get("YOSYNTH_MCP_GHDL_PLUGIN", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"YOSYNTH_MCP_GHDL_PLUGIN={raw!r} is not a file; build the "
                "plugin (ghdl-yosys-plugin) with `make` and point the "
                "variable at the resulting ghdl.so"
            )
        return path
    for entry in env.get("YOSYS_PLUGIN_PATH", "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        candidate = Path(entry).expanduser() / PLUGIN_BASENAME
        if candidate.is_file():
            return candidate
    return None


def _find_timeout(env: Mapping[str, str]) -> float:
    raw = env.get("YOSYNTH_MCP_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ConfigError(f"YOSYNTH_MCP_TIMEOUT={raw!r} is not a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("YOSYNTH_MCP_TIMEOUT must be a finite number > 0")
    return timeout


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (default: ``os.environ``).

    Raises:
        ConfigError: if no ghdl plugin can be located or the timeout is
            invalid. The MCP tools translate this into an actionable
            error string instead of raising.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    plugin = _find_plugin(source)
    if plugin is None:
        raise ConfigError(
            "ghdl-yosys-plugin not found: set YOSYNTH_MCP_GHDL_PLUGIN to the "
            "path of ghdl.so (built from ghdl-yosys-plugin) or add its "
            f"directory to YOSYS_PLUGIN_PATH so {PLUGIN_BASENAME} is found"
        )
    return Config(
        yosys=source.get("YOSYNTH_MCP_YOSYS", DEFAULT_YOSYS).strip()
        or DEFAULT_YOSYS,
        plugin=_find_plugin(source),
        ghdl_prefix=(source.get("YOSYNTH_MCP_GHDL_PREFIX", "").strip() or None),
        timeout=_find_timeout(source),
    )

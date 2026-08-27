"""Shared fixtures.

``config_env`` resolves the test configuration from the environment with
sane local fallbacks (the plugin and the GHDL std/ieee library caches of
a no-sudo install), so the end-to-end tests run both in a plain shell and
under ``uvx``-style isolated environments.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from yosynth_mcp.config import Config, ConfigError, load_config

DESIGNS_DIR = Path(__file__).parent / "designs"

# Local (no-sudo) install locations, used as fallbacks when the YOSYNTH_MCP_*
# environment variables are not set.
FALLBACK_PLUGIN = Path.home() / ".local" / "share" / "yosys" / "plugins" / "ghdl.so"
FALLBACK_GHDL_PREFIX = Path.home() / ".local" / "share" / "ghdl" / "ghdl"


def test_env() -> dict[str, str]:
    env = dict(os.environ)
    if "YOSYNTH_MCP_GHDL_PLUGIN" not in env and FALLBACK_PLUGIN.is_file():
        env["YOSYNTH_MCP_GHDL_PLUGIN"] = str(FALLBACK_PLUGIN)
    if "YOSYNTH_MCP_GHDL_PREFIX" not in env and FALLBACK_GHDL_PREFIX.is_dir():
        env["YOSYNTH_MCP_GHDL_PREFIX"] = str(FALLBACK_GHDL_PREFIX)
    return env


def _prefix_usable(prefix: str | None) -> bool:
    """A GHDL prefix must contain compiled std/ieee libraries."""
    if prefix is None:
        # Unset: ghdl falls back to its baked-in /usr/local/lib/ghdl.
        return Path("/usr/local/lib/ghdl/std").is_dir()
    base = Path(prefix)
    return (base / "std").is_dir() and (base / "ieee").is_dir()


def e2e_available(env: dict[str, str]) -> bool:
    """Whether a full yosys+plugin+GHDL-libs setup is usable here."""
    if shutil.which(env.get("YOSYNTH_MCP_YOSYS", "yosys")) is None:
        return False
    try:
        config = load_config(env)
    except ConfigError:
        return False
    return _prefix_usable(config.ghdl_prefix)


@pytest.fixture
def config_env() -> dict[str, str]:
    return test_env()


@pytest.fixture
def config(config_env) -> Config:
    """A loadable config; skip when no plugin can be found."""
    try:
        return load_config(config_env)
    except ConfigError as exc:
        pytest.skip(f"yosynth environment unavailable: {exc}")


@pytest.fixture
def e2e(config_env) -> Iterator[Config]:
    """A config for real synthesis runs; skip without a full setup."""
    if not e2e_available(config_env):
        pytest.skip(
            "yosys binary, ghdl plugin, and GHDL std/ieee libraries all "
            "required (set YOSYNTH_MCP_GHDL_PLUGIN / YOSYNTH_MCP_GHDL_PREFIX)"
        )
    yield load_config(config_env)


@pytest.fixture
def designs_dir() -> Path:
    return DESIGNS_DIR

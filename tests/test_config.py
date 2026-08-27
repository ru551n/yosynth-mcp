"""Unit tests for config loading (no yosys required)."""

from __future__ import annotations

import pytest

from yosynth_mcp.config import ConfigError, load_config


class TestDefaults:
    def test_defaults(self, tmp_path):
        plugin = tmp_path / "ghdl.so"
        plugin.touch()
        config = load_config({"YOSYNTH_MCP_GHDL_PLUGIN": str(plugin)})
        assert config.yosys == "yosys"
        assert config.plugin == plugin
        assert config.ghdl_prefix is None
        assert config.timeout == 300.0

    def test_yosys_and_timeout_overrides(self, tmp_path):
        plugin = tmp_path / "ghdl.so"
        plugin.touch()
        config = load_config(
            {
                "YOSYNTH_MCP_GHDL_PLUGIN": str(plugin),
                "YOSYNTH_MCP_YOSYS": "/opt/bin/yosys",
                "YOSYNTH_MCP_TIMEOUT": "12.5",
                "YOSYNTH_MCP_GHDL_PREFIX": "/some/ghdl/libs",
            }
        )
        assert config.yosys == "/opt/bin/yosys"
        assert config.timeout == 12.5
        assert config.ghdl_prefix == "/some/ghdl/libs"

    def test_blank_ghdl_prefix_treated_as_unset(self, tmp_path):
        plugin = tmp_path / "ghdl.so"
        plugin.touch()
        config = load_config(
            {
                "YOSYNTH_MCP_GHDL_PLUGIN": str(plugin),
                "YOSYNTH_MCP_GHDL_PREFIX": "   ",
            }
        )
        assert config.ghdl_prefix is None


class TestTimeout:
    @pytest.mark.parametrize("raw", ["0", "-1", "abc", "1e999"])
    def test_invalid_timeout_rejected(self, tmp_path, raw):
        plugin = tmp_path / "ghdl.so"
        plugin.touch()
        with pytest.raises(ConfigError, match="TIMEOUT"):
            load_config(
                {
                    "YOSYNTH_MCP_GHDL_PLUGIN": str(plugin),
                    "YOSYNTH_MCP_TIMEOUT": raw,
                }
            )


class TestPlugin:
    def test_plugin_from_env(self, tmp_path):
        plugin = tmp_path / "ghdl.so"
        plugin.touch()
        assert load_config({"YOSYNTH_MCP_GHDL_PLUGIN": str(plugin)}).plugin == plugin

    def test_plugin_from_yosys_plugin_path(self, tmp_path):
        other = tmp_path / "other.so"
        plugin = tmp_path / "ghdl.so"
        other.touch()
        plugin.touch()
        config = load_config({"YOSYS_PLUGIN_PATH": f"/nonexistent:{tmp_path}"})
        assert config.plugin == plugin

    def test_explicit_plugin_wins_over_path(self, tmp_path):
        from_path = tmp_path / "ghdl.so"
        explicit = tmp_path / "mine.so"
        from_path.touch()
        explicit.touch()
        config = load_config(
            {
                "YOSYNTH_MCP_GHDL_PLUGIN": str(explicit),
                "YOSYS_PLUGIN_PATH": str(tmp_path),
            }
        )
        assert config.plugin == explicit

    def test_missing_explicit_plugin_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="not a file"):
            load_config({"YOSYNTH_MCP_GHDL_PLUGIN": str(tmp_path / "nope.so")})

    def test_no_plugin_anywhere_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config({"YOSYS_PLUGIN_PATH": str(tmp_path)})

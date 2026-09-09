import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "desktop_launcher.py"
SPEC = importlib.util.spec_from_file_location("desktop_launcher", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def test_should_restart_tunnel_for_repeated_connectivity_failures():
    lines = [
        "error: stream 233 canceled by remote with error code 0",
        "error: failed to serve tunnel connection",
        "error: timeout: no recent network activity",
    ]
    assert module.should_restart_tunnel(lines)


def test_should_not_restart_tunnel_for_normal_messages():
    lines = [
        "Your quick Tunnel has been created! Visit it at ...",
        "Registered tunnel connection",
        "cloudflared will not automatically update",
    ]
    assert not module.should_restart_tunnel(lines)


def test_build_cloudflared_command_prefers_existing_config(tmp_path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("tunnel: abc")
    command = module.build_cloudflared_start_command(
        "http://127.0.0.1:8787",
        config_path=str(config_file),
        binary="cloudflared",
    )
    assert command == ["cloudflared", "tunnel", "--config", str(config_file), "run"]


def test_resolve_cloudflared_config_path_returns_none_when_missing(tmp_path, monkeypatch):
    missing = tmp_path / "config.yml"
    monkeypatch.setenv("HERMES_WEBUI_TUNNEL_CONFIG", str(missing))
    assert module.resolve_cloudflared_config_path() is None


def test_resolve_cloudflared_config_path_returns_path_if_exists(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yml"
    config_file.write_text("tunnel: abc")
    monkeypatch.setenv("HERMES_WEBUI_TUNNEL_CONFIG", str(config_file))
    assert module.resolve_cloudflared_config_path() == str(config_file)


def test_build_cloudflared_command_falls_back_to_url_mode_without_config():
    command = module.build_cloudflared_start_command(
        "http://127.0.0.1:8787",
        config_path=None,
        binary="cloudflared",
    )
    assert command[:3] == ["cloudflared", "tunnel", "--url"]
    assert command[3] == "http://127.0.0.1:8787"

#!/usr/bin/env python3
"""Desktop launcher for Hermes WebUI-first usage.

This launcher provides a simple local desktop window that lets the user:
- open the configured Hermes WebUI in their default browser
- launch Hermes Core in the background
- start a lightweight local WebUI placeholder server for local testing
- minimize to a tray icon when available
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib import parse, request
import webbrowser


DEFAULT_TUNNEL_RESTART_ON_ERRORS = True

# Import tkinter lazily and handle missing Tcl/Tk libraries gracefully. When
# PyInstaller bundles the binary on systems that do not have the matching
# libtcl/libtk runtime available (e.g. libtcl9.0.so) an import at module
# scope raises and aborts the whole binary. Capture that error and allow a
# CLI-only fallback.
try:
    import tkinter as tk
    from tkinter import messagebox
    TKINTER_AVAILABLE = True
except Exception as _tk_exc:  # pragma: no cover - runtime-dependent
    tk = None  # type: ignore
    messagebox = None  # type: ignore
    TKINTER_AVAILABLE = False
    TK_IMPORT_ERROR = _tk_exc


DEFAULT_WEBUI_URL = os.getenv("HERMES_WEBUI_URL", "http://127.0.0.1:8787")
DEFAULT_LOCAL_WEBUI_PORT = int(os.getenv("HERMES_LOCAL_WEBUI_PORT", "8765"))
DEFAULT_TUNNEL_ENABLED = os.getenv("HERMES_WEBUI_TUNNEL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_TUNNEL_BINARY = os.getenv("HERMES_CLOUDFLARED_BINARY", "cloudflared")
DEFAULT_TUNNEL_MODE = os.getenv("HERMES_WEBUI_TUNNEL_MODE", "url").strip().lower()
DEFAULT_TUNNEL_TARGET = os.getenv("HERMES_WEBUI_TUNNEL_TARGET", DEFAULT_WEBUI_URL)
DEFAULT_TUNNEL_LOG_FILE = os.getenv("HERMES_WEBUI_TUNNEL_LOG_FILE", os.path.join(os.path.expanduser("~/.hermes"), "webui-tunnel.log"))
DEFAULT_TUNNEL_PID_FILE = os.getenv("HERMES_WEBUI_TUNNEL_PID_FILE", os.path.join(os.path.expanduser("~/.hermes"), "webui-tunnel.pid"))
DEFAULT_TUNNEL_CONFIG_PATH = os.getenv("HERMES_WEBUI_TUNNEL_CONFIG", os.path.expanduser("~/.cloudflared/config.yml"))


def extract_tunnel_url(text: str) -> Optional[str]:
    match = re.search(r"https://[A-Za-z0-9.-]+\.trycloudflare\.com", text)
    if match:
        return match.group(0)
    return None


def should_restart_tunnel(lines: list[str]) -> bool:
    text = "\n".join(lines).lower()
    if "stream" in text and "canceled by remote" in text:
        return True
    if "failed to serve tunnel connection" in text:
        return True
    if "timeout: no recent network activity" in text:
        return True
    return False


def resolve_cloudflared_config_path() -> Optional[str]:
    config_path = os.getenv("HERMES_WEBUI_TUNNEL_CONFIG") or DEFAULT_TUNNEL_CONFIG_PATH
    if not config_path:
        return None
    config_path = os.path.expanduser(config_path)
    return config_path if os.path.exists(config_path) else None


def build_cloudflared_start_command(target_url: str, *, config_path: Optional[str], binary: str) -> list[str]:
    if config_path and os.path.exists(config_path):
        return [binary, "tunnel", "--config", config_path, "run"]
    return [binary, "tunnel", "--url", target_url, "--protocol", "http2", "--edge-bind-address", "0.0.0.0"]


class HermesDesktopLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Hermes Desktop Launcher")
        self.root.geometry("560x340")
        self.root.resizable(False, False)

        self.url_var = tk.StringVar(value=self._default_url())
        self.status_var = tk.StringVar(value="Ready")
        self.tunnel_url_var = tk.StringVar(value="")
        self._core_process: Optional[subprocess.Popen] = None
        self._webui_server: Optional[ThreadingHTTPServer] = None
        self._webui_thread: Optional[threading.Thread] = None
        self._tray_icon = None
        self._tunnel_process: Optional[subprocess.Popen] = None
        self._tunnel_thread: Optional[threading.Thread] = None
        self._tunnel_log_path = DEFAULT_TUNNEL_LOG_FILE
        self._tunnel_pid_file = DEFAULT_TUNNEL_PID_FILE
        self._tunnel_enabled = DEFAULT_TUNNEL_ENABLED
        self._tunnel_target = DEFAULT_TUNNEL_TARGET
        self._tunnel_restart_state = {"recent_error_count": 0, "last_restart_at": 0.0}
        self._last_notified_tunnel_url: Optional[str] = None

        self._load_tunnel_settings_from_env()
        self._build_ui()
        self._setup_tray_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self._restore_tunnel_url_from_log()
        if self._tunnel_enabled:
            self._start_cloudflared_tunnel(self._tunnel_target)

    def _default_url(self) -> str:
        return os.getenv("HERMES_WEBUI_URL", DEFAULT_WEBUI_URL)

    def _build_ui(self) -> None:
        header = tk.Label(self.root, text="Hermes Desktop Launcher", font=("Segoe UI", 16, "bold"))
        header.pack(pady=(16, 8))

        frame = tk.Frame(self.root, padx=20, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="WebUI URL:").grid(row=0, column=0, sticky="w")
        entry = tk.Entry(frame, textvariable=self.url_var, width=48)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        button_row = tk.Frame(frame)
        button_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        tk.Button(button_row, text="Open Web UI", command=self.open_webui).pack(side="left", padx=(0, 8))
        tk.Button(button_row, text="Launch Hermes Core", command=self.launch_core).pack(side="left", padx=8)
        tk.Button(button_row, text="Start Local WebUI Server", command=self.start_local_webui_server).pack(side="left", padx=8)

        tk.Label(frame, text="Status:").grid(row=2, column=0, sticky="w", pady=(14, 0))
        status = tk.Label(frame, textvariable=self.status_var, wraplength=420, justify="left")
        status.grid(row=2, column=1, sticky="w", pady=(14, 0))

        tk.Label(frame, text="Tunnel URL:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        tunnel_url = tk.Label(frame, textvariable=self.tunnel_url_var, wraplength=420, justify="left", fg="#0b57d0")
        tunnel_url.grid(row=3, column=1, sticky="w", pady=(8, 0))

        frame.columnconfigure(1, weight=1)

    def _setup_tray_icon(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return

        try:
            image = Image.new("RGB", (64, 64), color=(32, 64, 128))
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 54, 54), fill=(74, 144, 226))
            draw.rectangle((20, 20, 44, 44), fill=(255, 255, 255))
            self._tray_icon = pystray.Icon(
                "Hermes",
                image,
                "Hermes Launcher",
                menu=pystray.Menu(
                    pystray.MenuItem("Show", self._show_window),
                    pystray.MenuItem("Open Web UI", self.open_webui),
                    pystray.MenuItem("Quit", self._quit),
                ),
            )
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception:
            self._tray_icon = None

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", False))

    def _hide_to_tray(self) -> None:
        if self._tray_icon is not None:
            self.root.withdraw()
            self.status_var.set("Hidden to tray. Use the tray icon to reopen.")
        else:
            self.root.destroy()

    def _quit(self) -> None:
        self.stop_local_webui_server()
        self.stop_core_process()
        self._stop_cloudflared_process()
        self.root.destroy()

    def _is_http_ready(self, url: str) -> bool:
        try:
            with request.urlopen(url, timeout=2) as response:
                return response.status < 500
        except Exception:
            return False

    def _parse_webui_url(self, url: str) -> tuple[Optional[str], Optional[int]]:
        parsed = parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None, None
        return parsed.hostname, parsed.port

    def _resolve_webui_root(self) -> Optional[str]:
        root = os.getenv("HERMES_WEBUI_ROOT")
        if root:
            root = os.path.expanduser(root)
            if os.path.isdir(root):
                return root
        root_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(root_dir, "..", "Applications", "hermes-webui")
        candidate = os.path.abspath(candidate)
        if os.path.isdir(candidate):
            return candidate
        candidate = os.path.expanduser("~/Applications/hermes-webui")
        if os.path.isdir(candidate):
            return candidate
        candidate = os.path.expanduser("~/.hermes/hermes-webui")
        if os.path.isdir(candidate):
            return candidate
        return None

    def _resolve_webui_start_script(self) -> Optional[str]:
        webui_root = self._resolve_webui_root()
        if webui_root is None:
            return None
        start_script = os.path.join(webui_root, "start.sh")
        return start_script if os.path.isfile(start_script) else None

    def _resolve_webui_python(self) -> Optional[str]:
        python_path = os.getenv("HERMES_WEBUI_PYTHON")
        if python_path:
            python_path = os.path.expanduser(python_path)
            if os.path.isfile(python_path):
                return python_path
        for executable in ["python3", "python"]:
            path = shutil.which(executable)
            if path:
                return path
        return None

    def _resolve_cloudflared_binary(self) -> Optional[str]:
        binary = os.getenv("HERMES_CLOUDFLARED_BINARY", DEFAULT_TUNNEL_BINARY)
        if binary and os.path.isabs(binary) and os.path.isfile(binary):
            return binary
        if shutil.which(binary):
            return shutil.which(binary)
        return shutil.which("cloudflared")

    def _load_tunnel_settings_from_env(self) -> None:
        env_file = os.path.expanduser("~/Applications/hermes-webui/.env")
        if not os.path.exists(env_file):
            return
        try:
            with open(env_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key == "HERMES_WEBUI_TUNNEL_ENABLED" and value.lower() in {"1", "true", "yes", "on"}:
                        self._tunnel_enabled = True
                    elif key == "HERMES_WEBUI_TUNNEL_ENABLED" and value.lower() in {"0", "false", "no", "off"}:
                        self._tunnel_enabled = False
                    elif key == "HERMES_WEBUI_TUNNEL_TARGET":
                        self._tunnel_target = value
        except Exception:
            return

    def _restore_tunnel_url_from_log(self) -> None:
        if not os.path.exists(self._tunnel_log_path):
            return
        try:
            with open(self._tunnel_log_path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
        except Exception:
            return
        tunnel_url = extract_tunnel_url(text)
        if tunnel_url:
            self.tunnel_url_var.set(tunnel_url)
            self.status_var.set(f"Tunnel ready: {tunnel_url}")

    def _notify_tunnel_url(self, tunnel_url: str) -> None:
        webhook_url = os.getenv("HERMES_TUNNEL_NOTIFY_URL") or os.getenv("DISCORD_WEBHOOK_URL")
        if not webhook_url or tunnel_url == self._last_notified_tunnel_url:
            return

        self._last_notified_tunnel_url = tunnel_url
        message = os.getenv("HERMES_TUNNEL_NOTIFY_MESSAGE", f"Hermes tunnel ready: {tunnel_url}")
        payload = json.dumps({"content": message}).encode("utf-8")
        req = request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                if getattr(response, "status", None) and response.status >= 400:
                    self.status_var.set(f"Tunnel notification failed with HTTP {response.status}")
        except Exception as exc:  # pragma: no cover - network dependent
            self.status_var.set(f"Could not send tunnel notification: {exc}")

    def _set_tunnel_url(self, tunnel_url: Optional[str]) -> None:
        if tunnel_url:
            self.tunnel_url_var.set(tunnel_url)
            self.status_var.set(f"Tunnel ready: {tunnel_url}")
            self._notify_tunnel_url(tunnel_url)

    def _refresh_tunnel_url_from_log(self) -> None:
        if not os.path.exists(self._tunnel_log_path):
            return
        try:
            with open(self._tunnel_log_path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
        except Exception:
            return
        tunnel_url = extract_tunnel_url(text)
        if tunnel_url:
            self._set_tunnel_url(tunnel_url)

    def _start_cloudflared_tunnel(self, target_url: str) -> bool:
        if self._tunnel_process is not None and self._tunnel_process.poll() is None:
            return True

        binary = self._resolve_cloudflared_binary()
        if binary is None:
            self.status_var.set("cloudflared is not installed or not on PATH")
            return False

        os.makedirs(os.path.dirname(self._tunnel_log_path), exist_ok=True)
        os.makedirs(os.path.dirname(self._tunnel_pid_file), exist_ok=True)

        config_path = resolve_cloudflared_config_path()
        if config_path:
            self.status_var.set(f"Starting Cloudflare tunnel from config {config_path}...")
        else:
            self.status_var.set(f"Starting Cloudflare tunnel to {target_url} using URL mode...")

        command = build_cloudflared_start_command(target_url, config_path=config_path, binary=binary)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                cwd=os.path.dirname(os.path.abspath(__file__)),
                text=True,
                start_new_session=True,
            )
        except Exception as exc:
            self.status_var.set(f"Could not start cloudflared: {exc}")
            return False

        self._tunnel_process = proc
        try:
            with open(self._tunnel_pid_file, "w", encoding="utf-8") as pid_file:
                pid_file.write(str(proc.pid))
        except Exception:
            pass

        self._tunnel_thread = threading.Thread(
            target=self._monitor_tunnel_output,
            args=(proc,),
            daemon=True,
        )
        self._tunnel_thread.start()
        return True

    def _restart_tunnel_if_needed(self, line: str) -> None:
        if not DEFAULT_TUNNEL_RESTART_ON_ERRORS:
            return
        now = time.time()
        if now - self._tunnel_restart_state["last_restart_at"] < 20:
            return
        self._tunnel_restart_state["last_restart_at"] = now
        if self._tunnel_process is not None and self._tunnel_process.poll() is None:
            with contextlib.suppress(Exception):
                self._tunnel_process.terminate()
            self._tunnel_process = None
            self.status_var.set("Cloudflare tunnel dropped; restarting it...")
            self._start_cloudflared_tunnel(self._tunnel_target)

    def _monitor_tunnel_output(self, proc: subprocess.Popen) -> None:
        error_lines: list[str] = []
        try:
            with open(self._tunnel_log_path, "a", encoding="utf-8") as log_file:
                if proc.stdout is None:
                    return
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    log_file.write(line + "\n")
                    log_file.flush()
                    tunnel_url = extract_tunnel_url(line)
                    if tunnel_url:
                        self._set_tunnel_url(tunnel_url)
                    elif "Error" in line or "error" in line:
                        self.status_var.set(f"Cloudflare tunnel: {line.strip()}")
                        error_lines.append(line)
                        if should_restart_tunnel(error_lines[-3:]):
                            self._restart_tunnel_if_needed(line)
        finally:
            if proc.poll() is not None and self._tunnel_process is proc:
                self._tunnel_process = None
                self.status_var.set("Cloudflare tunnel stopped")

    def _stop_cloudflared_process(self) -> None:
        if self._tunnel_process is None:
            return
        if self._tunnel_process.poll() is None:
            with contextlib.suppress(Exception):
                self._tunnel_process.terminate()
        self._tunnel_process = None
        with contextlib.suppress(Exception):
            os.remove(self._tunnel_pid_file)

    def _start_real_webui_server(self, url: str) -> bool:
        if self._is_http_ready(url):
            return True

        start_script = self._resolve_webui_start_script()
        if start_script is None:
            return False

        host, port = self._parse_webui_url(url)
        env = os.environ.copy()
        if host:
            env["HERMES_WEBUI_HOST"] = host
        if port:
            env["HERMES_WEBUI_PORT"] = str(port)
        python_path = self._resolve_webui_python()
        if python_path:
            env["HERMES_WEBUI_PYTHON"] = python_path

        try:
            subprocess.Popen(
                ["bash", start_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=os.path.dirname(start_script),
                start_new_session=True,
            )
        except Exception:
            return False

        for _ in range(30):
            if self._is_http_ready(url):
                return True
            time.sleep(1)
        return False

    def open_webui(self) -> None:
        url = self.url_var.get().strip() or self._default_url()
        if not self._is_http_ready(url):
            self.status_var.set(f"Starting Hermes WebUI at {url}...")
            if self._start_real_webui_server(url):
                self.status_var.set(f"Hermes WebUI started at {url}")
            else:
                self.status_var.set(f"Could not start Hermes WebUI. Opening {url} anyway.")

        try:
            if self._tunnel_enabled:
                self._start_cloudflared_tunnel(url)
            webbrowser.open(url, new=0)
            self.status_var.set(f"Opened {url} in your default browser")
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            self.status_var.set(f"Could not open browser: {exc}")
            messagebox.showerror("Browser error", f"Could not open {url}: {exc}")

    def launch_core(self) -> None:
        binary = self._resolve_core_binary()
        if binary is None:
            self.status_var.set("Hermes Core executable was not found. Build it first with build.sh")
            messagebox.showwarning("Missing executable", "Could not find a Hermes Core binary. Run build.sh first.")
            return

        if self._core_process is not None and self._core_process.poll() is None:
            self.status_var.set("Hermes Core is already running")
            return

        try:
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            self._core_process = subprocess.Popen([binary], **kwargs)
            self.status_var.set(f"Started Hermes Core in the background: {binary}")
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            self.status_var.set(f"Failed to launch Hermes Core: {exc}")
            messagebox.showerror("Launch failed", str(exc))

    def stop_core_process(self) -> None:
        if self._core_process is None:
            return
        if self._core_process.poll() is None:
            with contextlib.suppress(Exception):
                self._core_process.terminate()
        self._core_process = None

    def start_local_webui_server(self) -> None:
        if self._webui_server is not None:
            self.status_var.set("Local WebUI server is already running")
            return

        port = DEFAULT_LOCAL_WEBUI_PORT
        try:
            self._webui_server = ThreadingHTTPServer(("127.0.0.1", port), self._make_handler())
            self._webui_server.url_target = self._default_url()
            self._webui_thread = threading.Thread(target=self._webui_server.serve_forever, daemon=True)
            self._webui_thread.start()
            self.status_var.set(f"Started local WebUI placeholder on http://127.0.0.1:{port}")
        except OSError as exc:
            self.status_var.set(f"Could not start local WebUI server: {exc}")
            messagebox.showwarning("Server error", str(exc))

    def stop_local_webui_server(self) -> None:
        if self._webui_server is None:
            return
        with contextlib.suppress(Exception):
            self._webui_server.shutdown()
        with contextlib.suppress(Exception):
            self._webui_server.server_close()
        self._webui_server = None
        self._webui_thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        class HermesPlaceholderHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = f"""
                <html>
                  <head><title>Hermes Local WebUI</title></head>
                  <body style=\"font-family: sans-serif; margin: 2rem;\">
                    <h1>Hermes Local WebUI</h1>
                    <p>This lightweight launcher page is running locally.</p>
                    <p>Open your real WebUI at <a href=\"{self.server.url_target}\">{self.server.url_target}</a>.</p>
                  </body>
                </html>
                """.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        return HermesPlaceholderHandler

    def _resolve_core_binary(self) -> Optional[str]:
        candidates = []
        root_dir = os.path.dirname(os.path.abspath(__file__))
        local_bin = os.path.join(root_dir, "dist", "hermes")
        if os.path.exists(local_bin) and os.access(local_bin, os.X_OK):
            candidates.append(local_bin)

        win_bin = os.path.join(root_dir, "dist", "hermes.exe")
        if os.path.exists(win_bin):
            candidates.append(win_bin)

        env_binary = os.getenv("HERMES_CORE_BINARY")
        if env_binary:
            candidates.append(env_binary)

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        if shutil.which("hermes"):
            return shutil.which("hermes")
        if shutil.which("hermes.exe"):
            return shutil.which("hermes.exe")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Desktop launcher for Hermes WebUI-first use")
    parser.add_argument("--url", default=DEFAULT_WEBUI_URL, help="URL to open in the browser")
    parser.add_argument("--no-window", action="store_true", help="Only print the configured URL and exit")
    args = parser.parse_args()

    if args.no_window:
        print(args.url)
        return 0

    if not TKINTER_AVAILABLE:
        # Give a clear, actionable message instead of crashing when Tcl/Tk is
        # missing at runtime in the packed binary.
        print("Hermes desktop launcher cannot start the GUI because the Tcl/Tk runtime is missing.")
        print("")
        print("Possible fixes:")
        print("  * Install your platform's Tcl/Tk runtime (e.g. on Debian/Ubuntu: sudo apt install tk tcl)")
        print("  * Rebuild the launcher on this machine so PyInstaller bundles the correct runtime")
        print("")
        print("Quick workaround: run with --no-window to print the WebUI URL and avoid the GUI.")
        print("")
        print("Import error:", TK_IMPORT_ERROR)
        return 2

    root = tk.Tk()
    app = HermesDesktopLauncher(root)
    app.url_var.set(args.url)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

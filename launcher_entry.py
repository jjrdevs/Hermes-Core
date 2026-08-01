#!/usr/bin/env python3
"""Minimal launcher entry that avoids importing tkinter at analysis time.

This script is used as the PyInstaller entrypoint. It does not import
`desktop_launcher` directly (which would cause PyInstaller to include
tkinter hooks). Instead, it execs the bundled `desktop_launcher.py` at
runtime from the extracted PyInstaller temp directory (`sys._MEIPASS`).
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes launcher entry")
    parser.add_argument("--url", default=os.getenv("HERMES_WEBUI_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--no-window", action="store_true")
    args, remaining = parser.parse_known_args()

    if args.no_window:
        print(args.url)
        return 0

    # Determine the path to the bundled desktop_launcher.py inside the onefile
    # extraction folder. When running from source (not bundled), use the
    # repository file next to this script.
    desktop_script = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(__file__)), "desktop_launcher.py")

    if not os.path.exists(desktop_script):
        print("Could not find bundled desktop_launcher.py at:", desktop_script, file=sys.stderr)
        return 2

    # Attempt to import tkinter at runtime. If importing tkinter fails (for
    # example because the system Tcl/Tk runtime is missing), don't attempt
    # to execute the GUI script — instead print guidance and exit cleanly.
    try:
        import importlib

        try:
            importlib.import_module("tkinter")
        except Exception as e:  # pragma: no cover - environment dependent
            print("Hermes desktop launcher cannot start the GUI because the Tcl/Tk runtime is missing.")
            print("")
            print("Possible fixes:")
            print("  * Install your platform's Tcl/Tk runtime (e.g. on Debian/Ubuntu: sudo apt install tk tcl)")
            print("  * Rebuild the launcher on this machine so PyInstaller bundles the correct runtime")
            print("")
            print("Quick workaround: run with --no-window to print the WebUI URL and avoid the GUI.")
            print("")
            print("Import error:", e)
            return 2

        # Import succeeded; execute the bundled desktop launcher.
        import importlib.util

        spec = importlib.util.spec_from_file_location("desktop_launcher", desktop_script)
        if spec is None or spec.loader is None:
            print("Could not load desktop_launcher.py", file=sys.stderr)
            return 3
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    except Exception as exc:
        print("Launcher failed:", exc, file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

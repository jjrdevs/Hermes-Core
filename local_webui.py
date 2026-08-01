#!/usr/bin/env python3
"""Simple local placeholder WebUI for the Hermes launcher.

Usage: python3 local_webui.py [port] [target_url]
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        target = self.server.target_url
        body = f"""
        <html>
          <head><title>Hermes Local WebUI</title></head>
          <body style="font-family: sans-serif; margin: 2rem;">
            <h1>Hermes Local WebUI</h1>
            <p>This placeholder page is provided by the Hermes launcher.</p>
            <p>Open the real WebUI at <a href=\"{target}\">{target}</a>.</p>
            <p>If the real WebUI is not running, check the Hermes logs at <code>~/.local/share/hermes/hermes.log</code>.</p>
          </body>
        </html>
        """.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    target = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8787"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.target_url = target
    print(f"Local WebUI serving on http://127.0.0.1:{port} (links to {target})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

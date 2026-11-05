"""Serve the buildless Community viewer locally; no data API or writes."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

DASHBOARD_ROOT = Path(__file__).resolve().parents[1] / "products/community-dashboard"
LOOPBACK = "127.0.0.1"
ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/assets/dashboard.css": "assets/dashboard.css",
    "/assets/dashboard.mjs": "assets/dashboard.mjs",
    "/assets/report-model.mjs": "assets/report-model.mjs",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; "
    "img-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def resolve_asset(request_path: str, root: Path = DASHBOARD_ROOT) -> Path | None:
    """Allow only the fixed public assets, never listings or repository files."""
    relative = ROUTES.get(unquote(urlsplit(request_path).path))
    if relative is None:
        return None
    target = root / relative
    if any(part.is_symlink() for part in (target, *target.parents)):
        return None
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


class DashboardHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript"}

    def send_head(self):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403, "Only localhost preview is supported")
            return None
        if resolve_asset(self.path) is None:
            self.send_error(404, "Static asset not available")
            return None
        return super().send_head()

    def translate_path(self, path: str) -> str:
        target = resolve_asset(path)
        return str(target) if target is not None else str(DASHBOARD_ROOT / "not-available")

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging arbitrary request paths or query strings.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    handler = partial(DashboardHandler, directory=str(DASHBOARD_ROOT))
    with ThreadingHTTPServer((LOOPBACK, args.port), handler) as server:
        print(f"Community dashboard: http://{LOOPBACK}:{args.port}/", flush=True)
        print("Static local preview only; no uploads, API or action execution.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

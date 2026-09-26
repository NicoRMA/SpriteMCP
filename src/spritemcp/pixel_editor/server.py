"""Local HTTP server + browser UI for editing design layer PNGs.

Stdlib only (plus Pillow for blank-layer creation). Serves a small HTML/JS
editor; Apply writes RGBA PNGs back under ``design/layers/``.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image

from ..base_idle import CANVAS_H, CANVAS_W, EXPORT_LAYER_NAMES
from ..character import character_base_dir, validate_character_name
from ..config import resolve_output_dir
from ..outfit import design_dir, design_layers_dir, slot_layer_path

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# One editor server per process (re-open replaces / reuses).
_lock = threading.Lock()
_active: dict[str, Any] | None = None

_LAYER_RE = re.compile(r"^[a-z0-9_]+$")


def _blank_rgba() -> Image.Image:
    return Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))


def _blank_png_bytes() -> bytes:
    buf = BytesIO()
    _blank_rgba().save(buf, format="PNG")
    return buf.getvalue()


def _ensure_design_layers(name: str, output_dir: Path | str | None) -> Path:
    """Create missing transparent design layer PNGs; return layers dir."""
    layers = design_layers_dir(name, output_dir)
    layers.mkdir(parents=True, exist_ok=True)
    for layer in EXPORT_LAYER_NAMES:
        path = layers / f"{layer}.png"
        if not path.is_file():
            _blank_rgba().save(path)
    return layers


def _png_bytes(path: Path) -> bytes:
    if not path.is_file():
        return _blank_png_bytes()
    img = Image.open(path).convert("RGBA")
    if img.size != (CANVAS_W, CANVAS_H):
        blank = _blank_rgba()
        blank.paste(img.crop((0, 0, min(img.width, CANVAS_W), min(img.height, CANVAS_H))))
        img = blank
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _underlay_bytes(name: str, layer: str, output_dir: Path | str | None) -> bytes:
    """Dimmed body layer for alignment (base only; never written back).

    Head underlay always includes authored ``eyes.png`` detail (same as compose).
    """
    base_path = character_base_dir(name, output_dir) / "layers" / f"{layer}.png"
    if not base_path.is_file():
        return _blank_png_bytes()
    img = Image.open(base_path).convert("RGBA")
    if img.size != (CANVAS_W, CANVAS_H):
        canvas = _blank_rgba()
        canvas.paste(
            img.crop((0, 0, min(img.width, CANVAS_W), min(img.height, CANVAS_H)))
        )
        img = canvas
    # Dim alpha so design paint stays readable on top.
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * 0.35) if v else 0)
    img = Image.merge("RGBA", (r, g, b, a))
    if layer == "head":
        from ..base_idle import (
            _stamp_eyes_detail,
            eyes_coords_from_part_map_payload,
            load_authored_eyes_coords,
        )

        eyes: list[tuple[int, int]]
        part_json = character_base_dir(name, output_dir) / "part_map.json"
        if part_json.is_file():
            try:
                payload = json.loads(part_json.read_text(encoding="utf-8"))
                eyes = eyes_coords_from_part_map_payload(payload)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                eyes = load_authored_eyes_coords()
        else:
            eyes = load_authored_eyes_coords()
        # Full-opacity white eyes on dimmed head (readable face detail).
        img = _stamp_eyes_detail(img, list(eyes))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_data_url_png(data_url: str) -> Image.Image:
    raw = data_url.strip()
    if raw.startswith("data:"):
        _, _, b64 = raw.partition(",")
    else:
        b64 = raw
    data = base64.b64decode(b64)
    img = Image.open(BytesIO(data)).convert("RGBA")
    if img.size != (CANVAS_W, CANVAS_H):
        raise ValueError(
            f"Layer PNG must be {CANVAS_W}x{CANVAS_H}, got {img.size[0]}x{img.size[1]}"
        )
    return img


def _make_handler(ctx: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    name: str = ctx["name"]
    output_dir: Path = ctx["output_dir"]
    layers_dir: Path = ctx["layers_dir"]

    class EditorHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Quiet console; MCP stderr stays readable.
            return

        def _send(
            self,
            code: int,
            body: bytes,
            content_type: str,
            *,
            cache: bool = False,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if not cache:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path in ("/", "/index.html"):
                html = (_STATIC_DIR / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
                return

            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                if ".." in rel or rel.startswith(("/", "\\")):
                    self._send_json(404, {"error": "not found"})
                    return
                file_path = (_STATIC_DIR / rel).resolve()
                if not str(file_path).startswith(str(_STATIC_DIR.resolve())):
                    self._send_json(404, {"error": "not found"})
                    return
                if not file_path.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                ctype = {
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".html": "text/html; charset=utf-8",
                    ".png": "image/png",
                }.get(file_path.suffix.lower(), "application/octet-stream")
                # No browser cache so UI tweaks show up after refresh.
                self._send(200, file_path.read_bytes(), ctype, cache=False)
                return

            if path == "/api/info":
                compose = design_dir(name, output_dir) / "compose_preview.png"
                self._send_json(
                    200,
                    {
                        "name": name,
                        "canvas": {"w": CANVAS_W, "h": CANVAS_H},
                        "layers": list(EXPORT_LAYER_NAMES),
                        "layers_dir": str(layers_dir),
                        "has_compose_preview": compose.is_file(),
                    },
                )
                return

            if path.startswith("/api/layer/"):
                layer = path[len("/api/layer/") :].removesuffix(".png")
                if layer not in EXPORT_LAYER_NAMES or not _LAYER_RE.match(layer):
                    self._send_json(404, {"error": f"unknown layer: {layer}"})
                    return
                png = _png_bytes(slot_layer_path(name, layer, output_dir))
                self._send(200, png, "image/png")
                return

            if path.startswith("/api/underlay/"):
                layer = path[len("/api/underlay/") :].removesuffix(".png")
                if layer not in EXPORT_LAYER_NAMES or not _LAYER_RE.match(layer):
                    self._send_json(404, {"error": f"unknown layer: {layer}"})
                    return
                png = _underlay_bytes(name, layer, output_dir)
                self._send(200, png, "image/png")
                return

            if path == "/api/compose_preview.png":
                compose = design_dir(name, output_dir) / "compose_preview.png"
                if not compose.is_file():
                    self._send_json(404, {"error": "compose_preview missing"})
                    return
                self._send(200, compose.read_bytes(), "image/png")
                return

            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path == "/api/apply":
                try:
                    payload = self._read_json()
                except (json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                layers_payload = payload.get("layers")
                if not isinstance(layers_payload, dict):
                    self._send_json(
                        400,
                        {"ok": False, "error": "layers must be an object"},
                    )
                    return
                written: list[str] = []
                try:
                    for layer, data_url in layers_payload.items():
                        if layer not in EXPORT_LAYER_NAMES:
                            raise ValueError(f"unknown layer: {layer}")
                        if not isinstance(data_url, str):
                            raise ValueError(f"layer {layer}: expected data URL string")
                        img = _decode_data_url_png(data_url)
                        out = slot_layer_path(name, layer, output_dir)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        img.save(out)
                        written.append(layer)
                except (ValueError, OSError) as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "written": written,
                        "layers_dir": str(layers_dir),
                        "next_step": (
                            "Design layers saved. Ask the agent to call "
                            "compose_character, then continue to plan_animation "
                            "if editing is done."
                        ),
                    },
                )
                return

            self._send_json(404, {"error": "not found"})

    return EditorHandler


def open_pixel_editor(
    name: str,
    *,
    output_dir: Path | str | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Start (or reuse) the local pixel editor for a character's design layers.

    Opens a desktop browser UI on a free localhost port. Apply writes PNGs to
    ``design/layers/<layer>.png`` only — never ``base/``.

    Parameters
    ----------
    name:
        Character folder name under ``characters/``.
    output_dir:
        Output root override (else session ``set_output_root`` / ``<cwd>/output``).
    port:
        TCP port (0 = ephemeral free port).
    open_browser:
        When True, open the system default browser to the editor URL.
    """
    global _active

    name = validate_character_name(name)
    root = resolve_output_dir(output_dir)
    char_base = character_base_dir(name, root)
    if not char_base.is_dir():
        raise FileNotFoundError(
            f"Character '{name}' not found under {root / 'characters'}. "
            "Call generate_character first."
        )

    layers_dir = _ensure_design_layers(name, root)
    ddir = design_dir(name, root)

    with _lock:
        if _active is not None:
            # Same character + root → reuse; otherwise shut down and replace.
            same = (
                _active.get("name") == name
                and Path(_active.get("output_dir", "")) == root
                and _active.get("httpd") is not None
            )
            if same:
                url = _active["url"]
                if open_browser:
                    webbrowser.open(url)
                return {
                    "ok": True,
                    "reused": True,
                    "name": name,
                    "url": url,
                    "host": _active["host"],
                    "port": _active["port"],
                    "layers_dir": str(layers_dir),
                    "design_dir": str(ddir),
                    "output_root": str(root),
                    "canvas": {"w": CANVAS_W, "h": CANVAS_H},
                    "layers": list(EXPORT_LAYER_NAMES),
                    "next_step": (
                        "Ask the user to edit in the browser, click Apply, "
                        "then call compose_character before plan_animation."
                    ),
                }
            try:
                _active["httpd"].shutdown()
            except Exception:
                pass
            _active = None

        ctx: dict[str, Any] = {
            "name": name,
            "output_dir": root,
            "layers_dir": layers_dir,
        }
        handler = _make_handler(ctx)
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        host, bound_port = httpd.server_address[0], httpd.server_address[1]
        url = f"http://{host}:{bound_port}/"

        thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"spritemcp-pixel-editor-{name}",
            daemon=True,
        )
        thread.start()

        _active = {
            "name": name,
            "output_dir": root,
            "layers_dir": layers_dir,
            "httpd": httpd,
            "thread": thread,
            "host": host,
            "port": bound_port,
            "url": url,
        }

    if open_browser:
        webbrowser.open(url)

    return {
        "ok": True,
        "reused": False,
        "name": name,
        "url": url,
        "host": host,
        "port": bound_port,
        "layers_dir": str(layers_dir),
        "design_dir": str(ddir),
        "output_root": str(root),
        "canvas": {"w": CANVAS_W, "h": CANVAS_H},
        "layers": list(EXPORT_LAYER_NAMES),
        "note": (
            "Pixel editor running locally. Edit design layers only "
            "(body underlay is alignment). Apply writes design/layers/*.png. "
            "After Apply, call compose_character."
        ),
        "next_step": (
            "Ask the user to edit in the browser, click Apply, "
            "then call compose_character before plan_animation."
        ),
    }

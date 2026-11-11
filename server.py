#!/usr/bin/env python3
"""AutoCAD MCP Server — DXF file operations via ezdxf.

This MCP server lets an AI agent create, edit, query, and export
AutoCAD-compatible DXF drawings entirely on macOS (no AutoCAD installation
required).  It uses the `ezdxf` library for all geometry operations.

Typical (fast) workflow — 2 round trips instead of 35:
  1. new_drawing / open_drawing                 → create or load a drawing
  2. apply_ops(drawing, ops=[...])              → many entities in ONE call
  3. finish(drawing, save=True, svg=True)       → save DXF + preview in ONE call
  4. inspect_drawing / view_drawing             → verify in ONE call

Every tool runs in a worker thread, so a 0.8 s save or a 1 s export never
blocks the server: other calls (and heartbeats) stay responsive.
"""

from __future__ import annotations

import atexit
import functools
import inspect
import json
import logging
import math
import os
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import anyio
import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.addons.drawing import Frontend, RenderContext, svg
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.layout import Page
from ezdxf.document import Drawing
from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------------------
# Logging — keep stderr quiet (mcp logs every request at INFO, 60+ lines/run)
# ---------------------------------------------------------------------------

_LOG_LEVEL = os.environ.get("AUTOCAD_MCP_LOG", "WARNING").upper()
for _logger_name in ("mcp", "ezdxf", "matplotlib", "httpx"):
    logging.getLogger(_logger_name).setLevel(_LOG_LEVEL)

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

mcp = FastMCP("autocad-mcp")

# In-memory store of open drawings keyed by a user-chosen name.
_drawings: dict[str, Drawing] = {}

# Per-drawing bounding-box caches (keyed by entity handle → reuse is safe for
# add/delete; we never mutate geometry in place).
_bbox_caches: dict[str, ezdxf_bbox.Cache] = {}

# Drawings modified since the last save; flushed to disk on exit.
_dirty: set[str] = set()

class _RWLock:
    """Shared/exclusive lock: read-only tools run concurrently, writes exclusive.

    A plain RLock serialised everything, so a 0.8 s export blocked even a
    1 ms `list_drawings`.  ezdxf Drawing objects are not thread-safe, but
    readers never mutate, so they can share.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self):
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


_lock = _RWLock()

# Rendering backends keep global state: matplotlib is not thread-safe.
_render_lock = threading.Lock()
_bbox_lock = threading.Lock()

# Default output directory (~ and $VARS are expanded).
_output_dir = Path(os.environ.get("AUTOCAD_MCP_OUTPUT", "~/Documents/AutoCAD_MCP")).expanduser()


def _ensure_output_dir() -> Path:
    _output_dir.mkdir(parents=True, exist_ok=True)
    return _output_dir


def _resolve_out(file_path: str, drawing: str, ext: str) -> Path:
    """Return the absolute output path, defaulting to <output_dir>/<drawing>.<ext>."""
    if file_path:
        path = Path(file_path).expanduser()
    else:
        _ensure_output_dir()
        path = _output_dir / f"{drawing}.{ext}"
        return path.resolve()
    return path.resolve()


def _get_drawing(name: str) -> Drawing:
    if name not in _drawings:
        raise ValueError(
            f"No drawing named '{name}' is open. "
            f"Open drawings: {list(_drawings.keys()) or '(none)'}. "
            f"Use new_drawing or open_drawing first."
        )
    return _drawings[name]


def _touch(name: str) -> None:
    _dirty.add(name)


# ---------------------------------------------------------------------------
# Tool registration: sync implementation + async (threaded, locked) wrapper
# ---------------------------------------------------------------------------


def tool(fn: Callable[..., Any] | None = None, *, readonly: bool = False):
    """Register a function as an MCP tool.

    The call runs in a worker thread so it never blocks the event loop, under
    a shared lock (readonly=True) or an exclusive lock (mutating tools).

    The decorated module-level name still resolves to the original *sync*
    function, so `server.draw_line(...)` keeps working from scripts and tests.
    """

    def register(func: Callable[..., Any]) -> Callable[..., Any]:
        # eval_str turns "Image" (from __future__ annotations) into the real
        # type — FastMCP needs the class, not the string, to detect `Image`.
        sig = inspect.signature(func, eval_str=True)
        guard = _lock.read if readonly else _lock.write

        @functools.wraps(func)
        async def _call(**kwargs: Any) -> Any:
            bound = sig.bind(**kwargs)
            bound.apply_defaults()
            args, kw = bound.args, bound.kwargs

            def _run() -> Any:
                with guard():
                    return func(*args, **kw)

            return await anyio.to_thread.run_sync(_run)

        _call.__signature__ = sig  # type: ignore[attr-defined]
        mcp.tool()(_call)
        return func

    return register(fn) if fn is not None else register


# ---------------------------------------------------------------------------
# Geometry helpers — shared by the single-entity tools and by apply_ops
# ---------------------------------------------------------------------------


def _attr(layer: str = "0", color: int | None = None) -> dict[str, Any]:
    attribs: dict[str, Any] = {"layer": layer or "0"}
    if color is not None:
        attribs["color"] = int(color)
    return attribs


def _ensure_layer(doc: Drawing, layer: str, color: int = 7) -> None:
    if layer and layer not in doc.layers:
        doc.layers.add(layer, color=int(color))


def _points(raw: Any) -> list[tuple[float, float]]:
    """Accept a JSON string or a list of [x, y] pairs → list of float tuples."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not raw or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in raw):
        raise ValueError("points must be an array of [x, y] pairs")
    return [(float(p[0]), float(p[1])) for p in raw]


def _add_line(doc, x1, y1, x2, y2, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_line((x1, y1), (x2, y2), dxfattribs=_attr(layer, color))


def _add_circle(doc, cx, cy, radius, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_circle((cx, cy), radius, dxfattribs=_attr(layer, color))


def _add_arc(doc, cx, cy, radius, start_angle, end_angle, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_arc(
        (cx, cy), radius, start_angle, end_angle, dxfattribs=_attr(layer, color)
    )


def _add_rect(doc, x, y, width, height, layer="0", color=None):
    _ensure_layer(doc, layer)
    points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    doc.modelspace().add_lwpolyline(points, close=True, dxfattribs=_attr(layer, color))


def _add_polygon(doc, cx, cy, radius, sides=6, rotation=0.0, layer="0", color=None):
    _ensure_layer(doc, layer)
    rot_rad = math.radians(rotation)
    points = [
        (
            cx + radius * math.cos(rot_rad + 2 * math.pi * i / sides),
            cy + radius * math.sin(rot_rad + 2 * math.pi * i / sides),
        )
        for i in range(sides)
    ]
    doc.modelspace().add_lwpolyline(points, close=True, dxfattribs=_attr(layer, color))


def _add_polyline(doc, points, closed=False, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_lwpolyline(
        _points(points), close=bool(closed), dxfattribs=_attr(layer, color)
    )


def _add_spline(doc, points, degree=3, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_spline(_points(points), degree=degree, dxfattribs=_attr(layer, color))


def _add_ellipse(doc, cx, cy, major_axis_x, major_axis_y, ratio=0.5, layer="0", color=None):
    _ensure_layer(doc, layer)
    doc.modelspace().add_ellipse(
        (cx, cy), major_axis=(major_axis_x, major_axis_y, 0), ratio=ratio,
        dxfattribs=_attr(layer, color),
    )


def _add_text(doc, text, x, y, height=2.5, rotation=0, layer="0", color=None):
    _ensure_layer(doc, layer)
    attribs = _attr(layer, color)
    attribs["rotation"] = rotation
    doc.modelspace().add_text(text, height=height, dxfattribs=attribs).set_placement((x, y))


def _add_mtext(doc, text, x, y, width=100, height=2.5, layer="0", color=None):
    _ensure_layer(doc, layer)
    attribs = _attr(layer, color)
    attribs.update({"char_height": height, "width": width})
    doc.modelspace().add_mtext(text, dxfattribs=attribs).set_location((x, y))


# Typical annotation text height per $INSUNITS value, in drawing units.
_UNIT_TEXT_HEIGHT = {1: 0.125, 2: 0.25, 4: 2.5, 5: 2.5, 6: 0.25}


def _default_text_height(doc) -> float:
    """Guess a sane text height from the drawing units (mm → 2.5, m → 0.25)."""
    try:
        units = int(doc.header.get("$INSUNITS", 4))
    except Exception:
        units = 4
    return _UNIT_TEXT_HEIGHT.get(units, 2.5)


def _add_dim(doc, x1, y1, x2, y2, distance=0.0, angle=0.0, layer="0", text_height=0.0):
    """Linear dimension, auto-scaled to the drawing units.

    `distance` is the signed offset of the dimension line away from the measured
    segment: positive places it below (horizontal dims) or to the left (vertical
    dims), 0 lets it scale with the text height.  `text_height` 0 = derive it
    from $INSUNITS, so a 10 m plan gets 0.25 m text instead of 2.5 m text.
    """
    _ensure_layer(doc, layer)
    th = float(text_height) if text_height and float(text_height) > 0 else _default_text_height(doc)
    offset = abs(float(distance)) if distance else th * 3.5
    if angle:  # vertical measurement along Y, dimension line offset in -X
        base = (min(x1, x2) - offset, y1)
        ang = 90
    else:  # horizontal measurement along X, dimension line offset in -Y
        base = (x1, min(y1, y2) - offset)
        ang = 0
    # NOTE: `layer` used to be ignored here (dimension landed on layer "0").
    doc.modelspace().add_linear_dim(
        base=base, p1=(x1, y1), p2=(x2, y2), angle=ang,
        override={
            "dimtxt": th,
            "dimasz": th * 0.7,     # arrow size
            "dimexo": th * 0.4,     # extension line offset
            "dimexe": th * 0.6,     # extension line beyond dim line
            "dimgap": th * 0.3,     # gap between text and line
            "dimdec": 2 if th <= 1 else 0,
        },
        dxfattribs={"layer": layer},
    ).render()


def _add_hatch(doc, points, pattern="ANSI31", scale=1.0, layer="0", color=None):
    _ensure_layer(doc, layer)
    hatch = doc.modelspace().add_hatch(dxfattribs=_attr(layer))
    hatch.set_pattern_fill(pattern, scale=scale)
    # set_pattern_fill() resets dxf.color to 7 — set it afterwards.
    # `color if color else 7` used to turn color=0 into 7; now None → BYLAYER.
    hatch.dxf.color = int(color) if color is not None else 256  # 256 = BYLAYER
    hatch.paths.add_polyline_path(_points(points), is_closed=True)


def _add_wall(doc, x1, y1, x2, y2, thickness=0.24, layer="Walls", color=1):
    _ensure_layer(doc, layer, color)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("start and end points are the same")
    nx, ny = -dy / length * thickness / 2, dx / length * thickness / 2
    points = [
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    ]
    doc.modelspace().add_lwpolyline(points, close=True, dxfattribs=_attr(layer))


def _add_door(doc, x, y, width=0.9, direction="right", layer="Doors", color=3):
    _ensure_layer(doc, layer, color)
    msp = doc.modelspace()
    attribs = _attr(layer)
    if str(direction).lower() == "right":
        msp.add_line((x, y), (x + width, y), dxfattribs=attribs)
        msp.add_arc((x, y), width, 0, 90, dxfattribs=attribs)
    else:
        msp.add_line((x, y), (x - width, y), dxfattribs=attribs)
        msp.add_arc((x, y), width, 90, 180, dxfattribs=attribs)


def _add_window(doc, x1, y1, x2, y2, thickness=0.24, layer="Windows", color=5):
    """Three parallel lines inside a wall opening (`thickness` was hard-coded)."""
    _ensure_layer(doc, layer, color)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("start and end points are the same")
    nx, ny = -dy / length * (thickness / 2), dx / length * (thickness / 2)
    msp = doc.modelspace()
    for offset in (-1.0, 0.0, 1.0):
        msp.add_line(
            (x1 + nx * offset, y1 + ny * offset),
            (x2 + nx * offset, y2 + ny * offset),
            dxfattribs=_attr(layer),
        )


def _add_block_entity(doc, block_name, entity_type, params):
    if block_name not in doc.blocks:
        raise ValueError(f"block '{block_name}' not found")
    blk = doc.blocks[block_name]
    et = str(entity_type).lower()
    if et == "line":
        blk.add_line(tuple(params["start"]), tuple(params["end"]))
    elif et == "circle":
        blk.add_circle(tuple(params["center"]), params["radius"])
    elif et == "arc":
        blk.add_arc(
            tuple(params["center"]), params["radius"],
            params["start_angle"], params["end_angle"],
        )
    elif et == "lwpolyline":
        blk.add_lwpolyline(params["points"], close=params.get("closed", False))
    elif et == "text":
        blk.add_text(params["text"], height=params.get("height", 2.5)).set_placement(
            tuple(params["insert"])
        )
    else:
        raise ValueError(
            f"unknown entity_type '{entity_type}'. Use: line, circle, arc, lwpolyline, text."
        )


def _extents(doc: Drawing, name: str):
    cache = _bbox_caches.get(name)
    if cache is None:
        cache = _bbox_caches[name] = ezdxf_bbox.Cache()
    return ezdxf_bbox.extents(doc.modelspace(), cache=cache)


def _describe_entity(index: int, entity) -> str:
    etype = entity.dxftype()
    handle = entity.dxf.handle
    info = f"  [{index}] #{handle} {etype} layer='{entity.dxf.layer}'"
    try:
        if etype == "LINE":
            info += (
                f" ({entity.dxf.start.x:.2f},{entity.dxf.start.y:.2f})"
                f"→({entity.dxf.end.x:.2f},{entity.dxf.end.y:.2f})"
            )
        elif etype == "CIRCLE":
            c = entity.dxf.center
            info += f" center=({c.x:.2f},{c.y:.2f}) r={entity.dxf.radius:.2f}"
        elif etype in ("TEXT", "MTEXT"):
            info += f" '{entity.dxf.text}'"
        elif etype == "LWPOLYLINE":
            info += f" {len(entity)} vertices{' closed' if entity.closed else ''}"
        elif etype == "INSERT":
            info += f" block='{entity.dxf.name}'"
    except Exception:  # pragma: no cover - defensive
        pass
    return info


# ── Drawing lifecycle ──────────────────────────────────────────────────────


@tool
def new_drawing(name: str, dxf_version: str = "R2018") -> str:
    """Create a new empty DXF drawing in memory.

    Args:
        name: A short label used to reference this drawing later.
        dxf_version: DXF version string, e.g. "R2018", "R2013", "R2010", "R2007", "R2004", "R2000".
    """
    if name in _drawings:
        return f"⚠️ Drawing '{name}' already exists. Close it first or pick another name."
    doc = ezdxf.new(dxf_version)
    doc.header["$INSUNITS"] = 6  # meters; change via set_units if needed
    _drawings[name] = doc
    _bbox_caches.pop(name, None)
    return f"✅ Created new drawing '{name}' (DXF {dxf_version})."


@tool
def open_drawing(name: str, file_path: str) -> str:
    """Open an existing DXF file from disk.

    Args:
        name: Label for this drawing session.
        file_path: Absolute path to the .dxf file.
    """
    if name in _drawings:
        return f"⚠️ Drawing '{name}' already exists in memory."
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"❌ File not found: {path}"
    try:
        doc = ezdxf.readfile(str(path))
    except Exception as e:
        return f"❌ Failed to open: {e}"
    _drawings[name] = doc
    _bbox_caches.pop(name, None)
    return f"✅ Opened '{path.name}' as '{name}'."


@tool(readonly=True)
def save_drawing(drawing: str = "", file_path: str = "", name: str = "") -> str:
    """Save the drawing to a DXF file.

    Args:
        drawing: Drawing label.
        file_path: Where to save. Defaults to <output_dir>/<drawing>.dxf.
        name: Deprecated alias for `drawing` (kept for backward compatibility).
    """
    target = drawing or name
    doc = _get_drawing(target)
    path = _resolve_out(file_path, target, "dxf")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.saveas(str(path))
    except Exception as e:
        return f"❌ Save failed: {e}"
    _dirty.discard(target)
    return f"✅ Saved '{target}' → {path}"


@tool
def close_drawing(name: str) -> str:
    """Close a drawing and remove it from memory (does NOT auto-save).

    Args:
        name: Drawing label to close.
    """
    if name not in _drawings:
        return f"⚠️ No drawing named '{name}' is open."
    del _drawings[name]
    _bbox_caches.pop(name, None)
    _dirty.discard(name)
    return f"✅ Closed drawing '{name}'."


@tool(readonly=True)
def list_drawings() -> str:
    """List all currently open drawings."""
    if not _drawings:
        return "No drawings are currently open."
    lines = []
    for n, doc in _drawings.items():
        msp = doc.modelspace()
        lines.append(f"  • {n}  (DXF {doc.dxfversion}, {len(msp)} entities in modelspace)")
    return "Open drawings:\n" + "\n".join(lines)


# ── Layer management ───────────────────────────────────────────────────────


@tool
def create_layer(
    drawing: str,
    layer_name: str,
    color: int = 7,
    linetype: str = "Continuous",
) -> str:
    """Create a new layer.

    Args:
        drawing: Drawing label.
        layer_name: Name for the new layer.
        color: ACI color index (1‑255). 7=white/black.
        linetype: Line type name (e.g. "Continuous", "DASHED").
    """
    doc = _get_drawing(drawing)
    if layer_name in doc.layers:
        return f"⚠️ Layer '{layer_name}' already exists."
    if linetype not in doc.linetypes:
        doc.layers.add(layer_name, color=color)
        return (
            f"⚠️ Layer '{layer_name}' created with linetype 'Continuous' "
            f"(linetype '{linetype}' is not loaded in this drawing)."
        )
    doc.layers.add(layer_name, color=color, linetype=linetype)
    return f"✅ Layer '{layer_name}' created (color={color}, linetype={linetype})."


@tool(readonly=True)
def list_layers(drawing: str) -> str:
    """List all layers in a drawing.

    Args:
        drawing: Drawing label.
    """
    doc = _get_drawing(drawing)
    lines = []
    for layer in doc.layers:
        lines.append(
            f"  • {layer.dxf.name}  color={layer.dxf.color}  "
            f"linetype={layer.dxf.linetype}  "
            f"{'🔒 locked' if layer.is_locked() else '🔓'}"
        )
    return "Layers:\n" + "\n".join(lines) if lines else "No layers defined."


@tool
def delete_layer(drawing: str, layer_name: str) -> str:
    """Delete a layer (ALL entities on it, including block entities, move to '0').

    Args:
        drawing: Drawing label.
        layer_name: Layer to delete.
    """
    doc = _get_drawing(drawing)
    if layer_name == "0":
        return "❌ Cannot delete the default layer '0'."
    if layer_name not in doc.layers:
        return f"⚠️ Layer '{layer_name}' does not exist."
    # Move entities off the layer first — modelspace AND block definitions,
    # otherwise blocks keep dangling references to a deleted layer.
    moved = 0
    for entity in doc.entitydb.values():
        try:
            if entity.dxf.layer == layer_name:
                entity.dxf.layer = "0"
                moved += 1
        except Exception:
            pass
    doc.layers.remove(layer_name)
    _touch(drawing)
    return f"✅ Layer '{layer_name}' deleted; {moved} entities moved to '0'."


# ── Drawing primitives ─────────────────────────────────────────────────────


@tool
def draw_line(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a line segment.

    Args:
        drawing: Drawing label.
        x1, y1: Start point coordinates.
        x2, y2: End point coordinates.
        layer: Target layer name.
        color: Optional ACI color override.
    """
    _add_line(_get_drawing(drawing), x1, y1, x2, y2, layer, color)
    _touch(drawing)
    return f"✅ Line ({x1},{y1}) → ({x2},{y2}) on layer '{layer}'."


@tool
def draw_circle(
    drawing: str,
    cx: float, cy: float,
    radius: float,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a circle.

    Args:
        drawing: Drawing label.
        cx, cy: Center point.
        radius: Circle radius.
        layer: Target layer name.
        color: Optional ACI color.
    """
    _add_circle(_get_drawing(drawing), cx, cy, radius, layer, color)
    _touch(drawing)
    return f"✅ Circle center=({cx},{cy}) r={radius} on '{layer}'."


@tool
def draw_arc(
    drawing: str,
    cx: float, cy: float,
    radius: float,
    start_angle: float,
    end_angle: float,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw an arc (part of a circle).

    Args:
        drawing: Drawing label.
        cx, cy: Center point.
        radius: Arc radius.
        start_angle: Start angle in degrees.
        end_angle: End angle in degrees.
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_arc(_get_drawing(drawing), cx, cy, radius, start_angle, end_angle, layer, color)
    _touch(drawing)
    return f"✅ Arc center=({cx},{cy}) r={radius} {start_angle}°→{end_angle}° on '{layer}'."


@tool
def draw_rectangle(
    drawing: str,
    x: float, y: float,
    width: float, height: float,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a rectangle (as a closed lightweight polyline).

    Args:
        drawing: Drawing label.
        x, y: Bottom-left corner.
        width: Rectangle width.
        height: Rectangle height.
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_rect(_get_drawing(drawing), x, y, width, height, layer, color)
    _touch(drawing)
    return f"✅ Rectangle ({x},{y}) {width}×{height} on '{layer}'."


@tool
def draw_polygon(
    drawing: str,
    cx: float, cy: float,
    radius: float,
    sides: int = 6,
    rotation: float = 0.0,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a regular polygon (as a closed polyline).

    Args:
        drawing: Drawing label.
        cx, cy: Center of the polygon.
        radius: Circumscribed radius.
        sides: Number of sides (3=triangle, 4=square, 6=hexagon …).
        rotation: Rotation angle in degrees.
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_polygon(_get_drawing(drawing), cx, cy, radius, sides, rotation, layer, color)
    _touch(drawing)
    return f"✅ {sides}-sided polygon center=({cx},{cy}) r={radius} on '{layer}'."


@tool
def draw_polyline(
    drawing: str,
    points_json: str,
    closed: bool = False,
    layer: str = "0",
    color: int | None = None,
    is_closed: bool | None = None,
) -> str:
    """Draw a lightweight polyline through a list of points.

    Args:
        drawing: Drawing label.
        points_json: JSON array of [x, y] pairs, e.g. "[[0,0],[10,0],[10,10]]".
        closed: Whether to close the polyline.
        layer: Target layer.
        color: Optional ACI color.
        is_closed: Deprecated alias for `closed` (README/SKILL used this name —
            it used to be silently dropped, producing an open polyline).
    """
    closed = bool(closed) or bool(is_closed)
    try:
        pts = _points(points_json)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ points_json must be a JSON array of [x, y] pairs ({e})."
    _add_polyline(_get_drawing(drawing), pts, closed, layer, color)
    _touch(drawing)
    return f"✅ Polyline with {len(pts)} vertices ({'closed' if closed else 'open'}) on '{layer}'."


@tool
def draw_spline(
    drawing: str,
    points_json: str,
    degree: int = 3,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a B-spline curve through control/fit points.

    Args:
        drawing: Drawing label.
        points_json: JSON array of [x, y] fit points.
        degree: Spline degree (default 3 = cubic).
        layer: Target layer.
        color: Optional ACI color.
    """
    try:
        fit_points = _points(points_json)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ points_json must be a JSON array of [x, y] pairs ({e})."
    _add_spline(_get_drawing(drawing), fit_points, degree, layer, color)
    _touch(drawing)
    return f"✅ Spline (degree={degree}) with {len(fit_points)} points on '{layer}'."


@tool
def draw_ellipse(
    drawing: str,
    cx: float, cy: float,
    major_axis_x: float, major_axis_y: float,
    ratio: float = 0.5,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw an ellipse.

    Args:
        drawing: Drawing label.
        cx, cy: Center point.
        major_axis_x, major_axis_y: Major axis endpoint relative to center.
        ratio: Minor-to-major axis ratio (0 < ratio ≤ 1).
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_ellipse(
        _get_drawing(drawing), cx, cy, major_axis_x, major_axis_y, ratio, layer, color
    )
    _touch(drawing)
    return f"✅ Ellipse center=({cx},{cy}) on '{layer}'."


# ── Text & Annotations ─────────────────────────────────────────────────────


@tool
def add_text(
    drawing: str,
    text: str,
    x: float, y: float,
    height: float = 2.5,
    rotation: float = 0,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Add single-line text.

    Args:
        drawing: Drawing label.
        text: The text string.
        x, y: Insertion point.
        height: Text height in drawing units.
        rotation: Text rotation in degrees.
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_text(_get_drawing(drawing), text, x, y, height, rotation, layer, color)
    _touch(drawing)
    return f"✅ Text '{text}' at ({x},{y}) height={height} on '{layer}'."


@tool
def add_mtext(
    drawing: str,
    text: str,
    x: float, y: float,
    width: float = 100,
    height: float = 2.5,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Add multi-line text (MTEXT) with word wrap.

    Args:
        drawing: Drawing label.
        text: The text content (supports \\P for newlines).
        x, y: Insertion point.
        width: Text box width for word wrapping.
        height: Character height.
        layer: Target layer.
        color: Optional ACI color.
    """
    _add_mtext(_get_drawing(drawing), text, x, y, width, height, layer, color)
    _touch(drawing)
    return f"✅ MText at ({x},{y}) width={width} on '{layer}'."


@tool
def add_dimension_linear(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    distance: float = 0.0,
    angle: float = 0,
    layer: str = "0",
    text_height: float = 0.0,
) -> str:
    """Add a linear dimension between two points.

    Args:
        drawing: Drawing label.
        x1, y1: First measurement point.
        x2, y2: Second measurement point.
        distance: Offset of the dimension line away from the measured segment;
            positive = below/left, 0 = 3.5× the text height.
        angle: Rotation angle of the dimension (0=horizontal, 90=vertical).
        layer: Target layer (now honoured — it used to always land on '0').
        text_height: Annotation height in drawing units; 0 = auto from $INSUNITS.
    """
    _add_dim(_get_drawing(drawing), x1, y1, x2, y2, distance, angle, layer, text_height)
    _touch(drawing)
    return f"✅ Dimension ({x1},{y1})→({x2},{y2}) on '{layer}'."


# ── Hatch / Fill ───────────────────────────────────────────────────────────


@tool
def add_hatch(
    drawing: str,
    points_json: str,
    pattern: str = "ANSI31",
    scale: float = 1.0,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Add a hatch pattern fill inside a closed boundary.

    Args:
        drawing: Drawing label.
        points_json: JSON array of [x,y] boundary vertices (auto-closed).
        pattern: Hatch pattern name (e.g. "ANSI31", "SOLID", "BRICK", "GRAVEL").
        scale: Pattern scale factor.
        layer: Target layer.
        color: Optional ACI color (None = BYLAYER; color=0 is now preserved).
    """
    try:
        pts = _points(points_json)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ points_json must be a JSON array of [x, y] pairs ({e})."
    _add_hatch(_get_drawing(drawing), pts, pattern, scale, layer, color)
    _touch(drawing)
    return f"✅ Hatch '{pattern}' with {len(pts)} boundary points on '{layer}'."


# ── Blocks ─────────────────────────────────────────────────────────────────


@tool
def create_block(drawing: str, block_name: str) -> str:
    """Create a new empty block definition (add entities separately).

    Args:
        drawing: Drawing label.
        block_name: Unique name for the block.
    """
    doc = _get_drawing(drawing)
    if block_name in doc.blocks:
        return f"⚠️ Block '{block_name}' already exists."
    doc.blocks.new(name=block_name)
    _touch(drawing)
    return f"✅ Block '{block_name}' created (empty). Add entities with add_entity_to_block."


@tool
def add_entity_to_block(
    drawing: str,
    block_name: str,
    entity_type: str,
    params_json: str,
) -> str:
    """Add a geometric entity to a block definition.

    Args:
        drawing: Drawing label.
        block_name: Target block.
        entity_type: One of "line", "circle", "arc", "lwpolyline", "text".
        params_json: JSON dict of params. E.g. for line: {"start":[0,0],"end":[10,10]};
                     circle: {"center":[0,0],"radius":5};
                     text: {"text":"Hello","insert":[0,0],"height":2.5}.
    """
    doc = _get_drawing(drawing)
    try:
        params = json.loads(params_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"
    try:
        _add_block_entity(doc, block_name, entity_type, params)
    except ValueError as e:
        return f"❌ {e}"
    except KeyError as e:
        return f"❌ Missing parameter {e} for entity_type '{entity_type}'."
    _touch(drawing)
    return f"✅ Added {str(entity_type).lower()} to block '{block_name}'."


@tool
def insert_block(
    drawing: str,
    block_name: str,
    x: float, y: float,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    rotation: float = 0,
    layer: str = "0",
) -> str:
    """Insert a block reference into modelspace.

    Args:
        drawing: Drawing label.
        block_name: Name of the block to insert.
        x, y: Insertion point.
        scale_x, scale_y: Scale factors.
        rotation: Rotation in degrees.
        layer: Target layer.
    """
    doc = _get_drawing(drawing)
    if block_name not in doc.blocks:
        return f"❌ Block '{block_name}' not found."
    _ensure_layer(doc, layer)
    doc.modelspace().add_blockref(
        block_name,
        insert=(x, y),
        dxfattribs={
            "xscale": scale_x,
            "yscale": scale_y,
            "rotation": rotation,
            "layer": layer,
        },
    )
    _touch(drawing)
    return f"✅ Inserted block '{block_name}' at ({x},{y}) on '{layer}'."


@tool(readonly=True)
def list_blocks(drawing: str) -> str:
    """List all block definitions in a drawing.

    Args:
        drawing: Drawing label.
    """
    doc = _get_drawing(drawing)
    blocks = [b.name for b in doc.blocks if not b.name.startswith("*")]
    if not blocks:
        return "No user-defined blocks."
    return "Blocks: " + ", ".join(blocks)


# ── Query & Analysis ──────────────────────────────────────────────────────


@tool(readonly=True)
def list_entities(drawing: str, layer: str = "") -> str:
    """List entities in modelspace, optionally filtered by layer.

    Args:
        drawing: Drawing label.
        layer: If set, only list entities on this layer.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    lines = []
    for i, entity in enumerate(msp):
        if layer and entity.dxf.layer != layer:
            continue
        lines.append(_describe_entity(i, entity))
    if not lines:
        return "No entities found" + (f" on layer '{layer}'" if layer else "") + "."
    more = f"\n  … {len(lines) - 200} more (use inspect_drawing for a summary)" if len(lines) > 200 else ""
    return f"Entities ({len(lines)}):\n" + "\n".join(lines[:200]) + more


@tool
def delete_entity(drawing: str, index: int = -1, handle: str = "") -> str:
    """Delete an entity by index in modelspace OR by its DXF handle.

    Prefer `handle`: indexes shift after every deletion, handles are stable.

    Args:
        drawing: Drawing label.
        index: Entity index from list_entities (shifts after each deletion).
        handle: DXF handle from list_entities, e.g. "2F". Preferred over index.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    entity = None
    if handle:
        entity = doc.entitydb.get(handle)
        if entity is None:
            return f"❌ No entity with handle '{handle}'."
    else:
        entities = list(msp)
        if not entities:
            return "❌ Modelspace is empty — nothing to delete."
        if index < 0 or index >= len(entities):
            return f"❌ Index {index} out of range (0–{len(entities)-1})."
        entity = entities[index]
    # Find the layout that owns the entity (modelspace, paper space or a block).
    # Describe it BEFORE deletion: ezdxf may invalidate the entity on delete.
    desc = f"{entity.dxftype()} #{entity.dxf.handle}"
    for layout in (msp, *doc.layouts, *doc.blocks):
        try:
            if entity in layout:
                layout.delete_entity(entity)
                _touch(drawing)
                return f"✅ Deleted {desc}."
        except Exception:
            continue
    return f"❌ Entity {desc} is not in any layout."


@tool(readonly=True)
def calculate_area(drawing: str, points_json: str) -> str:
    """Calculate the area of a closed polygon defined by its vertices.

    Args:
        drawing: Drawing label (not used, but keeps the API consistent).
        points_json: JSON array of [x, y] vertices forming a closed polygon.
    """
    try:
        pts = _points(points_json)
    except (ValueError, json.JSONDecodeError) as e:
        return f"❌ points_json must be a JSON array of [x, y] pairs ({e})."
    n = len(pts)
    if n < 3:
        return "❌ Need at least 3 points."
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    return f"✅ Area = {abs(area) / 2.0:.4f} square units."


@tool(readonly=True)
def get_drawing_extents(drawing: str) -> str:
    """Get the bounding box of all entities in the drawing.

    Args:
        drawing: Drawing label.
    """
    doc = _get_drawing(drawing)
    box = _extents(doc, drawing)
    if not box.has_data:
        return "Drawing is empty — no extents."
    return (
        f"Extents:\n"
        f"  Min: ({box.extmin.x:.4f}, {box.extmin.y:.4f})\n"
        f"  Max: ({box.extmax.x:.4f}, {box.extmax.y:.4f})\n"
        f"  Size: {box.size.x:.4f} × {box.size.y:.4f}"
    )


# ── Units ──────────────────────────────────────────────────────────────────

_UNITS_MAP = {
    "unitless": 0, "inches": 1, "feet": 2, "miles": 3,
    "millimeters": 4, "centimeters": 5, "meters": 6,
    "kilometers": 7, "microinches": 8, "mils": 9,
    "yards": 10, "angstroms": 11, "nanometers": 12,
    "microns": 13, "decimeters": 14,
}


@tool
def set_units(drawing: str, units: str = "millimeters") -> str:
    """Set the drawing units.

    Args:
        drawing: Drawing label.
        units: One of: unitless, inches, feet, miles, millimeters, centimeters,
               meters, kilometers, yards, decimeters.
    """
    doc = _get_drawing(drawing)
    key = units.lower()
    if key not in _UNITS_MAP:
        return f"❌ Unknown unit '{units}'. Valid: {', '.join(_UNITS_MAP.keys())}"
    doc.header["$INSUNITS"] = _UNITS_MAP[key]
    return f"✅ Drawing units set to {units}."


# ── Export / Render ────────────────────────────────────────────────────────


def _render_svg(doc: Drawing, path: Path) -> None:
    """Native ezdxf SVG backend: ~8x faster than matplotlib, no font cache."""
    if len(doc.modelspace()) == 0:
        raise ValueError("drawing is empty — nothing to render")
    backend = svg.SVGBackend()
    config = Configuration(background_policy=BackgroundPolicy.WHITE)
    Frontend(RenderContext(doc), backend, config=config).draw_layout(
        doc.modelspace(), finalize=True
    )
    path.write_text(backend.get_string(Page(0, 0)), encoding="utf-8")


def _render_png(doc: Drawing, path: Path, dpi: int = 150) -> None:
    import matplotlib

    matplotlib.use("Agg")  # headless: never try to open a window
    from ezdxf.addons.drawing.matplotlib import qsave

    # matplotlib keeps global figure state — one renderer at a time.
    with _render_lock:
        qsave(doc.modelspace(), str(path), dpi=int(dpi), bg="#FFFFFF")


def _write_outputs(
    doc: Drawing,
    drawing: str,
    save: bool = True,
    svg: bool = True,
    png: bool = False,
    dpi: int = 150,
    file_path: str = "",
) -> list[str]:
    """Save DXF and render previews; return one status line per output.

    Shared by `finish` and by `apply_ops(finish=True)` so a whole drawing fits
    into a single round trip.
    """
    lines: list[str] = [f"✅ Finished '{drawing}' ({len(doc.modelspace())} entities):"]
    failures = 0
    if save:
        path = _resolve_out(file_path, drawing, "dxf")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            doc.saveas(str(path))
            _dirty.discard(drawing)
            lines.append(f"  DXF → {path}")
        except Exception as e:
            failures += 1
            lines.append(f"  ✗ DXF save failed: {e}")
    if svg:
        path = _resolve_out("", drawing, "svg")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _render_svg(doc, path)
            lines.append(f"  SVG → {path}")
        except Exception as e:
            failures += 1
            lines.append(f"  ✗ SVG export failed: {e}")
    if png:
        path = _resolve_out("", drawing, "png")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _render_png(doc, path, dpi)
            lines.append(f"  PNG → {path}")
        except Exception as e:
            failures += 1
            lines.append(f"  ✗ PNG export failed: {e}")
    if failures:
        lines.append(f"⚠️ {failures} step(s) failed.")
    return lines


@tool(readonly=True)
def export_svg(drawing: str, file_path: str = "") -> str:
    """Export the drawing to SVG for quick preview (fast native renderer).

    Args:
        drawing: Drawing label.
        file_path: Output path. Defaults to <output_dir>/<drawing>.svg.
    """
    doc = _get_drawing(drawing)
    path = _resolve_out(file_path, drawing, "svg")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_svg(doc, path)
    except Exception as e:
        return f"❌ SVG export failed: {e}"
    return f"✅ Exported SVG → {path}"


@tool(readonly=True)
def export_png(drawing: str, file_path: str = "", dpi: int = 150) -> str:
    """Export the drawing to PNG image (requires matplotlib).

    Args:
        drawing: Drawing label.
        file_path: Output path. Defaults to <output_dir>/<drawing>.png.
        dpi: Resolution in dots per inch.
    """
    doc = _get_drawing(drawing)
    path = _resolve_out(file_path, drawing, "png")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_png(doc, path, dpi)
    except Exception as e:
        return f"❌ PNG export failed: {e}"
    return f"✅ Exported PNG → {path}"


@tool(readonly=True)
def view_drawing(drawing: str, dpi: int = 110) -> Image:
    """Render the drawing and return it as an inline image, so you can LOOK at
    the result without a separate file-open round trip.

    Args:
        drawing: Drawing label.
        dpi: Raster resolution (110 is a good preview size).
    """
    doc = _get_drawing(drawing)
    _ensure_output_dir()
    path = _output_dir / ".preview" / f"{drawing}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    _render_png(doc, path, dpi)
    return Image(path=str(path))


# ── Convenience: template shapes ───────────────────────────────────────────


@tool
def draw_wall(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    thickness: float = 0.24,
    layer: str = "Walls",
) -> str:
    """Draw a wall as a filled rectangle between two points (for floor plans).

    Args:
        drawing: Drawing label.
        x1, y1: Wall start center-line point.
        x2, y2: Wall end center-line point.
        thickness: Wall thickness in drawing units.
        layer: Target layer (auto-created if needed).
    """
    try:
        _add_wall(_get_drawing(drawing), x1, y1, x2, y2, thickness, layer)
    except ValueError as e:
        return f"❌ {e}"
    _touch(drawing)
    return f"✅ Wall ({x1},{y1})→({x2},{y2}) t={thickness} on '{layer}'."


@tool
def draw_door(
    drawing: str,
    x: float, y: float,
    width: float = 0.9,
    direction: str = "right",
    layer: str = "Doors",
) -> str:
    """Draw a door symbol (arc + lines) for floor plans.

    Args:
        drawing: Drawing label.
        x, y: Hinge point.
        width: Door width (swing radius).
        direction: "left" or "right" swing direction.
        layer: Target layer.
    """
    _add_door(_get_drawing(drawing), x, y, width, direction, layer)
    _touch(drawing)
    return f"✅ Door at ({x},{y}) width={width} swing={direction} on '{layer}'."


@tool
def draw_window(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    thickness: float = 0.24,
    layer: str = "Windows",
) -> str:
    """Draw a window symbol (three parallel lines) for floor plans.

    Args:
        drawing: Drawing label.
        x1, y1: Window start point.
        x2, y2: Window end point.
        thickness: Window frame width in drawing units (was hard-coded 0.24).
        layer: Target layer.
    """
    try:
        _add_window(_get_drawing(drawing), x1, y1, x2, y2, thickness, layer)
    except ValueError as e:
        return f"❌ {e}"
    _touch(drawing)
    return f"✅ Window ({x1},{y1})→({x2},{y2}) t={thickness} on '{layer}'."


# ── Batch / composite tools (the fast path) ────────────────────────────────

_OP_ALIASES = {
    "rectangle": "rect",
    "dimension": "dim",
    "polygon": "polygon",
    "layer_create": "layer",
    "block_entity": "block_entity",
}


def _apply_one(doc: Drawing, op: dict[str, Any]) -> None:
    """Execute a single op from apply_ops. Raises ValueError on bad input."""
    if not isinstance(op, dict):
        raise ValueError(f"op must be an object, got {type(op).__name__}")
    kind = str(op.get("op") or op.get("type") or "").strip().lower()
    kind = _OP_ALIASES.get(kind, kind)
    layer = str(op.get("layer", "0"))
    color = op.get("color")

    if kind == "layer":
        name = str(op.get("name") or op.get("layer_name") or "")
        if not name:
            raise ValueError("layer op needs 'name'")
        _ensure_layer(doc, name, int(op.get("color", 7)))
        return
    if kind == "units":
        units = str(op.get("units", "")).lower()
        if units not in _UNITS_MAP:
            raise ValueError(f"unknown unit '{units}'")
        doc.header["$INSUNITS"] = _UNITS_MAP[units]
        return
    if kind == "line":
        _add_line(doc, op["x1"], op["y1"], op["x2"], op["y2"], layer, color)
    elif kind == "circle":
        _add_circle(doc, op["cx"], op["cy"], op["radius"], layer, color)
    elif kind == "arc":
        _add_arc(
            doc, op["cx"], op["cy"], op["radius"],
            op["start_angle"], op["end_angle"], layer, color,
        )
    elif kind == "rect":
        _add_rect(doc, op["x"], op["y"], op["width"], op["height"], layer, color)
    elif kind == "polygon":
        _add_polygon(
            doc, op["cx"], op["cy"], op["radius"], int(op.get("sides", 6)),
            float(op.get("rotation", 0.0)), layer, color,
        )
    elif kind == "polyline":
        _add_polyline(doc, op["points"], op.get("closed", False), layer, color)
    elif kind == "spline":
        _add_spline(doc, op["points"], int(op.get("degree", 3)), layer, color)
    elif kind == "ellipse":
        _add_ellipse(
            doc, op["cx"], op["cy"], op["major_axis_x"], op["major_axis_y"],
            float(op.get("ratio", 0.5)), layer, color,
        )
    elif kind == "text":
        _add_text(
            doc, op["text"], op["x"], op["y"], float(op.get("height", 2.5)),
            float(op.get("rotation", 0)), layer, color,
        )
    elif kind == "mtext":
        _add_mtext(
            doc, op["text"], op["x"], op["y"], float(op.get("width", 100)),
            float(op.get("height", 2.5)), layer, color,
        )
    elif kind == "dim":
        _add_dim(
            doc, op["x1"], op["y1"], op["x2"], op["y2"],
            float(op.get("distance", 0.0)), float(op.get("angle", 0)), layer,
            float(op.get("text_height", 0.0)),
        )
    elif kind == "hatch":
        _add_hatch(
            doc, op["points"], str(op.get("pattern", "ANSI31")),
            float(op.get("scale", 1.0)), layer, color,
        )
    elif kind == "wall":
        # floor-plan helpers have their own layer defaults when none is given
        _add_wall(
            doc, op["x1"], op["y1"], op["x2"], op["y2"],
            float(op.get("thickness", 0.24)),
            str(op.get("layer") or "Walls"),
        )
    elif kind == "door":
        _add_door(
            doc, op["x"], op["y"], float(op.get("width", 0.9)),
            str(op.get("direction", "right")), str(op.get("layer") or "Doors"),
        )
    elif kind == "window":
        _add_window(
            doc, op["x1"], op["y1"], op["x2"], op["y2"],
            float(op.get("thickness", 0.24)), str(op.get("layer") or "Windows"),
        )
    elif kind == "block":
        name = str(op.get("name") or op.get("block_name") or "")
        if not name:
            raise ValueError("block op needs 'name'")
        if name not in doc.blocks:
            doc.blocks.new(name=name)
    elif kind == "block_entity":
        _add_block_entity(
            doc, op["block_name"], op["entity_type"], op.get("params") or {}
        )
    elif kind == "insert":
        block_name = str(op.get("block_name") or op.get("name") or "")
        if block_name not in doc.blocks:
            raise ValueError(f"block '{block_name}' not found")
        _ensure_layer(doc, layer)
        doc.modelspace().add_blockref(
            block_name,
            insert=(op["x"], op["y"]),
            dxfattribs={
                "xscale": float(op.get("scale_x", 1.0)),
                "yscale": float(op.get("scale_y", 1.0)),
                "rotation": float(op.get("rotation", 0)),
                "layer": layer,
            },
        )
    elif kind == "delete":
        handle = str(op.get("handle", ""))
        if not handle:
            raise ValueError("delete op needs 'handle'")
        entity = doc.entitydb.get(handle)
        if entity is None:
            raise ValueError(f"no entity with handle '{handle}'")
        for layout in (doc.modelspace(), *doc.layouts, *doc.blocks):
            try:
                if entity in layout:
                    layout.delete_entity(entity)
                    return
            except Exception:
                continue
        raise ValueError(f"entity #{handle} is not in any layout")
    elif kind == "save":
        doc.saveas(str(_resolve_out(op.get("file_path", ""), "drawing", "dxf")))
    else:
        raise ValueError(f"unknown op '{kind}'")


@tool
def apply_ops(
    drawing: str, ops: list[dict] | str, finish: bool = False, create: bool = True
) -> str:
    """Execute MANY drawing operations in ONE call — use this instead of dozens
    of single-entity tools. Each failed op is reported but does not abort the batch.

    Args:
        drawing: Drawing label.
        finish: Also save the .dxf and export the .svg right away — the whole
                plan in a single round trip (same as calling finish() after).
        create: Auto-create the drawing if it does not exist yet, so a whole
                plan needs no separate new_drawing call (default True).
        ops: List of op objects (or a JSON array string). Each op has "op" plus
             its parameters. Supported ops:
               {"op":"layer","name":"Walls","color":1}
               {"op":"units","units":"meters"}
               {"op":"line","x1":0,"y1":0,"x2":4,"y2":0,"layer":"Walls","color":1}
               {"op":"circle","cx":2,"cy":2,"radius":0.5}
               {"op":"arc","cx":0,"cy":0,"radius":1,"start_angle":0,"end_angle":90}
               {"op":"rect","x":0,"y":0,"width":4,"height":3}
               {"op":"polygon","cx":0,"cy":0,"radius":1,"sides":6,"rotation":0}
               {"op":"polyline","points":[[0,0],[1,1]],"closed":false}
               {"op":"spline","points":[[0,0],[1,2],[3,1]],"degree":3}
               {"op":"ellipse","cx":0,"cy":0,"major_axis_x":2,"major_axis_y":0,"ratio":0.5}
               {"op":"text","text":"Living room","x":1,"y":1,"height":0.3}
               {"op":"mtext","text":"Line1\\PLine2","x":0,"y":0,"width":5}
               {"op":"dim","x1":0,"y1":0,"x2":4,"y2":0,"distance":0.5,"angle":0}
               {"op":"hatch","points":[[0,0],[1,0],[1,1]],"pattern":"ANSI31"}
               {"op":"wall","x1":0,"y1":0,"x2":4,"y2":0,"thickness":0.24}
               {"op":"door","x":1,"y":0,"width":0.9,"direction":"right"}
               {"op":"window","x1":2,"y1":0,"x2":3,"y2":0,"thickness":0.24}
               {"op":"block","name":"DESK"}
               {"op":"block_entity","block_name":"DESK","entity_type":"circle",
                "params":{"center":[0,0],"radius":0.5}}
               {"op":"insert","block_name":"DESK","x":1,"y":1,"rotation":0}
               {"op":"delete","handle":"2F"}
    """
    doc = _drawings.get(drawing)
    created = False
    if doc is None:
        if not create:
            return (
                f"❌ No drawing named '{drawing}'. Open drawings: "
                f"{list(_drawings.keys()) or '(none)'}. Call new_drawing first, "
                f"or leave create=True to auto-create it."
            )
        doc = ezdxf.new("R2018")
        doc.header["$INSUNITS"] = 6
        _drawings[drawing] = doc
        _bbox_caches.pop(drawing, None)
        created = True
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except json.JSONDecodeError as e:
            return f"❌ ops JSON parse error: {e}"
    if not isinstance(ops, list):
        return "❌ ops must be a list of objects."

    ok, failures = 0, []
    for i, op in enumerate(ops):
        try:
            _apply_one(doc, op)
            ok += 1
        except Exception as e:  # one bad op must not kill the batch
            name = (op.get("op") or op.get("type")) if isinstance(op, dict) else "?"
            failures.append(f"  ✗ #{i} {name}: {e}")
    _touch(drawing)
    total = len(doc.modelspace())
    head = f"{'✅' if not failures else '⚠️'} apply_ops: {ok}/{len(ops)} ok"
    tail = f" (drawing '{drawing}', {total} entities{' — created' if created else ''})"
    out = head + tail
    if failures:
        out += "\n" + "\n".join(failures[:10])
    if finish:
        out += "\n" + "\n".join(_write_outputs(doc, drawing, True, True, False, 150, ""))
    return out


@tool
def finish(
    drawing: str,
    save: bool = True,
    svg: bool = True,
    png: bool = False,
    dpi: int = 150,
    file_path: str = "",
) -> str:
    """Save the DXF and export previews in ONE call (replaces save + export_svg
    + export_png round trips). Returns every output path.

    Args:
        drawing: Drawing label.
        save: Write the .dxf file.
        svg: Render a .svg preview (fast, native renderer).
        png: Render a .png preview (slower, needs matplotlib).
        dpi: PNG resolution.
        file_path: Override the DXF output path.
    """
    doc = _get_drawing(drawing)
    return "\n".join(_write_outputs(doc, drawing, save, svg, png, dpi, file_path))


@tool(readonly=True)
def inspect_drawing(drawing: str, max_entities: int = 20) -> str:
    """Summarise a drawing in ONE call: units, entity counts by type, layers,
    blocks, extents and the first N entities.

    Args:
        drawing: Drawing label.
        max_entities: How many entities to list (default 20).
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    entities = list(msp)
    counts: dict[str, int] = {}
    for e in entities:
        counts[e.dxftype()] = counts.get(e.dxftype(), 0) + 1
    layers = [layer.dxf.name for layer in doc.layers]
    blocks = [b.name for b in doc.blocks if not b.name.startswith("*")]
    units = doc.header.get("$INSUNITS", 0)

    lines = [
        f"Drawing '{drawing}' — DXF {doc.dxfversion}, $INSUNITS={units}",
        f"  Entities: {len(entities)}"
        + ("  (" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ")" if counts else ""),
        f"  Layers ({len(layers)}): " + ", ".join(layers),
        f"  Blocks ({len(blocks)}): " + (", ".join(blocks) if blocks else "—"),
    ]
    box = _extents(doc, drawing)
    if box.has_data:
        lines.append(
            f"  Extents: ({box.extmin.x:.3f}, {box.extmin.y:.3f}) → "
            f"({box.extmax.x:.3f}, {box.extmax.y:.3f}), "
            f"size {box.size.x:.3f} × {box.size.y:.3f}"
        )
    else:
        lines.append("  Extents: (empty)")
    if entities:
        shown = min(max(int(max_entities), 0), len(entities))
        lines.append(f"  First {shown} entities:")
        lines.extend(_describe_entity(i, e) for i, e in enumerate(entities[:shown]))
        if len(entities) > shown:
            lines.append(f"  … {len(entities) - shown} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shutdown handling — never lose a drawing because the server was restarted
# ---------------------------------------------------------------------------


def _autosave() -> None:
    if not _dirty:
        return
    saved: set[str] = set()
    try:
        root = _output_dir / ".autosave"
        root.mkdir(parents=True, exist_ok=True)
        for name in sorted(_dirty):
            doc = _drawings.get(name)
            if doc is None:
                saved.add(name)  # nothing left to persist
                continue
            try:
                with _lock.read():
                    doc.saveas(str(root / f"{name}.dxf"))
                saved.add(name)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _dirty.difference_update(saved)


atexit.register(_autosave)


def _install_signal_handlers() -> None:
    def _bail(signum, frame):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _bail)
        except Exception:
            pass


_warm_done = threading.Event()


def _warm_renderers() -> None:
    """Warm both renderers off the critical path so the first export is cheap:
    matplotlib's font cache (6–7 s cold) and the SVG backend's first-run setup
    (~120 ms). Runs on a background daemon thread."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager  # noqa: F401  (triggers cache build)
    except Exception:
        pass
    try:
        import tempfile
        from pathlib import Path as _Path

        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_line((0, 0), (1, 1))
        msp.add_text("warm", height=0.2)  # text pulls in the font machinery
        with tempfile.TemporaryDirectory() as tmp:
            _render_svg(doc, _Path(tmp) / "warm.svg")
    except Exception:
        pass
    finally:
        _warm_done.set()


def _start_warm() -> None:
    """Start the background renderer warm-up (also runs for imported servers,
    benches and tests). AUTOCAD_MCP_WARM=0 disables it."""
    if os.environ.get("AUTOCAD_MCP_WARM", "1") == "0":
        _warm_done.set()
        return
    # delay a little so the warm-up does not compete with the MCP handshake
    try:
        delay = float(os.environ.get("AUTOCAD_MCP_WARM_DELAY", "1.0"))
    except ValueError:
        delay = 1.0
    timer = threading.Timer(max(0.0, delay), _warm_renderers)
    timer.daemon = True
    timer.start()


_start_warm()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _install_signal_handlers()
    mcp.run()

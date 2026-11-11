#!/usr/bin/env python3
"""AutoCAD MCP Server — DXF file operations via ezdxf.

This MCP server lets an AI agent create, edit, query, and export
AutoCAD-compatible DXF drawings entirely on macOS (no AutoCAD installation
required).  It uses the `ezdxf` library for all geometry operations.

Typical workflow:
  1. new_drawing / open_drawing   → load or create a drawing
  2. draw_*, add_text, …          → add geometry
  3. save_drawing / export_image  → persist or visualise
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.document import Drawing
from ezdxf.math import Vec2
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP("autocad-mcp")

# In-memory store of open drawings keyed by a user-chosen name.
_drawings: dict[str, Drawing] = {}

# Default output directory
_output_dir = Path(os.environ.get("AUTOCAD_MCP_OUTPUT", Path.home() / "Documents" / "AutoCAD_MCP"))


def _ensure_output_dir() -> Path:
    _output_dir.mkdir(parents=True, exist_ok=True)
    return _output_dir


def _get_drawing(name: str) -> Drawing:
    if name not in _drawings:
        raise ValueError(
            f"No drawing named '{name}' is open. "
            f"Open drawings: {list(_drawings.keys()) or '(none)'}. "
            f"Use new_drawing or open_drawing first."
        )
    return _drawings[name]


# ── Drawing lifecycle ──────────────────────────────────────────────────────

@mcp.tool()
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
    return f"✅ Created new drawing '{name}' (DXF {dxf_version})."


@mcp.tool()
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
    return f"✅ Opened '{path.name}' as '{name}'."


@mcp.tool()
def save_drawing(name: str, file_path: str = "") -> str:
    """Save the drawing to a DXF file.

    Args:
        name: Drawing label.
        file_path: Where to save. Defaults to <output_dir>/<name>.dxf.
    """
    doc = _get_drawing(name)
    if not file_path:
        _ensure_output_dir()
        file_path = str(_output_dir / f"{name}.dxf")
    path = Path(file_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.saveas(str(path))
    except Exception as e:
        return f"❌ Save failed: {e}"
    return f"✅ Saved '{name}' → {path}"


@mcp.tool()
def close_drawing(name: str) -> str:
    """Close a drawing and remove it from memory (does NOT auto-save).

    Args:
        name: Drawing label to close.
    """
    if name not in _drawings:
        return f"⚠️ No drawing named '{name}' is open."
    del _drawings[name]
    return f"✅ Closed drawing '{name}'."


@mcp.tool()
def list_drawings() -> str:
    """List all currently open drawings."""
    if not _drawings:
        return "No drawings are currently open."
    lines = []
    for n, doc in _drawings.items():
        ver = doc.dxfversion
        msp = doc.modelspace()
        count = len(list(msp))
        lines.append(f"  • {n}  (DXF {ver}, {count} entities in modelspace)")
    return "Open drawings:\n" + "\n".join(lines)


# ── Layer management ───────────────────────────────────────────────────────

@mcp.tool()
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
    doc.layers.add(layer_name, color=color, linetype=linetype)
    return f"✅ Layer '{layer_name}' created (color={color}, linetype={linetype})."


@mcp.tool()
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


@mcp.tool()
def delete_layer(drawing: str, layer_name: str) -> str:
    """Delete a layer (entities on it are moved to layer '0').

    Args:
        drawing: Drawing label.
        layer_name: Layer to delete.
    """
    doc = _get_drawing(drawing)
    if layer_name == "0":
        return "❌ Cannot delete the default layer '0'."
    if layer_name not in doc.layers:
        return f"⚠️ Layer '{layer_name}' does not exist."
    # Move entities off the layer first
    msp = doc.modelspace()
    for entity in msp:
        if entity.dxf.layer == layer_name:
            entity.dxf.layer = "0"
    doc.layers.remove(layer_name)
    return f"✅ Layer '{layer_name}' deleted; entities moved to '0'."


# ── Drawing primitives ─────────────────────────────────────────────────────

@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    msp.add_line((x1, y1), (x2, y2), dxfattribs=attribs)
    return f"✅ Line ({x1},{y1}) → ({x2},{y2}) on layer '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    msp.add_circle((cx, cy), radius, dxfattribs=attribs)
    return f"✅ Circle center=({cx},{cy}) r={radius} on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    msp.add_arc((cx, cy), radius, start_angle, end_angle, dxfattribs=attribs)
    return f"✅ Arc center=({cx},{cy}) r={radius} {start_angle}°→{end_angle}° on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    points = [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    ]
    msp.add_lwpolyline(points, close=True, dxfattribs=attribs)
    return f"✅ Rectangle ({x},{y}) {width}×{height} on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    rot_rad = math.radians(rotation)
    points = []
    for i in range(sides):
        angle = rot_rad + 2 * math.pi * i / sides
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        points.append((px, py))
    msp.add_lwpolyline(points, close=True, dxfattribs=attribs)
    return f"✅ {sides}-sided polygon center=({cx},{cy}) r={radius} on '{layer}'."


@mcp.tool()
def draw_polyline(
    drawing: str,
    points_json: str,
    closed: bool = False,
    layer: str = "0",
    color: int | None = None,
) -> str:
    """Draw a lightweight polyline through a list of points.

    Args:
        drawing: Drawing label.
        points_json: JSON array of [x, y] pairs, e.g. "[[0,0],[10,0],[10,10]]".
        closed: Whether to close the polyline.
        layer: Target layer.
        color: Optional ACI color.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    try:
        pts = json.loads(points_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"
    if not pts or not all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in pts):
        return "❌ points_json must be an array of [x, y] pairs."
    msp.add_lwpolyline(pts, close=closed, dxfattribs=attribs)
    return f"✅ Polyline with {len(pts)} vertices ({'closed' if closed else 'open'}) on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    try:
        pts = json.loads(points_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"
    fit_points = [(p[0], p[1]) for p in pts]
    msp.add_spline(fit_points, degree=degree, dxfattribs=attribs)
    return f"✅ Spline (degree={degree}) with {len(fit_points)} points on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    msp.add_ellipse(
        (cx, cy),
        major_axis=(major_axis_x, major_axis_y, 0),
        ratio=ratio,
        dxfattribs=attribs,
    )
    return f"✅ Ellipse center=({cx},{cy}) on '{layer}'."


# ── Text & Annotations ────────────────────────────────────────────────────

@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer, "rotation": rotation}
    if color is not None:
        attribs["color"] = color
    msp.add_text(text, height=height, dxfattribs=attribs).set_placement((x, y))
    return f"✅ Text '{text}' at ({x},{y}) height={height} on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer, "char_height": height, "width": width}
    if color is not None:
        attribs["color"] = color
    msp.add_mtext(text, dxfattribs=attribs).set_location((x, y))
    return f"✅ MText at ({x},{y}) width={width} on '{layer}'."


@mcp.tool()
def add_dimension_linear(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    distance: float = 5.0,
    angle: float = 0,
    layer: str = "0",
) -> str:
    """Add a linear dimension between two points.

    Args:
        drawing: Drawing label.
        x1, y1: First measurement point.
        x2, y2: Second measurement point.
        distance: Offset of dimension line from measured points.
        angle: Rotation angle of the dimension (0=horizontal, 90=vertical).
        layer: Target layer.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    if angle == 0:
        dim_y = max(y1, y2) + distance
        msp.add_linear_dim(
            base=(0, dim_y),
            p1=(x1, y1),
            p2=(x2, y2),
            angle=0,
            override={"dimtxt": 2.5},
        ).render()
    else:
        dim_x = max(x1, x2) + distance
        msp.add_linear_dim(
            base=(dim_x, 0),
            p1=(x1, y1),
            p2=(x2, y2),
            angle=90,
            override={"dimtxt": 2.5},
        ).render()
    return f"✅ Dimension ({x1},{y1})→({x2},{y2}) on '{layer}'."


# ── Hatch / Fill ───────────────────────────────────────────────────────────

@mcp.tool()
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
        color: Optional ACI color.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    attribs: dict[str, Any] = {"layer": layer}
    if color is not None:
        attribs["color"] = color
    try:
        pts = json.loads(points_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"
    hatch = msp.add_hatch(color=color if color else 7, dxfattribs=attribs)
    hatch.set_pattern_fill(pattern, scale=scale)
    hatch.paths.add_polyline_path([(p[0], p[1]) for p in pts], is_closed=True)
    return f"✅ Hatch '{pattern}' with {len(pts)} boundary points on '{layer}'."


# ── Blocks ─────────────────────────────────────────────────────────────────

@mcp.tool()
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
    return f"✅ Block '{block_name}' created (empty). Add entities with add_entity_to_block."


@mcp.tool()
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
    if block_name not in doc.blocks:
        return f"❌ Block '{block_name}' not found."
    blk = doc.blocks[block_name]
    try:
        p = json.loads(params_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"

    et = entity_type.lower()
    if et == "line":
        blk.add_line(tuple(p["start"]), tuple(p["end"]))
    elif et == "circle":
        blk.add_circle(tuple(p["center"]), p["radius"])
    elif et == "arc":
        blk.add_arc(tuple(p["center"]), p["radius"], p["start_angle"], p["end_angle"])
    elif et == "lwpolyline":
        blk.add_lwpolyline(p["points"], close=p.get("closed", False))
    elif et == "text":
        blk.add_text(p["text"], height=p.get("height", 2.5)).set_placement(tuple(p["insert"]))
    else:
        return f"❌ Unknown entity_type '{entity_type}'. Use: line, circle, arc, lwpolyline, text."
    return f"✅ Added {et} to block '{block_name}'."


@mcp.tool()
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
    msp = doc.modelspace()
    msp.add_blockref(
        block_name,
        insert=(x, y),
        dxfattribs={
            "xscale": scale_x,
            "yscale": scale_y,
            "rotation": rotation,
            "layer": layer,
        },
    )
    return f"✅ Inserted block '{block_name}' at ({x},{y}) on '{layer}'."


@mcp.tool()
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

@mcp.tool()
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
        etype = entity.dxftype()
        elayer = entity.dxf.layer
        info = f"  [{i}] {etype} layer='{elayer}'"
        if etype == "LINE":
            info += f" ({entity.dxf.start.x:.1f},{entity.dxf.start.y:.1f})→({entity.dxf.end.x:.1f},{entity.dxf.end.y:.1f})"
        elif etype == "CIRCLE":
            info += f" center=({entity.dxf.center.x:.1f},{entity.dxf.center.y:.1f}) r={entity.dxf.radius:.1f}"
        elif etype == "TEXT":
            info += f" '{entity.dxf.text}'"
        lines.append(info)
    if not lines:
        return "No entities found" + (f" on layer '{layer}'" if layer else "") + "."
    return f"Entities ({len(lines)}):\n" + "\n".join(lines[:100])  # cap at 100


@mcp.tool()
def delete_entity(drawing: str, index: int) -> str:
    """Delete an entity by its index in modelspace (see list_entities).

    Args:
        drawing: Drawing label.
        index: Entity index from list_entities.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    entities = list(msp)
    if index < 0 or index >= len(entities):
        return f"❌ Index {index} out of range (0–{len(entities)-1})."
    msp.delete_entity(entities[index])
    return f"✅ Deleted entity [{index}]."


@mcp.tool()
def calculate_area(drawing: str, points_json: str) -> str:
    """Calculate the area of a closed polygon defined by its vertices.

    Args:
        drawing: Drawing label (not used, but keeps the API consistent).
        points_json: JSON array of [x, y] vertices forming a closed polygon.
    """
    try:
        pts = json.loads(points_json)
    except json.JSONDecodeError as e:
        return f"❌ Invalid JSON: {e}"
    n = len(pts)
    if n < 3:
        return "❌ Need at least 3 points."
    # Shoelace formula
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    area = abs(area) / 2.0
    return f"✅ Area = {area:.4f} square units."


@mcp.tool()
def get_drawing_extents(drawing: str) -> str:
    """Get the bounding box of all entities in the drawing.

    Args:
        drawing: Drawing label.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    cache = ezdxf_bbox.Cache()
    box = ezdxf_bbox.extents(msp, cache=cache)
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

@mcp.tool()
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

@mcp.tool()
def export_svg(drawing: str, file_path: str = "") -> str:
    """Export the drawing to SVG for quick preview.

    Args:
        drawing: Drawing label.
        file_path: Output path. Defaults to <output_dir>/<name>.svg.
    """
    doc = _get_drawing(drawing)
    if not file_path:
        _ensure_output_dir()
        file_path = str(_output_dir / f"{drawing}.svg")
    path = Path(file_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from ezdxf.addons.drawing.matplotlib import qsave
        qsave(doc.modelspace(), str(path), bg="#FFFFFF")
    except Exception as e:
        return f"❌ SVG export failed: {e}"
    return f"✅ Exported SVG → {path}"


@mcp.tool()
def export_png(drawing: str, file_path: str = "", dpi: int = 150) -> str:
    """Export the drawing to PNG image.

    Args:
        drawing: Drawing label.
        file_path: Output path. Defaults to <output_dir>/<name>.png.
        dpi: Resolution in dots per inch.
    """
    doc = _get_drawing(drawing)
    if not file_path:
        _ensure_output_dir()
        file_path = str(_output_dir / f"{drawing}.png")
    path = Path(file_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from ezdxf.addons.drawing.matplotlib import qsave
        qsave(doc.modelspace(), str(path), dpi=dpi, bg="#FFFFFF")
    except Exception as e:
        return f"❌ PNG export failed: {e}"
    return f"✅ Exported PNG → {path}"


# ── Convenience: template shapes ───────────────────────────────────────────

@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    # Auto-create layer
    if layer not in doc.layers:
        doc.layers.add(layer, color=1)

    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length == 0:
        return "❌ Start and end points are the same."
    # Normal vector
    nx = -dy / length * thickness / 2
    ny = dx / length * thickness / 2
    pts = [
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    return f"✅ Wall ({x1},{y1})→({x2},{y2}) t={thickness} on '{layer}'."


@mcp.tool()
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
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    if layer not in doc.layers:
        doc.layers.add(layer, color=3)

    attribs = {"layer": layer}
    if direction.lower() == "right":
        msp.add_line((x, y), (x + width, y), dxfattribs=attribs)
        msp.add_arc((x, y), width, 0, 90, dxfattribs=attribs)
    else:
        msp.add_line((x, y), (x - width, y), dxfattribs=attribs)
        msp.add_arc((x, y), width, 90, 180, dxfattribs=attribs)
    return f"✅ Door at ({x},{y}) width={width} swing={direction} on '{layer}'."


@mcp.tool()
def draw_window(
    drawing: str,
    x1: float, y1: float,
    x2: float, y2: float,
    layer: str = "Windows",
) -> str:
    """Draw a window symbol (three parallel lines) for floor plans.

    Args:
        drawing: Drawing label.
        x1, y1: Window start point.
        x2, y2: Window end point.
        layer: Target layer.
    """
    doc = _get_drawing(drawing)
    msp = doc.modelspace()
    if layer not in doc.layers:
        doc.layers.add(layer, color=5)

    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length == 0:
        return "❌ Start and end points are the same."
    nx = -dy / length * 0.06
    ny = dx / length * 0.06
    attribs = {"layer": layer}
    # Three parallel lines
    for offset in [-1, 0, 1]:
        ox = nx * offset * 2
        oy = ny * offset * 2
        msp.add_line(
            (x1 + ox, y1 + oy),
            (x2 + ox, y2 + oy),
            dxfattribs=attribs,
        )
    return f"✅ Window ({x1},{y1})→({x2},{y2}) on '{layer}'."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

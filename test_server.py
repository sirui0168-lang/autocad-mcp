#!/usr/bin/env python3
"""Self-test script for AutoCAD MCP Server.

Validates that ezdxf, matplotlib, and server functions operate correctly,
including the regression fixes for v1.1.0 (batch ops, layer handling,
polyline `is_closed` alias, hatch colors, delete-by-handle, …).

Usage:
    python3 test_server.py          # full test suite
"""

import json
import sys
from pathlib import Path

# Ensure plugin dir is on path
plugin_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(plugin_dir))

import server  # noqa: E402

PASSED = 0
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name} {detail}")


def main() -> int:
    print("=" * 68)
    print("AutoCAD MCP Server Self-Test (v1.1.0)")
    print("=" * 68)

    out_dir = Path.home() / "Documents" / "AutoCAD_MCP"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. lifecycle ──────────────────────────────────────────────────────
    print("\n[1] drawing lifecycle")
    check("new_drawing", "Created new drawing" in server.new_drawing("test_run", "R2018"))
    check("set_units", "units set to meters" in server.set_units("test_run", "meters"))
    check("list_drawings", "test_run" in server.list_drawings())

    # ── 2. layers ─────────────────────────────────────────────────────────
    print("\n[2] layers")
    check("create_layer", "created" in server.create_layer("test_run", "Walls", 1))
    check("duplicate layer warns", "already exists" in server.create_layer("test_run", "Walls", 1))
    check("list_layers", "Walls" in server.list_layers("test_run"))

    # ── 3. batch ops (the fast path) ──────────────────────────────────────
    print("\n[3] apply_ops batch")
    res = server.apply_ops(
        "test_run",
        [
            {"op": "layer", "name": "Doors", "color": 3},
            {"op": "layer", "name": "Windows", "color": 5},
            {"op": "layer", "name": "Dims", "color": 2},
            {"op": "layer", "name": "Text", "color": 7},
            {"op": "wall", "x1": 0, "y1": 0, "x2": 8, "y2": 0},
            {"op": "wall", "x1": 8, "y1": 0, "x2": 8, "y2": 6},
            {"op": "wall", "x1": 8, "y1": 6, "x2": 0, "y2": 6},
            {"op": "wall", "x1": 0, "y1": 6, "x2": 0, "y2": 0},
            {"op": "rect", "x": 0, "y": 0, "width": 8, "height": 6, "layer": "0"},
            {"op": "door", "x": 3, "y": 0, "width": 0.9},
            {"op": "window", "x1": 5, "y1": 0, "x2": 6.5, "y2": 0},
            {"op": "text", "text": "Living room", "x": 3, "y": 3, "height": 0.3, "layer": "Text"},
            {"op": "dim", "x1": 0, "y1": 0, "x2": 8, "y2": 0, "distance": 0.5, "layer": "Dims"},
            {"op": "hatch", "points": [[0, 0], [2, 0], [2, 2]], "pattern": "ANSI31"},
            {"op": "circle", "cx": 6, "cy": 4, "radius": 0.4},
        ],
    )
    check("apply_ops all ok", "15/15 ok" in res, res)

    bad = server.apply_ops("test_run", [{"op": "no_such_op"}])
    check("apply_ops reports bad op", "0/1 ok" in bad and "unknown op" in bad, bad)
    check("apply_ops continues after error", "1/2 ok" in server.apply_ops(
        "test_run", [{"op": "nope"}, {"op": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1}]
    ))

    doc = server._get_drawing("test_run")
    msp = doc.modelspace()

    # ── 4. regression: layers honoured ────────────────────────────────────
    print("\n[4] regression — layer routing")
    def layers_of(etype: str) -> set[str]:
        return {e.dxf.layer for e in msp if e.dxftype() == etype}

    check("walls on 'Walls'", layers_of("LWPOLYLINE") >= {"Walls", "0"})
    check("door on 'Doors'", "Doors" in layers_of("ARC"), str(layers_of("ARC")))
    check("dimension on 'Dims'", layers_of("DIMENSION") == {"Dims"}, str(layers_of("DIMENSION")))
    check("text on 'Text'", layers_of("TEXT") == {"Text"}, str(layers_of("TEXT")))

    # ── 5. regression: draw_polyline is_closed alias ─────────────────────
    print("\n[5] regression — is_closed alias")
    server.draw_polyline("test_run", "[[0,0],[1,0],[1,1]]", is_closed=True)
    poly = [e for e in msp if e.dxftype() == "LWPOLYLINE"][-1]
    check("is_closed=True closes the polyline", bool(poly.closed))

    # ── 6. regression: save_drawing accepts drawing= ─────────────────────
    print("\n[6] regression — save_drawing(drawing=...)")
    dxf_file = str(out_dir / "test_run.dxf")
    res = server.save_drawing(drawing="test_run", file_path=dxf_file)
    check("save with drawing kw", "Saved" in res, res)
    check("save with legacy name kw", "Saved" in server.save_drawing(name="test_run", file_path=dxf_file))
    check("dxf exists", Path(dxf_file).exists())

    # ── 7. regression: hatch colors ──────────────────────────────────────
    print("\n[7] regression — hatch color")
    server.add_hatch("test_run", "[[0,0],[1,0],[1,1]]", color=0)
    server.add_hatch("test_run", "[[0,0],[1,0],[1,1]]")
    hats = [e for e in msp if e.dxftype() == "HATCH"]
    check("color=0 preserved", hats[-2].dxf.color == 0, str(hats[-2].dxf.color))
    check("None → BYLAYER (256)", hats[-1].dxf.color == 256, str(hats[-1].dxf.color))

    # ── 8. regression: delete by handle / delete_layer with blocks ───────
    print("\n[8] regression — deletion")
    server.create_block("test_run", "DESK")
    server.add_entity_to_block("test_run", "DESK", "line",
                               json.dumps({"start": [0, 0], "end": [1, 1]}))
    server.create_layer("test_run", "Tmp", 2)
    server.insert_block("test_run", "DESK", 1, 1, layer="Tmp")
    res = server.delete_layer("test_run", "Tmp")
    block_layers = {e.dxf.layer for e in doc.blocks["DESK"]}
    check("delete_layer moves block entities", "Tmp" not in block_layers, str(block_layers))
    check("delete_layer reports moved count", "entities moved" in res, res)

    victim = [e for e in msp if e.dxftype() == "CIRCLE"][-1]
    handle = victim.dxf.handle
    check("delete by handle", "Deleted CIRCLE" in server.delete_entity("test_run", handle=handle))
    check("entity left modelspace", handle not in {e.dxf.handle for e in msp})

    # ── 9. regression: window thickness ──────────────────────────────────
    print("\n[9] regression — window thickness")
    before = len(msp)
    server.draw_window("test_run", 0, 0, 2, 0, thickness=0.5)
    lines = [e for e in list(msp)[before:] if e.dxftype() == "LINE"]
    ys = sorted(round(e.dxf.start.y, 4) for e in lines)
    check("three parallel lines", len(lines) == 3, str(len(lines)))
    check("spacing follows thickness", ys == [-0.25, 0.0, 0.25], str(ys))

    # ── 10. queries ──────────────────────────────────────────────────────
    print("\n[10] queries")
    check("list_entities shows handles", "#" in server.list_entities("test_run"))
    check("inspect_drawing summary", "Entities:" in server.inspect_drawing("test_run"))
    check("extents", "Min:" in server.get_drawing_extents("test_run"))
    check("area", "Area = 3.0000" in server.calculate_area("test_run", "[[0,0],[2,0],[2,3]]"))

    # ── 11. export ────────────────────────────────────────────────────────
    print("\n[11] export")
    check("export_svg", "SVG" in server.export_svg("test_run", str(out_dir / "test_run.svg")))
    check("export_png", "PNG" in server.export_png("test_run", str(out_dir / "test_run.png"), dpi=120))
    img = server.view_drawing("test_run")
    check("view_drawing returns Image", type(img).__name__ == "Image", type(img).__name__)
    res = server.finish("test_run", save=True, svg=True, png=False)
    check("finish writes both files", "DXF →" in res and "SVG →" in res, res)

    # ── 12. output dir expansion ─────────────────────────────────────────
    print("\n[12] output dir")
    check("no literal ~ in output dir", "~" not in str(server._output_dir), str(server._output_dir))

    # ── 13. cleanup ──────────────────────────────────────────────────────
    print("\n[13] cleanup")
    check("close_drawing", "Closed" in server.close_drawing("test_run"))
    check("closed drawing gone", "test_run" not in server._drawings)

    print("\n" + "=" * 68)
    if FAILED:
        print(f"❌ {len(FAILED)} test(s) failed: {', '.join(FAILED)}")
        print("=" * 68)
        return 1
    print(f"✅ All {PASSED} checks passed. Output files: {out_dir}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())

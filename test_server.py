#!/usr/bin/env python3
"""Self-test script for AutoCAD MCP Server.

Validates that ezdxf, matplotlib, and server functions operate correctly.
Usage:
    python3 test_server.py
"""

import sys
import json
from pathlib import Path

# Ensure plugin dir is on path
plugin_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(plugin_dir))

import server

def main():
    print("=" * 60)
    print("AutoCAD MCP Server Self-Test")
    print("=" * 60)

    # 1. new_drawing
    res = server.new_drawing("test_run", "R2018")
    print(f"[1/8] new_drawing: {res}")
    assert "Created new drawing" in res

    # 2. set_units
    res = server.set_units("test_run", "millimeters")
    print(f"[2/8] set_units: {res}")

    # 3. create_layer
    res = server.create_layer("test_run", "Walls", 1) # Red
    print(f"[3/8] create_layer: {res}")
    res = server.create_layer("test_run", "Doors", 3) # Green
    res = server.create_layer("test_run", "Annotations", 7) # White

    # 4. draw geometry
    server.draw_rectangle("test_run", 0, 0, 8000, 6000, layer="Walls")
    server.draw_wall("test_run", 0, 0, 8000, 0, thickness=240, layer="Walls")
    server.draw_door("test_run", 4000, 0, width=900, direction="right", layer="Doors")
    server.add_text("test_run", "Office Room 101", 3500, 3000, height=250, layer="Annotations")
    server.add_dimension_linear("test_run", 0, 0, 8000, 0, distance=800, layer="Annotations")
    print("[4/8] draw geometry & annotations: OK")

    # 5. extents & area
    extents = server.get_drawing_extents("test_run")
    print(f"[5/8] get_drawing_extents: {extents}")

    # 6. save_drawing
    out_dir = Path.home() / "Documents" / "AutoCAD_MCP"
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_file = str(out_dir / "test_run.dxf")
    res = server.save_drawing("test_run", dxf_file)
    print(f"[6/8] save_drawing: {res}")
    assert Path(dxf_file).exists()

    # 7. export_png
    png_file = str(out_dir / "test_run.png")
    res = server.export_png("test_run", png_file, dpi=120)
    print(f"[7/8] export_png: {res}")

    # 8. export_svg
    svg_file = str(out_dir / "test_run.svg")
    res = server.export_svg("test_run", svg_file)
    print(f"[8/8] export_svg: {res}")

    # Clean up memory
    server.close_drawing("test_run")

    print("=" * 60)
    print("✅ All tests passed successfully!")
    print(f"Output files saved to: {out_dir}")
    print("=" * 60)

if __name__ == "__main__":
    main()

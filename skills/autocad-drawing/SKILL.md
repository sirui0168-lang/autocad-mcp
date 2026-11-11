---
name: autocad-drawing
description: >-
  Skill for creating and editing AutoCAD DXF drawings using the autocad-mcp
  MCP server tools. Covers floor plans, mechanical drawings, annotations,
  layers, blocks, and export workflows. Activate when the user wants to
  create or edit CAD drawings.
---

# AutoCAD Drawing Skill

This skill drives the **autocad-mcp** MCP server to create and manipulate
DXF drawings.  The server runs entirely on macOS via `ezdxf` — no AutoCAD
installation is required.

## Available MCP Tools

| Tool                    | Purpose                                          |
|:------------------------|:-------------------------------------------------|
| `new_drawing`           | Create a new empty DXF drawing in memory         |
| `open_drawing`          | Open an existing DXF file                        |
| `save_drawing`          | Save to DXF file                                 |
| `close_drawing`         | Close a drawing from memory                      |
| `list_drawings`         | List all open drawings                           |
| `set_units`             | Set drawing units (mm, m, inches…)               |
| `create_layer`          | Create a layer with color and linetype           |
| `list_layers`           | List all layers                                  |
| `delete_layer`          | Delete a layer                                   |
| `draw_line`             | Draw a line segment                              |
| `draw_circle`           | Draw a circle                                    |
| `draw_arc`              | Draw an arc                                      |
| `draw_rectangle`        | Draw a rectangle                                 |
| `draw_polygon`          | Draw a regular polygon                           |
| `draw_polyline`         | Draw a polyline through points                   |
| `draw_spline`           | Draw a B-spline curve                            |
| `draw_ellipse`          | Draw an ellipse                                  |
| `add_text`              | Add single-line text                             |
| `add_mtext`             | Add multi-line text                              |
| `add_dimension_linear`  | Add linear dimension                             |
| `add_hatch`             | Add hatch pattern fill                           |
| `create_block`          | Create a block definition                        |
| `add_entity_to_block`   | Add entity to block                              |
| `insert_block`          | Insert a block reference                         |
| `list_blocks`           | List block definitions                           |
| `list_entities`         | List entities in modelspace                      |
| `delete_entity`         | Delete an entity by index                        |
| `calculate_area`        | Calculate polygon area                           |
| `get_drawing_extents`   | Get bounding box                                 |
| `export_svg`            | Export to SVG                                     |
| `export_png`            | Export to PNG (requires matplotlib)               |
| `draw_wall`             | Draw a wall (floor plan helper)                  |
| `draw_door`             | Draw a door symbol                               |
| `draw_window`           | Draw a window symbol                             |

## Typical Workflow

1. **Create** a new drawing with `new_drawing` and set units with `set_units`.
2. **Organize** with layers using `create_layer` — e.g., "Walls", "Doors", "Windows", "Dimensions", "Annotations".
3. **Draw** geometry using draw_* tools.
4. **Annotate** with `add_text`, `add_mtext`, `add_dimension_linear`.
5. **Save** with `save_drawing` (DXF) and optionally export to SVG/PNG for preview.
6. The saved DXF file can be opened directly in AutoCAD 2025.

## Floor Plan Conventions

- Use **meters** as units and **millimeters** for detail.
- Wall thickness: typically 0.24 m (exterior) or 0.12 m (interior).
- Door width: standard 0.9 m.
- Window height: typically 1.5 m, sill at 0.9 m.
- Create separate layers for Walls, Doors, Windows, Furniture, Dimensions, Text.
- Use ACI colors: 1=red (walls), 3=green (doors), 5=blue (windows), 7=white (text).

## Output Directory

Files are saved to `~/Documents/AutoCAD_MCP/` by default. Override with
the `file_path` parameter on `save_drawing` / `export_svg` / `export_png`.

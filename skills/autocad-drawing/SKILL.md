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
DXF drawings. The server runs entirely on macOS via `ezdxf` — no AutoCAD
installation is required.

## The one rule that matters: draw in batches

Every single-entity tool call is a full round trip (model → server → model),
and that round trip is what makes drawing feel slow. A three-room floor plan
done entity-by-entity costs ~35 tool calls.

**Do this instead — ONE call for a whole drawing:**

```text
apply_ops(drawing="plan", ops=[ ...every wall, door, window, dim, text... ],
          finish=True)                     # creates the drawing, saves DXF + SVG
```

Split it into `apply_ops(...)` + `finish(drawing="plan")` only when you want to
inspect the result before writing files.

- `apply_ops` executes the whole list in one round trip. A bad op is reported
  in the result and the rest still run — you can fix just that op afterwards.
- `finish(save=True, svg=True)` writes the DXF and the SVG preview at once.
- `inspect_drawing` gives units, entity counts, layers, blocks, extents and the
  first entities in one call — use it to check your work instead of
  `list_entities` + `list_layers` + `get_drawing_extents`.
- `view_drawing` returns the rendered image inline so you can actually look at
  the result without opening a file.

Use the single-entity tools only when you need to change one thing in a
drawing you already have.

## Ops for `apply_ops`

Most ops accept an optional `layer` and `color` (ACI 1–255).

| op | Parameters | op | Parameters |
| :--- | :--- | :--- | :--- |
| `layer` | `name, color, linetype` | `mtext` | `text, x, y, width, height` |
| `units` | `units` | `dim` | `x1, y1, x2, y2, distance, angle, text_height` |
| `line` | `x1, y1, x2, y2` | `hatch` | `points, pattern, scale` |
| `circle` | `cx, cy, radius` | `wall` | `x1, y1, x2, y2, thickness` |
| `arc` | `cx, cy, radius, start_angle, end_angle` | `door` | `x, y, width, direction` |
| `rect` | `x, y, width, height` | `window` | `x1, y1, x2, y2, thickness` |
| `polygon` | `cx, cy, radius, sides, rotation` | `block` | `name` |
| `polyline` | `points, closed` | `block_entity` | `block_name, entity_type, params` |
| `spline` | `points, degree` | `insert` | `block_name, x, y, scale, rotation` |
| `ellipse` | `cx, cy, major_axis_x, major_axis_y, ratio` | `delete` | `handle` (or `index`) |
| `text` | `text, x, y, height, rotation` | `save` | — |

`points` is a list of `[x, y]` pairs. `hatch.points` must describe a closed
boundary. `delete` takes the `#handle` shown by `list_entities` — indices shift
after every deletion, handles do not.

## Typical workflow

1. `new_drawing(name="plan")` — and put `{"op": "units", "units": "meters"}`
   as the first op (units of a fresh drawing default to millimetres).
2. Put every layer you need in the same batch:
   `{"op": "layer", "name": "Walls", "color": 1}` (walls), `Doors` (3),
   `Windows` (5), `Dims` (2), `Text` (7).
3. Put all geometry in one `apply_ops` batch, in construction order:
   outer walls → interior walls → doors → windows → furniture → dimensions →
   room labels.
4. `inspect_drawing(drawing="plan")` to verify counts/extents.
5. `finish(drawing="plan")` — DXF + SVG, then report the paths.
6. Only if the user asks for a raster image: `export_png(drawing="plan")`
   (matplotlib; fine for small drawings, slow on tens of thousands of entities).

## Tool reference

| Tool | Purpose |
|:---|:---|
| `apply_ops` | **Batch: run many drawing ops in one call** |
| `finish` | **Save DXF + export SVG/PNG in one call** |
| `inspect_drawing` | **Summarise drawing: units, counts, layers, blocks, extents** |
| `view_drawing` | **Render drawing and return it as an inline image** |
| `new_drawing` / `open_drawing` / `close_drawing` | Drawing session lifecycle |
| `save_drawing` | Save to DXF (`drawing=` or legacy `name=`) |
| `list_drawings` | List open drawings |
| `set_units` | Drawing units (`mm`, `m`, `inches`…) |
| `create_layer` / `list_layers` / `delete_layer` | Layer management (`delete_layer` migrates entities to layer `0`) |
| `draw_line` / `draw_circle` / `draw_arc` / `draw_rectangle` | Single primitives |
| `draw_polygon` / `draw_polyline` / `draw_spline` / `draw_ellipse` | Curves and polylines (`draw_polyline` accepts `is_closed` as an alias of `closed`) |
| `draw_wall` / `draw_door` / `draw_window` | Architectural symbols |
| `add_text` / `add_mtext` | Text |
| `add_dimension_linear` | Linear dimension (honours `layer`) |
| `add_hatch` | Hatch (`color=0` means black; omit `color` for BYLAYER) |
| `create_block` / `add_entity_to_block` / `insert_block` / `list_blocks` | Block definitions and references |
| `list_entities` / `delete_entity` | Inspect and delete (prefer `handle=`) |
| `calculate_area` / `get_drawing_extents` | Measurements |
| `export_svg` | SVG (native renderer, fast, no matplotlib needed) |
| `export_png` | PNG (matplotlib) |

## Floor plan conventions

- Use **meters** as units and **millimeters** for detail.
- Wall thickness: 0.24 m (exterior) or 0.12 m (interior).
- Door width: 0.9 m.
- Window height: 1.5 m, sill at 0.9 m.
- Separate layers for Walls, Doors, Windows, Furniture, Dimensions, Text.
- ACI colors: 1=red (walls), 3=green (doors), 5=blue (windows), 7=text.
- Set text height relative to the drawing scale: 0.28 m reads well on a plan
  drawn in metres.

## Output

Files go to `~/Documents/AutoCAD_MCP/` by default (`AUTOCAD_MCP_OUTPUT`
overrides it). `save_drawing` / `finish` / `export_*` accept `file_path` for an
explicit location. Unsaved changes are also auto-saved on shutdown to
`<output>/.autosave/<name>.dxf`.

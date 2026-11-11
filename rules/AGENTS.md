# AutoCAD MCP Rules

When the user asks to create or edit CAD drawings:

## Efficiency rules (most important)

- **Draw in batches.** Prefer ONE `apply_ops(drawing, ops=[...])` call over many
  single-entity tools. Each separate tool call is a round trip; a floor plan
  done call-by-call costs dozens of them and feels slow.
- **Finish in one call.** Use `finish(drawing)` to save the DXF and export the
  SVG preview together instead of `save_drawing` + `export_svg` + `export_png`.
- **Inspect in one call.** Use `inspect_drawing(drawing)` (units, entity counts,
  layers, blocks, extents) instead of `list_layers` + `list_entities` +
  `get_drawing_extents`.
- **Look without files.** Use `view_drawing(drawing)` to see the result inline.
- Use single-entity tools only for a small edit to an existing drawing.

## Drawing rules

- Always start by calling `new_drawing` or `open_drawing` to establish a drawing session.
- Set appropriate units up front (`{"op": "units", "units": "meters"}`) — default is millimetres.
- Organize entities on separate layers by purpose (Walls, Doors, Windows, Dimensions, Text, Furniture).
- Use standard ACI colors for layers: 1=red, 2=yellow, 3=green, 4=cyan, 5=blue, 6=magenta, 7=white.
- Always `finish` (or `save_drawing`) when the user's request is complete.
- Offer a preview (`finish` exports SVG; `view_drawing` shows it inline).
- Prefer `export_svg` over `export_png`: SVG uses the native renderer and is
  ~10× faster; PNG goes through matplotlib and is slow on large drawings.
- The output directory is `~/Documents/AutoCAD_MCP/` by default (`AUTOCAD_MCP_OUTPUT` overrides).
- Generated DXF files can be opened directly in AutoCAD 2025 or any DXF-compatible software.

## Gotchas

- `delete_entity`: prefer `handle=` from `list_entities`; indices shift after deletion.
- `delete_layer` moves entities (including those inside blocks) to layer `0`.
- `add_hatch`: omit `color` for BYLAYER; `color=0` means black.
- `draw_polyline` accepts `is_closed` as an alias of `closed`.
- Restart-safe: unsaved drawings are auto-saved on shutdown to `<output>/.autosave/<name>.dxf`.

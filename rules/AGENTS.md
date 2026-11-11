# AutoCAD MCP Rules

When the user asks to create or edit CAD drawings:

- Always start by calling `new_drawing` or `open_drawing` to establish a drawing session.
- Set appropriate units with `set_units` (default: millimeters for architectural, meters for site plans).
- Organize entities on separate layers by purpose (Walls, Doors, Windows, Dimensions, Text, Furniture).
- Use standard ACI colors for layers: 1=red, 2=yellow, 3=green, 4=cyan, 5=blue, 6=magenta, 7=white.
- Always `save_drawing` when the user's request is complete.
- Offer to `export_svg` for quick preview when the drawing is done.
- The output directory is `~/Documents/AutoCAD_MCP/` by default.
- Generated DXF files can be opened directly in AutoCAD 2025 or any DXF-compatible software.

#!/usr/bin/env python3
"""Benchmark for the AutoCAD MCP Server.

Run with the plugin's virtualenv python:

    .venv/bin/python3 benchmark.py

Measures the things that actually made the server feel slow:
  * process start-up / MCP handshake cost
  * tools/list size (it is re-sent to the model every turn)
  * end-to-end "draw a floor plan + save + preview" time
  * export time on a 50k-entity drawing
  * whether a long export blocks other tool calls (event-loop blocking)
"""

import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

T0 = time.perf_counter()
import server  # noqa: E402
IMPORT_MS = (time.perf_counter() - T0) * 1000

OUT = Path("/tmp/autocad_mcp_bench")
OUT.mkdir(parents=True, exist_ok=True)


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def hr(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


async def main() -> None:
    print("=" * 68)
    print("AutoCAD MCP Server benchmark (v1.1.0)")
    print("=" * 68)

    hr("1. start-up")
    print(f"import server                     {IMPORT_MS:8.1f} ms")
    tools = await server.mcp.list_tools()
    schema = json.dumps([t.inputSchema for t in tools])
    print(f"registered tools                  {len(tools):8d} tools")
    print(f"tools/list schema size            {len(schema):8d} chars (~{len(schema)//4} tokens)")

    # let the background renderer warm-up finish, as it would before a real call
    if server._warm_done.wait(10):
        print(f"renderer warm-up                  done")

    print("\n" + "=" * 68)
    print("2. floor plan: batch ops + finish (the agent's real workflow)")
    print("=" * 68)
    ops = [{"op": "layer", "name": n, "color": c} for n, c in
           [("Walls", 1), ("Doors", 3), ("Windows", 5), ("Dims", 2), ("Text", 7)]]
    ops += [
        {"op": "wall", "x1": 0, "y1": 0, "x2": 8, "y2": 0},
        {"op": "wall", "x1": 8, "y1": 0, "x2": 8, "y2": 6},
        {"op": "wall", "x1": 8, "y1": 6, "x2": 0, "y2": 6},
        {"op": "wall", "x1": 0, "y1": 6, "x2": 0, "y2": 0},
        {"op": "wall", "x1": 4, "y1": 0, "x2": 4, "y2": 6, "thickness": 0.12},
        {"op": "rect", "x": 0, "y": 0, "width": 8, "height": 6},
        {"op": "door", "x": 3, "y": 0, "width": 0.9},
        {"op": "door", "x": 4, "y": 3, "width": 0.8, "direction": "left"},
        {"op": "window", "x1": 5, "y1": 0, "x2": 6.5, "y2": 0},
        {"op": "window", "x1": 0, "y1": 2, "x2": 0, "y2": 3.5},
        {"op": "dim", "x1": 0, "y1": 0, "x2": 8, "y2": 0, "distance": 0.6, "layer": "Dims"},
        {"op": "dim", "x1": 8, "y1": 0, "x2": 8, "y2": 6, "distance": 0.6,
         "angle": 90, "layer": "Dims"},
        {"op": "hatch", "points": [[4.2, 0.2], [7.8, 0.2], [7.8, 2.8], [4.2, 2.8]],
         "pattern": "ANSI31", "scale": 0.1},
    ]
    for room, (x, y) in {"Living room": (2.0, 4.5), "Bedroom": (6.0, 4.5),
                         "Kitchen": (6.0, 1.5)}.items():
        ops.append({"op": "text", "text": room, "x": x, "y": y,
                    "height": 0.28, "layer": "Text"})
    ops.append({"op": "circle", "cx": 4, "cy": 4.6, "radius": 0.35})

    server.new_drawing("bench", "R2018")
    server.set_units("bench", "meters")
    t = time.perf_counter()
    res = server.apply_ops("bench", ops)
    apply_ms = ms(t)
    print(f"apply_ops  ({len(ops)} ops, 1 round trip)   {apply_ms:8.1f} ms   {res.splitlines()[0]}")

    # v1.2.0: the same drawing in ONE round trip
    server.new_drawing("bench1", "R2018")
    server.set_units("bench1", "meters")
    t = time.perf_counter()
    res1 = server.apply_ops("bench1", ops, finish=True)
    onepass_ms = ms(t)
    print(f"apply_ops(finish=True) ({len(ops)} ops, 1 round trip) {onepass_ms:8.1f} ms   "
          f"{'DXF+SVG ok' if 'SVG →' in res1 else 'FAILED'}")

    t = time.perf_counter()
    res = server.finish("bench", save=True, svg=True)
    finish_ms = ms(t)
    print(f"finish (DXF + SVG, 1 round trip)    {finish_ms:8.1f} ms")
    for line in res.splitlines()[1:]:
        print("   " + line.strip())

    t = time.perf_counter()
    server.export_svg("bench")  # warm run
    print(f"export_svg warm                     {ms(t):8.1f} ms")

    # ── 3. big drawing ────────────────────────────────────────────────────
    hr("3. 50,000-entity drawing (5.8 MB DXF)")
    big = server._drawings["bench"]
    msp = big.modelspace()
    t = time.perf_counter()
    for i in range(50000):
        msp.add_line((i % 500, i // 500), ((i % 500) + 0.5, (i // 500) + 0.5))
    print(f"create 50k lines                    {ms(t):8.1f} ms")

    big_path = OUT / "big.dxf"
    t = time.perf_counter()
    big.saveas(str(big_path))
    print(f"save 50k entities ({big_path.stat().st_size/1e6:.1f} MB)  {ms(t):8.1f} ms")

    t = time.perf_counter()
    server.export_svg("bench", str(OUT / "big.svg"))
    svg_ms = ms(t)
    print(f"export_svg 50k entities (native)    {svg_ms:8.1f} ms")

    server._bbox_caches.pop("bench", None)
    t = time.perf_counter()
    server.get_drawing_extents("bench")
    print(f"extents 50k (cold cache)            {ms(t):8.1f} ms")
    t = time.perf_counter()
    server.get_drawing_extents("bench")
    print(f"extents 50k (cached)                {ms(t):8.1f} ms")

    # ── 4. does a long export block other calls? ─────────────────────────
    hr("4. concurrency — export must not block the server")
    async def timed_call(name: str, args: dict) -> tuple[str, float]:
        t0 = time.perf_counter()
        await server.mcp.call_tool(name, args)
        return name, ms(t0)

    results = await asyncio.gather(
        timed_call("export_svg", {"drawing": "bench"}),
        timed_call("list_drawings", {}),
    )
    for name, elapsed in results:
        print(f"{name:32s} {elapsed:8.1f} ms")
    side = dict(results)["list_drawings"]
    print(f"→ list_drawings during export was {side:8.1f} ms "
          f"({'✅ not blocked' if side < 100 else '❌ BLOCKED'})")

    # ── 5. image export ───────────────────────────────────────────────────
    hr("5. PNG / inline preview")
    await server.mcp.call_tool("view_drawing", {"drawing": "bench", "dpi": 100})
    server.new_drawing("preview", "R2018")
    for i in range(60):
        server.draw_line("preview", i, 0, i, 5)
    t = time.perf_counter()
    server.export_png("preview", str(OUT / "preview.png"), 150)
    print(f"export_png small drawing            {ms(t):8.1f} ms")
    t = time.perf_counter()
    await server.mcp.call_tool("view_drawing", {"drawing": "preview", "dpi": 110})
    print(f"view_drawing small (inline image)   {ms(t):8.1f} ms")
    await server.mcp.call_tool("view_drawing", {"drawing": "bench", "dpi": 110})

    content, structured = await server.mcp.call_tool(
        "apply_ops", {"drawing": "bench", "ops": [{"op": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1}]}
    )
    print(f"apply_ops via MCP                   ok ({type(content[0]).__name__})")

    print("\n" + "=" * 68)
    print("summary")
    print("=" * 68)
    print(f"  start-up                       {IMPORT_MS:8.1f} ms")
    print(f"  floor plan: 1 tool call        {onepass_ms:8.1f} ms "
          f"(was ~35 round trips)")
    print(f"  50k-entity SVG export          {svg_ms:8.1f} ms (matplotlib was ~7000 ms)")
    print(f"  server blocked during export   {'no' if side < 100 else 'yes'}")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())

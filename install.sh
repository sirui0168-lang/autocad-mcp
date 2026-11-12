#!/usr/bin/env bash
# Install autocad-mcp into this directory and wire up the MCP config.
#
#   ./install.sh                # venv + deps + mcp_config.json
#   AUTOCAD_MCP_OUTPUT=~/Cad ./install.sh
#
# Any MCP client (Claude Desktop, Claude Code, Codex, DSH, Antigravity/Gemini
# plugins, Cursor, ...) runs the same stdio server — only the config location
# and syntax differ, and the snippets are printed at the end.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python3"
OUT_DIR="${AUTOCAD_MCP_OUTPUT:-$HOME/Documents/AutoCAD_MCP}"

if [ ! -x "$PY" ]; then
  echo "→ creating venv in $DIR/.venv"
  python3 -m venv "$DIR/.venv"
fi

echo "→ installing dependencies"
"$PY" -m pip install --quiet --upgrade pip || true
"$PY" -m pip install --quiet -r "$DIR/requirements.txt"

mkdir -p "$OUT_DIR"

echo "→ writing mcp_config.json"
cat > "$DIR/mcp_config.json" <<JSON
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "$PY",
      "args": ["$DIR/server.py"],
      "env": {
        "AUTOCAD_MCP_OUTPUT": "$OUT_DIR"
      }
    }
  }
}
JSON

echo "→ syntax check"
"$PY" -c "import ast, sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$DIR/server.py"

cat <<EOF

✅ Done. Server: $PY $DIR/server.py
   Output dir: $OUT_DIR
   PyMuPDF (fast PNG): $("$PY" -c "import pymupdf; print('yes', pymupdf.__doc__ or '')" 2>/dev/null || echo "no (PNG falls back to matplotlib)")

Wire it into a client — same command and args everywhere:

  # Claude Code
  claude mcp add autocad-mcp --env AUTOCAD_MCP_OUTPUT="$OUT_DIR" -- "$PY" "$DIR/server.py"

  # Codex
  codex mcp add autocad-mcp --env AUTOCAD_MCP_OUTPUT="$OUT_DIR" -- "$PY" "$DIR/server.py"

  # DSH: add to <profile>/cordis.patch.yml (or use the MCP settings page)
  #   - insert:
  #       - id: autocad-mcp
  #         name: "@deepseek-ai/dsh-mcp-client"
  #         config:
  #           serverName: autocad-mcp
  #           transport: stdio
  #           command: "$PY"
  #           args: ["$DIR/server.py"]

  # Claude Desktop / Cursor / Antigravity: merge mcp_config.json into their
  # mcpServers block (Claude Desktop: ~/Library/Application Support/Claude/
  # claude_desktop_config.json).

Optional knobs (env): AUTOCAD_MCP_TOOLS=core (9 tools), AUTOCAD_MCP_PNG=matplotlib,
AUTOCAD_MCP_LOG=DEBUG, AUTOCAD_MCP_WARM=0.
EOF

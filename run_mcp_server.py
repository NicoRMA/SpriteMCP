"""Bulletproof Cursor MCP entrypoint for SpriteMCP.

Forces ``src/`` onto ``sys.path`` so the editable package cannot be shadowed,
then runs ``spritemcp.mcp_server:main``. Prefer this script from
``.cursor/mcp.json`` over bare ``-m spritemcp.mcp_server``.

On stderr: prints the loaded module path and registered tool count before
serving. If Cursor Settings still show 28 tools after a restart, the client
cached an old tools/list — bump ``spritemcp_CATALOG_EPOCH`` in mcp.json and
clear the project MCP cache under ``%USERPROFILE%\\.cursor\\projects\\``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

print(
    f"SpriteMCP run_mcp_server: src={_SRC} exe={Path(__file__).resolve()}",
    file=sys.stderr,
    flush=True,
)

from spritemcp.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    main()

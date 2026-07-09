"""Directly unit-tests the shipped src/diary/static/app.js SSE frame parser (parseSseFrame) via
Node, closing the multi-line streamed-text corruption bug at the exact layer it lives.

The Python-side persistence test in test_routes_entry.py proves the DB stays clean, but the bug
was purely client-side rendering; only running the real JS guards the browser parser itself
against a regression back to the single greedy `data:` capture. Skipped when node is absent."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sse_starlette.sse import ServerSentEvent

APP_JS = Path(__file__).resolve().parents[1] / "src" / "diary" / "static" / "app.js"

# Stub the browser globals app.js touches at load time, then hand a real server frame to the
# actual parseSseFrame and print its result as JSON for the Python side to assert on.
_NODE_HARNESS = (
    "globalThis.document = { body: { addEventListener() {} }, cookie: '' };"
    "const {parseSseFrame} = require(process.argv[1]);"
    "const {ev, data} = parseSseFrame(process.argv[2]);"
    "process.stdout.write(JSON.stringify({ev, data}));"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_app_js_parseSseFrame_rejoins_multiline_data_from_real_server_frame():
    token = "第一行\n第二行\n\n列表：\n- 项目一"
    # The exact wire frame sse-starlette emits: embedded newlines become multiple `data: ` lines.
    frame = ServerSentEvent(data=token, event="token", sep="\n").encode().decode().split("\n\n")[0]
    assert frame.count("data: ") > 1  # sanity: the frame really is multi-`data:`-line

    out = subprocess.run(
        ["node", "-e", _NODE_HARNESS, str(APP_JS), frame],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = json.loads(out)

    assert result["ev"] == "token"
    assert result["data"] == token  # multi-line token reconstructed exactly
    assert "data: " not in result["data"]  # no stray SSE field prefix leaked into rendered text

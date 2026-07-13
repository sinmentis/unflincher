import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parents[1] / "src" / "unflincher" / "static" / "js"


def _run_node(module_name: str, source: str) -> str:
    return subprocess.run(
        ["node", "-e", source, str(STATIC_JS / module_name)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_entry_module_loads_without_browser_globals():
    output = _run_node(
        "entry.js",
        "const {initEntryPage} = require(process.argv[1]); process.stdout.write(typeof initEntryPage);",
    )
    assert output == "function"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_chat_session_title_validation_rejects_blank_values():
    output = _run_node(
        "chat.js",
        """
        const {isValidSessionTitle} = require(process.argv[1]);
        process.stdout.write(JSON.stringify({
          blank: isValidSessionTitle('   '),
          title: isValidSessionTitle('Choosing without certainty'),
        }));
        """,
    )
    assert json.loads(output) == {"blank": False, "title": True}

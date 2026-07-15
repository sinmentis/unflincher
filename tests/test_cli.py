import subprocess
import sys

import openpyxl


def _make_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(["标题", "链接", "创建时间", "修改时间", "内容"])
    ws.append(["t", "https://x", "2026-01-01 00:00:00", "2026-01-01 00:00:00", "<p>hi</p>"])
    wb.save(path)


def test_cli_import_reports_count(tmp_path):
    xlsx_path = tmp_path / "export.xlsx"
    db_path = tmp_path / "unflincher.db"
    _make_workbook(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "Imported 1 " in result.stdout or "imported 1 " in result.stdout.lower()


def test_cli_import_missing_columns_exits_nonzero(tmp_path):
    xlsx_path = tmp_path / "bad.xlsx"
    db_path = tmp_path / "unflincher.db"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(["标题", "内容"])
    ws.append(["t", "<p>hi</p>"])
    wb.save(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "链接" in result.stderr
    assert "创建时间" in result.stderr


# ---------------------------------------------------------------------------
# Deployment probe subcommand (in-process: a real subprocess would need a real Copilot CLI/token)
# ---------------------------------------------------------------------------

def test_cli_probe_prints_reply_and_exits_zero(monkeypatch, capsys):
    import unflincher.cli as cli_module

    async def _fake_run_probe(model):
        assert model == "claude-sonnet-4.6"
        return "ok"

    shutdown_calls = []

    async def _fake_shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe", "--model", "claude-sonnet-4.6"])

    assert exit_code == 0
    assert shutdown_calls == [True]  # client always torn down after a one-shot CLI invocation
    captured = capsys.readouterr()
    assert "ok" in captured.out
    assert "claude-sonnet-4.6" in captured.out


def test_cli_probe_defaults_to_configured_model(monkeypatch):
    import unflincher.cli as cli_module

    seen = {}

    async def _fake_run_probe(model):
        seen["model"] = model
        return "ok"

    async def _fake_shutdown():
        pass

    monkeypatch.setenv("UNFLINCHER_LLM_MODEL", "gpt-5.5")
    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe"])

    assert exit_code == 0
    assert seen["model"] == "gpt-5.5"


def test_cli_probe_exits_nonzero_and_still_shuts_down_client_on_failure(monkeypatch, capsys):
    import unflincher.cli as cli_module

    async def _fake_run_probe(model):
        raise RuntimeError("model unavailable")

    shutdown_calls = []

    async def _fake_shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe", "--model", "bad-model"])

    assert exit_code == 1
    assert shutdown_calls == [True]  # torn down even on failure
    captured = capsys.readouterr()
    assert "model unavailable" in captured.err


def test_cli_probe_never_touches_a_database_connection(monkeypatch):
    # The probe subcommand itself must never call get_connection()/init_schema() -- unlike the
    # "import" subcommand, no --db argument even exists for "probe".
    import unflincher.cli as cli_module

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("cli probe subcommand must never open a database connection")

    async def _fake_run_probe(model):
        return "ok"

    async def _fake_shutdown():
        pass

    monkeypatch.setattr(cli_module, "get_connection", _fail_if_called)
    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe"])

    assert exit_code == 0

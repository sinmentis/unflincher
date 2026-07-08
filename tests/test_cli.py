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
    db_path = tmp_path / "diary.db"
    _make_workbook(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "diary.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "Imported 1 " in result.stdout or "imported 1 " in result.stdout.lower()


def test_cli_import_missing_columns_exits_nonzero(tmp_path):
    xlsx_path = tmp_path / "bad.xlsx"
    db_path = tmp_path / "diary.db"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(["标题", "内容"])
    ws.append(["t", "<p>hi</p>"])
    wb.save(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "diary.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "链接" in result.stderr
    assert "创建时间" in result.stderr

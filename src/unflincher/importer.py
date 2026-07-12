"""One-time historical import: 豆伴 (Tofu) Chrome-extension Excel export -> diary_entry.

Required columns (verified against the real export): 标题, 链接, 创建时间, 修改时间, 内容.
Per technical design §4 边界情况: fails loudly listing missing columns rather than silently
partial-importing.
"""
import re

import openpyxl
from bs4 import BeautifulSoup

from unflincher.sanitize import sanitize_diary_html

REQUIRED_COLUMNS = ["标题", "链接", "创建时间", "修改时间", "内容"]
SHEET_NAME = "日记"

_EMPTY_P_RE = re.compile(r"<p>\s*</p>")
_REPEATED_HR_RE = re.compile(r"(<hr\s*/?>\s*){2,}")


class MissingColumnsError(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"Excel import missing required columns: {', '.join(missing)}")


def derive_plain_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(separator="\n").strip()


def _clean_import_artifacts(html: str) -> str:
    html = _EMPTY_P_RE.sub("", html)
    html = _REPEATED_HR_RE.sub("<hr>", html)
    return html


def import_excel(path: str, conn) -> int:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]

    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        raise MissingColumnsError(missing)

    col_index = {name: header.index(name) for name in REQUIRED_COLUMNS}
    imported = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows[1:]:
            title = row[col_index["标题"]] or ""
            url = row[col_index["链接"]] or None
            created = row[col_index["创建时间"]]
            modified = row[col_index["修改时间"]]
            raw_html = row[col_index["内容"]] or ""

            clean_html = _clean_import_artifacts(sanitize_diary_html(raw_html))
            content_text = derive_plain_text(clean_html)
            entry_date = str(created)

            conn.execute(
                "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
                "entry_date, source, douban_url, source_modified_at) "
                "VALUES (?, ?, ?, ?, ?, 'import', ?, ?)",
                (title, raw_html, clean_html, content_text, entry_date, url, str(modified) if modified else None),
            )
            imported += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return imported

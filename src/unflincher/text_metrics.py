import re


_CJK_CHARACTER_RE = re.compile(
    "["
    "\u1100-\u11ff"
    "\u3040-\u30ff"
    "\u3130-\u318f"
    "\u31f0-\u31ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uac00-\ud7af"
    "\uf900-\ufaff"
    "\uff66-\uff9d"
    "\U00020000-\U0002fa1f"
    "]"
)
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def count_writing_units(text: str) -> int:
    """Count CJK characters individually and whitespace-delimited language as words."""
    cjk_count = len(_CJK_CHARACTER_RE.findall(text))
    non_cjk_text = _CJK_CHARACTER_RE.sub(" ", text)
    return cjk_count + len(_WORD_RE.findall(non_cjk_text))

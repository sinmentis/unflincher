"""One shared Jinja2Templates instance for the whole app. Every route module imports
`templates` from here instead of constructing its own -- a per-module instance would each
need `t`/`lang` registered separately and could drift. The `unflincher_lang` cookie (not a
server-side session -- this app has no accounts) picks the language; context_processors
binds `t`/`lang` once per request so templates just call {{ t("some.key") }}."""
import functools

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from unflincher.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGE_CODES
from unflincher.i18n import t as _t

LANG_COOKIE_NAME = "unflincher_lang"


def get_current_language(request: Request) -> str:
    lang = request.cookies.get(LANG_COOKIE_NAME)
    if lang in SUPPORTED_LANGUAGE_CODES:
        return lang
    return DEFAULT_LANGUAGE


NAV_LABEL_KEYS = {
    "timeline": "nav.timeline",
    "report": "nav.report",
    "chat": "nav.chat",
    "new_entry": "nav.new_entry",
    "workshop": "nav.workshop",
}


def get_ui_state(path: str) -> tuple[str | None, str]:
    if path == "/":
        return "timeline", "timeline"
    if path.startswith("/entry/"):
        return "timeline", "entry"
    if path.startswith("/report"):
        return "report", "report"
    if path == "/chat":
        return "chat", "chat-list"
    if path.startswith("/chat/"):
        return "chat", "chat-session"
    if path.startswith("/new"):
        return "new_entry", "new-entry"
    if path.startswith("/workshop"):
        return "workshop", "workshop"
    return None, "error"


def _i18n_context(request: Request) -> dict:
    lang = get_current_language(request)
    if getattr(request.state, "ui_error_page", False):
        active_nav, page_id = None, "error"
    else:
        active_nav, page_id = get_ui_state(request.url.path)
    translate = functools.partial(_t, lang)
    current_nav_label = translate(NAV_LABEL_KEYS[active_nav]) if active_nav else translate("nav.title")
    return {
        "lang": lang,
        "t": translate,
        "active_nav": active_nav,
        "page_id": page_id,
        "current_nav_label": current_nav_label,
    }


templates = Jinja2Templates(
    directory="src/unflincher/templates",
    context_processors=[_i18n_context],
)

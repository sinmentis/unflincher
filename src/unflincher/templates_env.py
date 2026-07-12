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


def _i18n_context(request: Request) -> dict:
    lang = get_current_language(request)
    return {"lang": lang, "t": functools.partial(_t, lang)}


templates = Jinja2Templates(
    directory="src/unflincher/templates",
    context_processors=[_i18n_context],
)

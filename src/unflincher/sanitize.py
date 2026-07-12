"""Two sanitization paths, per technical design §5.2/§6.3:
- sanitize_diary_html(): run ONCE at import time on the raw Douban export HTML. The result
  is stored as diary_entry.content_html and is the ONLY HTML field ever rendered with `| safe`.
- render_ai_markdown(): run every time AI-generated text (commentary/report/chat) is displayed.
  AI output is untrusted input regardless of who "asked" for it — never trust it just because
  a model produced it.
"""
from html import escape

import nh3
from markdown_it import MarkdownIt

_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "b", "i", "blockquote", "hr", "ul", "ol", "li",
    "a", "h1", "h2", "h3", "h4", "img", "span", "div", "figure", "figcaption",
    "pre", "code",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href"},
    "img": {"src", "alt"},
}
_URL_SCHEMES = {"http", "https", "mailto"}

_md = MarkdownIt("commonmark", {"html": False})  # raw HTML in model output is escaped, not passed through


def sanitize_diary_html(raw_html: str) -> str:
    cleaned = nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    # nh3 doesn't have a first-class "force these extra attrs on img" option, so add the
    # privacy/perf attributes as a light post-process pass.
    cleaned = cleaned.replace("<img ", '<img loading="lazy" referrerpolicy="no-referrer" ')
    return cleaned


def render_ai_markdown(text: str) -> str:
    html = _md.render(text)
    # Backstop: even with html=False, run the same allow-list sanitizer over the output.
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


def plain_text_to_safe_html(text: str) -> str:
    """User-typed new entries are plain text, but content_html is rendered with `| safe`
    everywhere — so it must be escaped and wrapped, never stored verbatim."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)

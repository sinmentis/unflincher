from unflincher.sanitize import plain_text_to_safe_html, render_ai_markdown, sanitize_diary_html


def test_sanitize_strips_script_tags():
    out = sanitize_diary_html("<p>hi</p><script>alert(1)</script>")
    assert "<script>" not in out
    assert "alert" not in out
    assert "<p>hi</p>" in out


def test_sanitize_strips_data_page_and_id_and_class_and_style():
    out = sanitize_diary_html(
        '<p data-page="0" id="x" class="y" style="color:red">text</p>'
    )
    assert "data-page" not in out
    assert 'id="x"' not in out
    assert 'class="y"' not in out
    assert "style=" not in out
    assert "text" in out


def test_sanitize_allows_real_content_tags():
    out = sanitize_diary_html(
        "<p>a</p><strong>b</strong><blockquote>c</blockquote><hr><h2>d</h2>"
        "<ol><li>e</li></ol>"
    )
    for tag in ("<p>", "<strong>", "<blockquote>", "<hr", "<h2>", "<ol>", "<li>"):
        assert tag in out


def test_sanitize_forces_safe_link_attributes():
    out = sanitize_diary_html('<a href="https://example.com">link</a>')
    assert 'rel="noopener noreferrer nofollow"' in out
    assert "https://example.com" in out


def test_sanitize_strips_javascript_scheme_links():
    out = sanitize_diary_html('<a href="javascript:alert(1)">bad</a>')
    assert "javascript:" not in out


def test_sanitize_forces_safe_image_attributes():
    out = sanitize_diary_html('<img src="https://img9.doubanio.com/x.jpg">')
    assert 'loading="lazy"' in out
    assert 'referrerpolicy="no-referrer"' in out


def test_render_ai_markdown_disables_raw_html():
    out = render_ai_markdown("hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "alert(1)" not in out or "&lt;script&gt;" in out or "alert(1)" not in out


def test_render_ai_markdown_renders_basic_formatting():
    out = render_ai_markdown("**bold** and *italic*")
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out


def test_plain_text_to_safe_html_escapes_and_wraps_paragraphs():
    out = plain_text_to_safe_html("第一段\n\n第二段 <script>alert(1)</script>")
    assert out.count("<p>") == 2
    assert "<script>" not in out
    assert "&lt;script&gt;" in out

from leech.core.helpers.text.paginator import strip_footer, FOOTER_RE


def test_footer_stripped():
    text = "list 'x' (3 tasks):\n0: a\n1: b\n\n`page 1/2 · reply: next / prev / <number> / stop`"
    out = strip_footer(text)
    assert "page 1/2" not in out
    assert "0: a" in out
    assert "1: b" in out


def test_footer_with_closing_note_stripped():
    text = "body\n\n`page 3/7 · reply: next / prev / <number> / stop · closing words`"
    out = strip_footer(text)
    assert out == "body"


def test_clean_text_untouched():
    assert strip_footer("just output") == "just output"
    assert strip_footer("") == ""


def test_footer_regex_matches_real_footer_shape():
    # the exact shape paginator._footer() emits
    from leech.core.helpers.text.paginator import _footer

    rendered = "body" + _footer(2, 5)
    assert FOOTER_RE.search(rendered)
    assert strip_footer(rendered) == "body"

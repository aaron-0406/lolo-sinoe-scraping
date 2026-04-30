"""Verify URL slug generation for filesystem-safe filenames."""

from lolo_sinoe.exploration.recorder import url_to_slug


def test_basic_url() -> None:
    assert url_to_slug("https://casillas.pj.gob.pe/sinoe/bandeja.xhtml")


def test_special_chars_replaced() -> None:
    out = url_to_slug("https://x.com/path?id=42&q=foo bar")
    assert " " not in out
    assert "?" not in out
    assert "&" not in out
    assert "=" not in out


def test_max_length() -> None:
    long_url = "https://x.com/" + "a" * 500
    out = url_to_slug(long_url, max_len=100)
    assert len(out) <= 100


def test_empty_safe_returns_default() -> None:
    assert url_to_slug("?+!@#$") == "page"

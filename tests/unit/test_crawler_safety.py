"""Verify the crawler's safety guards (destructive text patterns, host whitelist)."""

from lolo_sinoe.exploration.crawler import (
    _canonical,
    _is_allowed_host,
    _looks_destructive,
)


def test_destructive_text_caught() -> None:
    assert _looks_destructive("Marcar como leído")
    assert _looks_destructive("Enviar")
    assert _looks_destructive("PRESENTAR escrito")
    assert _looks_destructive("eliminar")
    assert _looks_destructive("Cerrar sesión")


def test_safe_text_not_caught() -> None:
    assert not _looks_destructive("Ver detalle")
    assert not _looks_destructive("Bandeja")
    assert not _looks_destructive("Notificaciones")
    assert not _looks_destructive("Histórico")


def test_host_whitelist() -> None:
    allowed = ("casillas.pj.gob.pe",)
    assert _is_allowed_host("https://casillas.pj.gob.pe/sinoe/bandeja.xhtml", allowed)
    assert _is_allowed_host("https://sub.casillas.pj.gob.pe/x", allowed)
    assert not _is_allowed_host("https://google.com", allowed)
    assert not _is_allowed_host("https://pj.gob.pe", allowed)


def test_canonical_strips_fragment_and_trailing_slash() -> None:
    assert _canonical("https://x.com/a/#frag") == "https://x.com/a"
    assert _canonical("https://x.com/a/") == "https://x.com/a"

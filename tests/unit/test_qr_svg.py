"""Unit tests for _qr_svg helper (Feature 1 — server-side QR rendering)."""
from __future__ import annotations

from zabbix_ai.admin.routes.auth_routes import _qr_svg


def test_qr_svg_returns_xml_declaration():
    svg = _qr_svg("otpauth://totp/test?secret=JBSWY3DPEHPK3PXP")
    assert svg.startswith("<?xml")


def test_qr_svg_contains_svg_element():
    svg = _qr_svg("otpauth://totp/test?secret=JBSWY3DPEHPK3PXP")
    assert "<svg" in svg


def test_qr_svg_is_string():
    result = _qr_svg("hello")
    assert isinstance(result, str)
    assert len(result) > 0

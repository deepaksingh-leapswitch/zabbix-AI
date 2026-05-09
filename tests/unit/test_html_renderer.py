# tests/unit/test_html_renderer.py
from zabbix_ai.renderers.html import render_investigate_page


def test_page_includes_eventid_and_instance():
    page = render_investigate_page(eventid=998877, instance="monitoring",
                                    sse_path="/investigate/stream?token=abc")
    assert "998877" in page
    assert "monitoring" in page
    assert "/investigate/stream?token=abc" in page


def test_page_escapes_dangerous_eventid():
    bad = "</script><script>alert(1)</script>"
    page = render_investigate_page(eventid=bad, instance="monitoring",
                                    sse_path="/x")
    assert "<script>alert(1)</script>" not in page


def test_page_has_sse_consumer_js():
    page = render_investigate_page(eventid=1, instance="monitoring", sse_path="/x")
    assert "EventSource" in page

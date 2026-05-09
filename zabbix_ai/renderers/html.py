from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_investigate_page(*, eventid: int | str, instance: str,
                            sse_path: str) -> str:
    tmpl = _env.get_template("investigate.html")
    return tmpl.render(eventid=eventid, instance=instance, sse_path=sse_path)

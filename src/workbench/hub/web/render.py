"""Server-side HTML without a template engine (SPEC "기술 보완" 1).

Every value goes through :func:`escape` unless it is already :class:`Markup` (built here from
escaped parts). User text is shown only as text nodes or inside ``<pre>``; no inline script or
style exists anywhere (CSP ``script-src 'self'; style-src 'self'``).
"""
from __future__ import annotations

import html

from aiohttp import web

from ..config import INSECURE_WARNING_TEXT

TITLE = "WB Hub"


class Markup(str):
    """HTML that is already safe (escaped or fixed markup)."""


def escape(value: object) -> Markup:
    if isinstance(value, Markup):
        return value
    return Markup(html.escape("" if value is None else str(value), quote=True))


def html_join(parts) -> Markup:
    return Markup("".join(escape(part) for part in parts))


def _attributes(attributes: dict) -> str:
    rendered = []
    for key, value in attributes.items():
        if value is None or value is False:
            continue
        key = key.rstrip("_").replace("_", "-")
        rendered.append(f" {key}" if value is True else f' {key}="{escape(value)}"')
    return "".join(rendered)


def tag(element: str, content: object = "", /, **attributes: object) -> Markup:
    """``<element attr="...">content</element>``; ``class_`` means ``class``; ``True`` is a bare attribute."""
    return Markup(f"<{element}{_attributes(attributes)}>{escape(content)}</{element}>")


def void(element: str, /, **attributes: object) -> Markup:
    return Markup(f"<{element}{_attributes(attributes)}>")


def pre(text: object) -> Markup:
    return tag("pre", text)


def hidden(name: str, value: object) -> Markup:
    return void("input", type="hidden", name=name, value=value)


def csrf_field(csrf: str | None) -> Markup:
    return hidden("csrf", csrf) if csrf else Markup("")


def layout(title: str, body: object, *, insecure_warning: bool = False, csrf: str | None = None,
           stylesheets: tuple = (), scripts: tuple = (), refresh: int | None = None) -> Markup:
    """The page frame: plain-HTTP banner, header with the badge slot and (when logged in) logout.

    ``stylesheets``/``scripts`` are extra ``/static/...`` URLs a page asks for (never inline); ``refresh`` is a
    ``<meta http-equiv="refresh">`` interval in seconds (the status board's 10 s, no JavaScript)."""
    banner = tag("div", INSECURE_WARNING_TEXT, class_="banner insecure", role="alert") if insecure_warning else ""
    logout = (tag("form", html_join([csrf_field(csrf), tag("button", "로그아웃", type="submit")]),
                  method="post", action="/logout", class_="logout") if csrf else "")
    header = tag("header", html_join([tag("a", TITLE, href="/", class_="brand"),
                                      tag("span", "", id="wb-badge", class_="badge"), logout]))
    head = html_join([void("meta", charset="utf-8"),
                      void("meta", name="viewport", content="width=device-width, initial-scale=1"),
                      *([void("meta", http_equiv="refresh", content=str(int(refresh)))] if refresh else []),
                      tag("title", f"{title} - {TITLE}" if title else TITLE),
                      void("link", rel="stylesheet", href="/static/base.css"),
                      *(void("link", rel="stylesheet", href=href) for href in stylesheets),
                      *(tag("script", "", src=src, defer=True) for src in scripts)])
    return Markup("<!doctype html>" + tag("html", html_join([tag("head", head), tag(
        "body", html_join([banner, header, tag("main", body)]))]), lang="ko"))


def page(title: str, body: object, *, status: int = 200, insecure_warning: bool = False,
         csrf: str | None = None, stylesheets: tuple = (), scripts: tuple = (),
         refresh: int | None = None) -> web.Response:
    return web.Response(status=status, text=layout(title, body, insecure_warning=insecure_warning, csrf=csrf,
                                                   stylesheets=stylesheets, scripts=scripts, refresh=refresh),
                        content_type="text/html", charset="utf-8")

"""Server-side HTML without a template engine (SPEC "기술 보완" 1).

Every value goes through :func:`escape` unless it is already :class:`Markup` (built here from
escaped parts). User text is shown only as text nodes or inside ``<pre>``; no inline script or
style exists anywhere (CSP ``script-src 'self'; style-src 'self'``).
"""
from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class NavContext:
    """What the global nav needs from a request: the page ``path`` and the Board agent's ``board_state``."""

    path: str
    board_state: str = "off"


# A logged-in screen restored from the back/forward cache asks ``/api/badge`` whether its session still lives
# (web-board-ux SPEC "Cache 정책과 bfcache"); every page frame with the nav loads it.
SESSION_CHECK_SCRIPT = "/static/session-check.js"

BOARD_DOT_TITLES = {"on": "실행 중", "off": "중지", "error": "오류"}
NAV_LINKS = (("대시보드", "/"), ("요청", "/requests"), ("새 요청", "/requests/new"), ("Board agent", "/board"))
SETTINGS_LINKS = (("비밀번호", "/settings"), ("API key", "/settings/keys"), ("감사 기록", "/settings/audit"))


def nav_for(request: web.Request) -> NavContext:
    """The nav context of a logged-in request; a broken ``board_state`` reads as ``error``."""
    from .middleware import SERVICES
    try:
        state = request.app[SERVICES].board_state()
    except Exception:  # noqa: BLE001 - the nav never fails a page
        state = "error"
    return NavContext(request.path, state if isinstance(state, str) and state in BOARD_DOT_TITLES else "error")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def current_href(path: str) -> str | None:
    """The nav link of the screen at ``path`` (SPEC "전역 nav": by path prefix), None for any other path."""
    if path == "/" or _under(path, "/projects"):
        return "/"
    if path == "/requests/new":
        return "/requests/new"
    if _under(path, "/requests"):
        return "/requests"
    if _under(path, "/board"):
        return "/board"
    if _under(path, "/settings/keys"):
        return "/settings/keys"
    if _under(path, "/settings/audit"):
        return "/settings/audit"
    if _under(path, "/settings"):
        return "/settings"
    return None


def nav_markup(nav: NavContext) -> Markup:
    here = current_href(nav.path)

    def link(label: str, href: str, suffix: object = "") -> Markup:
        return tag("a", html_join([label, suffix]), href=href, aria_current="page" if href == here else None,
                   class_="new-request" if href == "/requests/new" else None)
    title = BOARD_DOT_TITLES.get(nav.board_state, "오류")
    dot = tag("span", "", class_=f"dot dot-{nav.board_state}", title=title)
    # The state is text too (visually hidden, next to the link), so the dot is never colour-only.
    state_text = tag("span", title, class_="visually-hidden")
    links = []
    for label, href in NAV_LINKS:
        links.append(link(label, href, html_join([" ", dot]) if href == "/board" else ""))
        if href == "/board":
            links.append(state_text)
    in_settings = here is not None and here.startswith("/settings")
    summary = tag("summary", "설정", aria_current="page" if in_settings else None)
    links.append(tag("details", html_join([summary, *(link(label, href) for label, href in SETTINGS_LINKS)]),
                     class_="nav-settings"))
    # Narrow screens collapse the nav with no JavaScript: a checkbox before the nav, ``:checked ~ nav`` in base.css.
    toggle = void("input", type="checkbox", id="wb-nav-toggle", class_="nav-toggle")
    toggle_label = tag("label", "메뉴", for_="wb-nav-toggle", class_="nav-toggle-label")
    return html_join([toggle, toggle_label, tag("nav", html_join(links), aria_label="주요")])


def layout(title: str, body: object, *, insecure_warning: bool = False, csrf: str | None = None,
           stylesheets: tuple = (), scripts: tuple = (), refresh: int | None = None,
           module_scripts: tuple = (), metas: dict | None = None, nav: NavContext | None = None) -> Markup:
    """The page frame: plain-HTTP banner, header with the badge slot and (when logged in) logout.

    ``stylesheets``/``scripts`` are extra ``/static/...`` URLs a page asks for (never inline); ``refresh`` is a
    ``<meta http-equiv="refresh">`` interval in seconds (the status board's 10 s, no JavaScript). ``module_scripts``
    are ``<script type="module" src>`` (run after the plain deferred ones, in order); ``metas`` is ``{name: content}``
    for data a script reads (the Board page's CSRF token). A frame with ``nav`` (a logged-in screen) also loads
    :data:`SESSION_CHECK_SCRIPT`."""
    if nav is not None and SESSION_CHECK_SCRIPT not in scripts:
        scripts = (SESSION_CHECK_SCRIPT, *scripts)
    banner = tag("div", INSECURE_WARNING_TEXT, class_="banner insecure", role="alert") if insecure_warning else ""
    logout = (tag("form", html_join([csrf_field(csrf), tag("button", "로그아웃", type="submit")]),
                  method="post", action="/logout", class_="logout") if csrf else "")
    header = tag("header", html_join([tag("a", TITLE, href="/", class_="brand"),
                                      nav_markup(nav) if nav is not None else "",
                                      tag("span", "", id="wb-badge", class_="badge"), logout]))
    head = html_join([void("meta", charset="utf-8"),
                      void("meta", name="viewport", content="width=device-width, initial-scale=1"),
                      *([void("meta", http_equiv="refresh", content=str(int(refresh)))] if refresh else []),
                      *(void("meta", name=name, content=content) for name, content in (metas or {}).items()),
                      tag("title", f"{title} - {TITLE}" if title else TITLE),
                      void("link", rel="stylesheet", href="/static/base.css"),
                      *(void("link", rel="stylesheet", href=href) for href in stylesheets),
                      *(tag("script", "", src=src, defer=True) for src in scripts),
                      *(tag("script", "", src=src, type="module") for src in module_scripts)])
    return Markup("<!doctype html>" + tag("html", html_join([tag("head", head), tag(
        "body", html_join([banner, header, tag("main", body)]))]), lang="ko"))


def page(title: str, body: object, *, status: int = 200, insecure_warning: bool = False,
         csrf: str | None = None, stylesheets: tuple = (), scripts: tuple = (),
         refresh: int | None = None, module_scripts: tuple = (), metas: dict | None = None,
         nav: NavContext | None = None) -> web.Response:
    return web.Response(status=status, text=layout(title, body, insecure_warning=insecure_warning, csrf=csrf,
                                                   stylesheets=stylesheets, scripts=scripts, refresh=refresh,
                                                   module_scripts=module_scripts, metas=metas, nav=nav),
                        content_type="text/html", charset="utf-8")

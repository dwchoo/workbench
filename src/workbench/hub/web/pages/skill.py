"""``/skill/…``: the Workbench user skill files, served to agent machines (web-board-ux SPEC "`/skill/` 제공").

A fixed list of four GET/HEAD routes, public (no login; the web listener's ``allow_from``, Host and bind rules still
apply because they are middleware). Nothing is resolved from the request path: each route names its file. The
served bytes are the installed file's bytes (:func:`workbench.assets.canonical_path`) with two textual changes: the
documented Hub address placeholder becomes this request's origin, and the README's marker line becomes the
``[web] skill_repo_url`` line (or an empty line). ``.env`` is never served. Any other ``/skill/…`` path is a 404.
"""
from __future__ import annotations

from aiohttp import web

from ....assets import canonical_path
from ..middleware import SERVICES, public

PREFIX = "/skill/"
PLACEHOLDER = b"http://YOUR-HUB-HOST:8780"
REPO_MARKER = b"<!-- wb:skill-repo -->"
# Published path -> installed file (relative to the asset root). The only files this module ever opens.
FILES = {
    "README.md": "skills/README.md",
    "wb-inbox/SKILL.md": "skills/wb-inbox/SKILL.md",
    "wb-inbox/wb_inbox.py": "skills/wb-inbox/wb_inbox.py",
    "wb-inbox/.env.example": "skills/wb-inbox/.env.example",
}


def render_file(published: str, data: bytes, origin: str, repo_url: str) -> bytes:
    """``data`` with the Hub address placeholder replaced by ``origin``; the README also gets its repository line.
    ``wb_inbox.py`` is code and stays byte for byte."""
    if published.endswith(".py"):
        return data
    data = data.replace(PLACEHOLDER, origin.encode("ascii"))
    if published == "README.md":
        line = f"Source repository: {repo_url}".encode("ascii") if repo_url else b""
        data = data.replace(REPO_MARKER, line)
    return data


def _not_found() -> web.Response:
    return web.Response(status=404, text="not_found")


def _handler(published: str, installed: str):
    async def serve(request: web.Request) -> web.Response:
        services = request.app[SERVICES]
        policy = services.policy
        origin = f"{policy.scheme}://{request.headers.get('Host', '')}"
        try:
            data = await services.blocking(canonical_path(installed).read_bytes)
        except OSError:
            return _not_found()
        body = render_file(published, data, origin, policy.skill_repo_url)
        return web.Response(body=body, content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": "inline"})
    return public(serve)


@public
async def not_found(request: web.Request) -> web.Response:
    return _not_found()


def register(app: web.Application) -> None:
    for published, installed in FILES.items():
        app.router.add_get(PREFIX + published, _handler(published, installed))
    app.router.add_get(PREFIX[:-1], not_found)
    app.router.add_get(PREFIX + r"{tail:[\s\S]*}", not_found)  # after the fixed list: those match first

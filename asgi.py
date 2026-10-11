"""Productie-entrypoint: Flask-webapp + MCP-server in één proces.

    uvicorn asgi:app --host 0.0.0.0 --port 8000

- /mcp/<MCP_TOKEN>   → MCP-server (voor de Claude-connector)
- alles anders       → de Flask receptenapp

Zonder (of met een te kort) MCP_TOKEN staat de MCP-server uit.
"""
import logging
import os

from a2wsgi import WSGIMiddleware

from app import app as flask_app
from mcp_server import build_server
from recipe_db import RecipeDB
from sheets import get_spreadsheet

log = logging.getLogger("recepten")

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
if MCP_TOKEN and len(MCP_TOKEN) < 24:
    log.warning("MCP_TOKEN is korter dan 24 tekens – MCP-server uitgeschakeld")
    MCP_TOKEN = ""

MCP_PATH = f"/mcp/{MCP_TOKEN}" if MCP_TOKEN else None



def _mcp_db() -> RecipeDB:
    # Claude werkt altijd met verse gegevens (voorstellen en bevestigingscodes
    # moeten op de actuele sheet gebaseerd zijn); dit leegt ook de webcache.
    ss = get_spreadsheet()
    ss.invalidate()
    return RecipeDB(ss)


mcp = build_server(_mcp_db)
mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH or "/mcp/uitgeschakeld",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",   # achter reverse proxy; beveiliging via het geheime pad
)
flask_asgi = WSGIMiddleware(flask_app)


async def _not_found(send):
    await send({"type": "http.response.start", "status": 404,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"Not found"})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        # De MCP session manager heeft een lifespan nodig
        return await mcp_app(scope, receive, send)
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        if MCP_PATH and (path == MCP_PATH or path == MCP_PATH + "/"):
            return await mcp_app(scope, receive, send)
        return await _not_found(send)
    return await flask_asgi(scope, receive, send)

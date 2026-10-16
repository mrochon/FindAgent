"""MCP server that lets agents discover Copilot agents indexed in Azure AI Search."""

import hmac
import os
from typing import Annotated, Literal

import uvicorn
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "agents")
API_KEY = os.environ.get("MCP_API_KEY")
PORT = int(os.environ.get("PORT", "8000"))

SUMMARY_FIELDS = [
    "packageId",
    "displayName",
    "shortDescription",
    "longDescription",
    "publisher",
    "activeUsers",
    "supportedHosts",
    "categories",
    "lastModifiedDateTime",
]
MAX_DESCRIPTION_CHARS = 1000

mcp = FastMCP(
    "agent-finder",
    instructions=(
        "Search the organization's catalog of Microsoft 365 Copilot agents. "
        "Use search_agents to find agents by what they do, optionally filtered by publisher or active users; "
        "use list_publishers to discover valid publisher values; use get_agent for full details of one agent."
    ),
    # Binding to 0.0.0.0 also disables the localhost-only Host header check, which would reject
    # requests arriving through the Container Apps ingress FQDN.
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)

_search = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, DefaultAzureCredential())


def _odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _trim(doc: dict) -> dict:
    desc = doc.get("longDescription")
    if desc and len(desc) > MAX_DESCRIPTION_CHARS:
        doc["longDescription"] = desc[:MAX_DESCRIPTION_CHARS] + "…"
    return doc


@mcp.tool()
async def search_agents(
    query: Annotated[
        str, Field(description="Free-text query matched against agent names and descriptions. Use '*' to match all agents.")
    ] = "*",
    publisher: Annotated[
        str | None, Field(description="Only return agents from this exact publisher (see list_publishers).")
    ] = None,
    min_active_users: Annotated[
        int | None, Field(ge=0, description="Only return agents with at least this many active users in the last 30 days.")
    ] = None,
    max_active_users: Annotated[
        int | None, Field(ge=0, description="Only return agents with at most this many active users in the last 30 days.")
    ] = None,
    order_by: Annotated[
        Literal["relevance", "activeUsers", "lastModified"],
        Field(description="Sort by search relevance, most active users, or most recently modified."),
    ] = "relevance",
    top: Annotated[int, Field(ge=1, le=50, description="Maximum number of agents to return.")] = 10,
    include_blocked: Annotated[bool, Field(description="Include agents an admin has blocked.")] = False,
) -> dict:
    """Search the Copilot agent catalog for agents that match a task or topic."""
    filters = []
    if publisher:
        filters.append(f"publisher eq {_odata_string(publisher)}")
    if min_active_users is not None:
        filters.append(f"activeUsers ge {min_active_users}")
    if max_active_users is not None:
        filters.append(f"activeUsers le {max_active_users}")
    if not include_blocked:
        filters.append("isBlocked ne true")

    sort = {
        "relevance": None,
        "activeUsers": ["activeUsers desc"],
        "lastModified": ["lastModifiedDateTime desc"],
    }[order_by]

    results = await _search.search(
        search_text=query or "*",
        filter=" and ".join(filters) or None,
        order_by=sort,
        select=SUMMARY_FIELDS,
        top=top,
        include_total_count=True,
    )
    agents = [_trim({**doc, "score": doc["@search.score"]}) async for doc in results]
    for agent in agents:
        for key in [k for k in agent if k.startswith("@search.")]:
            del agent[key]
    return {"totalMatches": await results.get_count(), "agents": agents}


@mcp.tool()
async def get_agent(
    package_id: Annotated[str, Field(description="The packageId returned by search_agents.")],
) -> dict:
    """Get the full indexed details of one agent."""
    results = await _search.search(search_text="*", filter=f"packageId eq {_odata_string(package_id)}", top=1)
    async for doc in results:
        return {k: v for k, v in doc.items() if not k.startswith("@search.") and k != "id"}
    return {"error": f"No agent with packageId {package_id!r}"}


@mcp.tool()
async def list_publishers(
    top: Annotated[int, Field(ge=1, le=1000, description="Maximum number of publishers to return.")] = 100,
) -> list[dict]:
    """List agent publishers and how many agents each has, for use as the search_agents publisher filter."""
    results = await _search.search(search_text="*", facets=[f"publisher,count:{top}"], top=0)
    facets = await results.get_facets() or {}
    return [{"publisher": f["value"], "agentCount": f["count"]} for f in facets.get("publisher", [])]


class ApiKeyMiddleware:
    """Requires `x-api-key: <key>` or `Authorization: Bearer <key>` on every HTTP request."""

    def __init__(self, app: ASGIApp, key: str) -> None:
        self._app = app
        self._key = key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            supplied = headers.get(b"x-api-key", b"")
            auth = headers.get(b"authorization", b"")
            if not supplied and auth[:7].lower() == b"bearer ":
                supplied = auth[7:]
            if not hmac.compare_digest(supplied, self._key):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def main() -> None:
    app: ASGIApp = mcp.streamable_http_app()
    if API_KEY:
        app = ApiKeyMiddleware(app, API_KEY)
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

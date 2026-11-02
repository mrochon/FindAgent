"""MCP server that lets agents discover Copilot agents indexed in Azure AI Search."""

import asyncio
import logging
import os
from contextvars import ContextVar
from typing import Annotated, Literal
from urllib.parse import urlparse

import jwt
import uvicorn
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP

from blueprints import BlueprintPermissions

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "agents")
PORT = int(os.environ.get("PORT", "8000"))

# Microsoft Entra ID app registration that represents this MCP server's API.
AUTH_TENANT_ID = os.environ.get("MCP_AUTH_TENANT_ID")
AUTH_CLIENT_ID = os.environ.get("MCP_AUTH_CLIENT_ID")
AUTH_APP_ID_URI = os.environ.get("MCP_AUTH_APP_ID_URI") or (AUTH_CLIENT_ID and f"api://{AUTH_CLIENT_ID}")
AUTH_REQUIRED_ROLE = os.environ.get("MCP_AUTH_REQUIRED_ROLE", "agent.find.all")
AUTH_DISABLED = os.environ.get("MCP_AUTH_DISABLED", "").lower() == "true"
# Public URL of the MCP endpoint, advertised in the OAuth protected resource metadata.
RESOURCE_URL = os.environ.get("MCP_RESOURCE_URL", f"http://localhost:{PORT}/mcp")
# Microsoft Entra ID rejects the RFC 8707 `resource` parameter that MCP clients send once they discover
# this metadata (AADSTS9010010), so it can be turned off for clients configured with the auth server directly.
PUBLISH_RESOURCE_METADATA = os.environ.get("MCP_PUBLISH_RESOURCE_METADATA", "true").lower() == "true"
# Delegated scope interactive clients request; the role check is what grants access.
USER_SCOPE = "access_as_user"
# Optional delegated scope: when the caller's token carries it, agent results also list the delegated
# permissions required by the agent's Entra agent identity blueprint.
REQUIRED_PERMISSIONS_SCOPE = "mcp.requiredpermissions"

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
    "agentIdentityId",
    "isAgent",
]
# The index holds every catalog package; isAgent separates agents from plain Teams apps and Office
# add-ins. `ne false` rather than `eq true` keeps documents indexed before the field existed (null).
AGENTS_FILTER = "isAgent ne false"
MAX_DESCRIPTION_CHARS = 1000

mcp = FastMCP(
    "agent-finder",
    instructions=(
        "Search the organization's catalog of Microsoft 365 Copilot agents. "
        "Use search_agents to find agents by what they do, optionally filtered by publisher or active users; "
        "use list_publishers to discover valid publisher values; use get_agent for full details of one agent. "
        "If the caller's token has the mcp.requiredpermissions scope, agents with an Entra agent identity also "
        "list the delegated permissions their agent identity blueprint requires (requiredDelegatedPermissions)."
    ),
    # Binding to 0.0.0.0 also disables the localhost-only Host header check, which would reject
    # requests arriving through the Container Apps ingress FQDN.
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)

_credential = DefaultAzureCredential()
_search = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, _credential)
_blueprints = BlueprintPermissions(_credential)
# Delegated scopes (`scp`) of the current request's access token, set by EntraAuthMiddleware.
_token_scopes: ContextVar[frozenset[str]] = ContextVar("token_scopes", default=frozenset())


def _odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _trim(doc: dict) -> dict:
    desc = doc.get("longDescription")
    if desc and len(desc) > MAX_DESCRIPTION_CHARS:
        doc["longDescription"] = desc[:MAX_DESCRIPTION_CHARS] + "…"
    return doc


def _add_entra_flag(doc: dict) -> dict:
    # Agents managed in Entra ID have an Entra agent identity; for others agentIdentityId is null.
    doc["entraManaged"] = bool(doc.get("agentIdentityId"))
    return doc


async def _add_required_permissions(agents: list[dict]) -> None:
    """Adds `blueprint` (appId, name and required delegated permissions) to Entra-managed agents, if the
    caller's token has the mcp.requiredpermissions scope."""
    if REQUIRED_PERMISSIONS_SCOPE not in _token_scopes.get():
        return
    managed = [a for a in agents if a.get("agentIdentityId")]
    results = await asyncio.gather(*(_blueprints.for_agent(a["agentIdentityId"]) for a in managed))
    for agent, blueprint in zip(managed, results):
        agent["blueprint"] = blueprint


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
    entra_managed: Annotated[
        bool | None,
        Field(
            description="Only return agents whose access control is (true) or is not (false) managed by "
            "Microsoft Entra ID, i.e. that have an Entra agent identity."
        ),
    ] = None,
    include_non_agents: Annotated[
        bool, Field(description="Also return catalog packages that aren't agents, such as plain Teams apps and Office add-ins.")
    ] = False,
) -> dict:
    """Search the Copilot agent catalog for agents that match a task or topic."""
    filters = [] if include_non_agents else [AGENTS_FILTER]
    if publisher:
        filters.append(f"publisher eq {_odata_string(publisher)}")
    if min_active_users is not None:
        filters.append(f"activeUsers ge {min_active_users}")
    if max_active_users is not None:
        filters.append(f"activeUsers le {max_active_users}")
    if not include_blocked:
        filters.append("isBlocked ne true")
    if entra_managed is not None:
        filters.append(f"agentIdentityId {'ne' if entra_managed else 'eq'} null")

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
    agents = [_add_entra_flag(_trim({**doc, "score": doc["@search.score"]})) async for doc in results]
    for agent in agents:
        for key in [k for k in agent if k.startswith("@search.")]:
            del agent[key]
    await _add_required_permissions(agents)
    return {"totalMatches": await results.get_count(), "agents": agents}


@mcp.tool()
async def get_agent(
    package_id: Annotated[str, Field(description="The packageId returned by search_agents.")],
) -> dict:
    """Get the full indexed details of one agent."""
    results = await _search.search(search_text="*", filter=f"packageId eq {_odata_string(package_id)}", top=1)
    async for doc in results:
        agent = _add_entra_flag({k: v for k, v in doc.items() if not k.startswith("@search.") and k != "id"})
        await _add_required_permissions([agent])
        return agent
    return {"error": f"No agent with packageId {package_id!r}"}


@mcp.tool()
async def list_publishers(
    top: Annotated[int, Field(ge=1, le=1000, description="Maximum number of publishers to return.")] = 100,
    include_non_agents: Annotated[
        bool, Field(description="Also count catalog packages that aren't agents, such as plain Teams apps and Office add-ins.")
    ] = False,
) -> list[dict]:
    """List agent publishers and how many agents each has, for use as the search_agents publisher filter."""
    results = await _search.search(
        search_text="*",
        filter=None if include_non_agents else AGENTS_FILTER,
        facets=[f"publisher,count:{top}"],
        top=0,
    )
    facets = await results.get_facets() or {}
    return [{"publisher": f["value"], "agentCount": f["count"]} for f in facets.get("publisher", [])]


class EntraAuthMiddleware:
    """Requires a Microsoft Entra ID access token for this API that carries the required app role.

    Works for delegated (user) and application tokens alike: in both, the app roles assigned to the
    caller appear in the `roles` claim. Also serves the OAuth protected resource metadata (RFC 9728)
    so MCP clients can discover where to sign in, unless `resource_url` is None.
    """

    def __init__(
        self,
        app: ASGIApp,
        tenant_id: str,
        audiences: list[str],
        required_role: str,
        resource_url: str | None,
        scopes_supported: list[str],
    ) -> None:
        self._app = app
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._jwks = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", lifespan=3600)
        self._audiences = audiences
        self._required_role = required_role

        self._metadata_paths: set[str] = set()
        self._metadata_url: str | None = None
        if resource_url is None:
            return
        resource = urlparse(resource_url)
        well_known = "/.well-known/oauth-protected-resource"
        self._metadata_paths = {well_known, well_known + resource.path}
        self._metadata_url = f"{resource.scheme}://{resource.netloc}{well_known}{resource.path}"
        self._metadata = {
            "resource": resource_url,
            "authorization_servers": [self._issuer],
            "scopes_supported": scopes_supported,
            "bearer_methods_supported": ["header"],
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope["path"] in self._metadata_paths:
            await JSONResponse(self._metadata)(scope, receive, send)
            return
        if not self._metadata_paths and scope["path"].startswith("/.well-known/oauth-protected-resource"):
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return

        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        if auth[:7].lower() != "bearer ":
            await self._reject(scope, receive, send, 401)
            return
        try:
            # Key lookup may hit the network on a cache miss, so keep it off the event loop.
            claims = await asyncio.to_thread(self._validate, auth[7:].strip())
        except jwt.PyJWTError as e:
            logging.warning("Rejected access token: %s", e)
            await self._reject(scope, receive, send, 401, "invalid_token", "The access token is invalid or expired.")
            return
        if self._required_role not in claims.get("roles", []):
            logging.warning("Token for %s lacks role %s", claims.get("oid") or claims.get("azp"), self._required_role)
            await self._reject(
                scope, receive, send, 403, "insufficient_scope", f"The caller is not assigned the '{self._required_role}' role."
            )
            return
        # Tools read the delegated scopes to decide on optional output. Application tokens have no `scp`.
        _token_scopes.set(frozenset(claims.get("scp", "").split()))
        await self._app(scope, receive, send)

    def _validate(self, token: str) -> dict:
        key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=self._audiences,
            issuer=self._issuer,
            options={"require": ["exp", "iss", "aud"]},
        )

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, status: int, error: str | None = None, description: str | None = None
    ) -> None:
        params = [f'resource_metadata="{self._metadata_url}"'] if self._metadata_url else []
        if error:
            params += [f'error="{error}"', f'error_description="{description}"']
        challenge = "Bearer " + ", ".join(params) if params else "Bearer"
        body = {"error": error or "unauthorized", "error_description": description or "A bearer token is required."}
        await JSONResponse(body, status_code=status, headers={"WWW-Authenticate": challenge})(scope, receive, send)


def main() -> None:
    app: ASGIApp = mcp.streamable_http_app()
    if AUTH_DISABLED:
        logging.warning("MCP_AUTH_DISABLED=true: the server accepts unauthenticated requests.")
    elif not (AUTH_TENANT_ID and AUTH_CLIENT_ID):
        raise SystemExit("Set MCP_AUTH_TENANT_ID and MCP_AUTH_CLIENT_ID, or MCP_AUTH_DISABLED=true for local testing.")
    else:
        app = EntraAuthMiddleware(
            app,
            tenant_id=AUTH_TENANT_ID,
            # v2 tokens carry the client ID as audience; accept the App ID URI too.
            audiences=[AUTH_CLIENT_ID, AUTH_APP_ID_URI],
            required_role=AUTH_REQUIRED_ROLE,
            resource_url=RESOURCE_URL if PUBLISH_RESOURCE_METADATA else None,
            scopes_supported=[f"{AUTH_APP_ID_URI}/{USER_SCOPE}"],
        )
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()

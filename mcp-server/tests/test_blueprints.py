import asyncio
import json
import os

import pytest

os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://example.search.windows.net")
import server  # noqa: E402
from blueprints import BlueprintPermissions  # noqa: E402

GRAPH_APP = "00000003-0000-0000-c000-000000000000"
MAIL_READ = "570282fd-fa5c-430d-a7fd-fc8dc98a9dca"
USER_READ = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
CUSTOM_API = "11111111-1111-1111-1111-111111111111"

GRAPH_DATA = {
    "beta/servicePrincipals/agent-1/microsoft.graph.agentIdentity": {
        "id": "agent-1",
        "displayName": "MR Sales Agent",
        "agentIdentityBlueprintId": "bp-app",
    },
    "beta/applications/microsoft.graph.agentIdentityBlueprint": {
        "value": [
            {
                "id": "bp-obj",
                "appId": "bp-app",
                "displayName": "Sales blueprint",
                "requiredResourceAccess": [
                    {
                        "resourceAppId": GRAPH_APP,
                        "resourceAccess": [
                            {"id": MAIL_READ, "type": "Scope"},
                            {"id": USER_READ, "type": "Scope"},
                            {"id": "app-role-id", "type": "Role"},
                        ],
                    },
                    # Only application permissions: not a delegated requirement.
                    {"resourceAppId": "roles-only", "resourceAccess": [{"id": "r", "type": "Role"}]},
                    {"resourceAppId": CUSTOM_API, "resourceAccess": [{"id": "unknown-scope", "type": "Scope"}]},
                ],
            }
        ]
    },
    f"v1.0/servicePrincipals(appId='{GRAPH_APP}')": {
        "displayName": "Microsoft Graph",
        "oauth2PermissionScopes": [{"id": MAIL_READ, "value": "Mail.Read"}, {"id": USER_READ, "value": "User.Read"}],
    },
}


class FakeGraph(BlueprintPermissions):
    def __init__(self, data: dict) -> None:
        super().__init__(credential=None)
        self.data = data
        self.calls: list[tuple[str, dict]] = []

    async def _get(self, path, params):
        self.calls.append((path, params))
        return self.data.get(path)


def test_resolves_required_delegated_permissions():
    graph = FakeGraph(GRAPH_DATA)
    result = asyncio.run(graph.for_agent("agent-1"))
    assert result == {
        "blueprintAppId": "bp-app",
        "blueprintDisplayName": "Sales blueprint",
        "requiredDelegatedPermissions": [
            {"resourceAppId": GRAPH_APP, "resourceDisplayName": "Microsoft Graph", "scopes": ["Mail.Read", "User.Read"]},
            # The custom API has no service principal here, so its scope stays an ID.
            {"resourceAppId": CUSTOM_API, "resourceDisplayName": None, "scopes": ["unknown-scope"]},
        ],
    }
    blueprint_call = next(params for path, params in graph.calls if path.endswith("agentIdentityBlueprint"))
    assert blueprint_call["$filter"] == "appId eq 'bp-app'"


def test_results_are_cached():
    graph = FakeGraph(GRAPH_DATA)
    asyncio.run(graph.for_agent("agent-1"))
    calls = len(graph.calls)
    asyncio.run(graph.for_agent("agent-1"))
    assert len(graph.calls) == calls


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "No agent identity agent-1"),
        ({"beta/servicePrincipals/agent-1/microsoft.graph.agentIdentity": {"id": "agent-1"}}, "has no blueprint"),
        (
            {"beta/servicePrincipals/agent-1/microsoft.graph.agentIdentity": {"agentIdentityBlueprintId": "bp-app"}},
            "No agent identity blueprint with appId bp-app",
        ),
    ],
)
def test_lookup_failures_are_reported_not_raised(data, message):
    result = asyncio.run(FakeGraph(data).for_agent("agent-1"))
    assert message in result["error"]


class FakeBlueprints:
    def __init__(self) -> None:
        self.looked_up: list[str] = []

    async def for_agent(self, agent_identity_id):
        self.looked_up.append(agent_identity_id)
        return {"blueprintAppId": f"bp-for-{agent_identity_id}", "requiredDelegatedPermissions": []}


def run_with_scopes(scopes: set[str], agents: list[dict]) -> None:
    async def go():
        server._token_scopes.set(frozenset(scopes))
        await server._add_required_permissions(agents)

    asyncio.run(go())


@pytest.fixture
def blueprints(monkeypatch) -> FakeBlueprints:
    fake = FakeBlueprints()
    monkeypatch.setattr(server, "_blueprints", fake)
    return fake


@pytest.mark.parametrize(("scp", "expected"), [("access_as_user", False), ("access_as_user mcp.requiredpermissions", True)])
def test_token_scope_reaches_tools_through_mcp_app(blueprints, monkeypatch, scp, expected):
    """End to end: the middleware's scopes must survive the MCP SDK's request handling."""
    from test_auth import CLIENT_ID, KEY, TENANT, token
    from test_tools import FakeSearch

    from types import SimpleNamespace
    from starlette.testclient import TestClient

    monkeypatch.setattr(server, "_search", FakeSearch([{"id": "a", "packageId": "P_1", "agentIdentityId": "agent-1"}]))
    monkeypatch.setattr(server.mcp, "_session_manager", None, raising=False)
    app = server.EntraAuthMiddleware(
        server.mcp.streamable_http_app(),
        tenant_id=TENANT,
        audiences=[CLIENT_ID],
        required_role="agent.find.all",
        resource_url=None,
        scopes_supported=[],
    )
    app._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=KEY.public_key()))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_agent", "arguments": {"package_id": "P_1"}}}
    headers = {
        "Authorization": f"Bearer {token(scp=scp)}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    with TestClient(app) as client:
        resp = client.post("/mcp", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    agent = json.loads(resp.json()["result"]["content"][0]["text"])
    assert ("blueprint" in agent) is expected


def test_permissions_added_only_with_scope(blueprints):
    agents = [{"packageId": "P_1", "agentIdentityId": "agent-1"}, {"packageId": "P_2", "agentIdentityId": None}]
    run_with_scopes({"access_as_user"}, agents)
    assert "blueprint" not in agents[0] and not blueprints.looked_up

    run_with_scopes({"access_as_user", "mcp.requiredpermissions"}, agents)
    assert agents[0]["blueprint"]["blueprintAppId"] == "bp-for-agent-1"
    # Agents without an Entra agent identity have no blueprint to look up.
    assert "blueprint" not in agents[1]
    assert blueprints.looked_up == ["agent-1"]

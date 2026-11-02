"""Looks up the delegated permissions an agent's Microsoft Entra agent identity blueprint requires.

An agent identity (a kind of service principal) names its blueprint by appId. The blueprint is a kind of
application, and its requiredResourceAccess lists the permissions it needs: entries of type "Scope" are
delegated permissions. They're stored as scope IDs, so each resource's service principal is read to turn
them into names.

Requires the Graph application permission Application.Read.All (it covers agent identities, blueprints
and the resource service principals). The agent identity and blueprint endpoints are beta-only.
"""

import asyncio
import logging
import time
from typing import Any

import aiohttp
from azure.core.credentials_async import AsyncTokenCredential

GRAPH = "https://graph.microsoft.com"
CACHE_SECONDS = 15 * 60


class GraphError(Exception):
    pass


class BlueprintPermissions:
    def __init__(self, credential: AsyncTokenCredential, graph_url: str = GRAPH) -> None:
        self._credential = credential
        self._graph_url = graph_url
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    async def for_agent(self, agent_identity_id: str) -> dict:
        """Blueprint and required delegated permissions for an agent identity, or an `error` entry."""
        try:
            return await self._cached(f"agent:{agent_identity_id}", lambda: self._lookup(agent_identity_id))
        except GraphError as e:
            logging.warning("Blueprint lookup failed for agent identity %s: %s", agent_identity_id, e)
            return {"error": str(e)}

    async def _lookup(self, agent_identity_id: str) -> dict:
        identity = await self._get(
            f"beta/servicePrincipals/{agent_identity_id}/microsoft.graph.agentIdentity",
            {"$select": "id,displayName,agentIdentityBlueprintId"},
        )
        if identity is None:
            raise GraphError(f"No agent identity {agent_identity_id} in Microsoft Entra ID")
        blueprint_app_id = identity.get("agentIdentityBlueprintId")
        if not blueprint_app_id:
            raise GraphError(f"Agent identity {agent_identity_id} has no blueprint")

        found = await self._get(
            "beta/applications/microsoft.graph.agentIdentityBlueprint",
            {"$filter": f"appId eq '{blueprint_app_id}'", "$select": "id,appId,displayName,requiredResourceAccess"},
        )
        blueprints = (found or {}).get("value") or []
        if not blueprints:
            raise GraphError(f"No agent identity blueprint with appId {blueprint_app_id} in Microsoft Entra ID")
        blueprint = blueprints[0]

        resources = [r for r in blueprint.get("requiredResourceAccess") or [] if _scope_ids(r)]
        permissions = await asyncio.gather(*(self._delegated(r) for r in resources))
        return {
            "blueprintAppId": blueprint.get("appId"),
            "blueprintDisplayName": blueprint.get("displayName"),
            "requiredDelegatedPermissions": permissions,
        }

    async def _delegated(self, required: dict) -> dict:
        resource_app_id = required["resourceAppId"]
        resource = await self._cached(f"resource:{resource_app_id}", lambda: self._resource(resource_app_id))
        names = resource["scopes"]
        return {
            "resourceAppId": resource_app_id,
            "resourceDisplayName": resource["displayName"],
            # A scope the resource no longer defines is returned by ID rather than dropped.
            "scopes": [names.get(scope_id, scope_id) for scope_id in _scope_ids(required)],
        }

    async def _resource(self, resource_app_id: str) -> dict:
        sp = await self._get(
            f"v1.0/servicePrincipals(appId='{resource_app_id}')", {"$select": "displayName,oauth2PermissionScopes"}
        )
        if sp is None:
            # The resource API has no service principal in this tenant, so its scope names are unknown.
            return {"displayName": None, "scopes": {}}
        return {
            "displayName": sp.get("displayName"),
            "scopes": {s["id"]: s["value"] for s in sp.get("oauth2PermissionScopes") or []},
        }

    async def _cached(self, key: str, load) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await load()
        self._cache[key] = (time.monotonic() + CACHE_SECONDS, value)
        return value

    async def _get(self, path: str, params: dict[str, str]) -> dict | None:
        """GET a Graph resource; None when it doesn't exist."""
        token = await self._credential.get_token(f"{GRAPH}/.default")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.get(
            f"{self._graph_url}/{path}", params=params, headers={"Authorization": f"Bearer {token.token}"}
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status >= 400:
                raise GraphError(f"Graph returned {resp.status} for {path}: {(await resp.text())[:300]}")
            return await resp.json()


def _scope_ids(required: dict) -> list[str]:
    return [a["id"] for a in required.get("resourceAccess") or [] if a.get("type") == "Scope"]

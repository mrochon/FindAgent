# MCP server: requests and formats

This describes what the `agent-finder` MCP server ([mcp-server/server.py](mcp-server/server.py)) accepts and returns.

The server has one MCP endpoint, `POST /mcp`, plus a public sign-in metadata endpoint. Everything else (tool discovery and calls) is carried inside `POST /mcp` as JSON-RPC 2.0 messages.

## HTTP endpoints

| Method and path | Auth | Request | Response |
|---|---|---|---|
| `POST /mcp` | Bearer token with the `agent.find.all` role | One JSON-RPC 2.0 message, `Content-Type: application/json` | JSON-RPC response as `application/json` |
| `GET /.well-known/oauth-protected-resource` (also with `/mcp` on the end) | None | none | Sign-in metadata (RFC 9728): `resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported`. Returns `404` if `MCP_PUBLISH_RESOURCE_METADATA=false` |

How `POST /mcp` behaves:

- **Transport:** it uses MCP's Streamable HTTP transport, in stateless JSON mode.
- **No sessions:** each POST is handled independently, so a client can call `tools/list` or `tools/call` without calling `initialize` first. The server never pushes messages on its own.
- **No streaming:** responses are plain JSON, never server-sent event streams.
- **Accept header:** clients should send `Accept: application/json, text/event-stream`, which MCP clients do automatically.

Rejected requests get an OAuth-style error before reaching MCP:

| Status | When | Body |
|---|---|---|
| `401` | No bearer token | `{"error":"unauthorized","error_description":"A bearer token is required."}` |
| `401` | Bad signature, wrong audience or issuer, or expired token | `{"error":"invalid_token",...}` |
| `403` | Valid token without the `agent.find.all` role | `{"error":"insufficient_scope",...}` |

All three include a `WWW-Authenticate: Bearer ...` header. It points to the metadata URL when publishing is on.

See "MCP server authentication" in the [README](README.md) for how to get a token and how roles are assigned.

## MCP messages (inside `POST /mcp`)

- **Startup and health:** `initialize`, `notifications/initialized` and `ping`.
- **Tools:** `tools/list` returns the three tools with their JSON Schema inputs. `tools/call` runs one.
- **Resources and prompts:** none are defined, so their list requests return empty lists.

A `tools/call` request looks like this:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"search_agents","arguments":{"query":"HR policy","min_active_users":10,"top":5}}}
```

The result carries the tool's output twice: as JSON in `structuredContent`, and as the same JSON serialized into a text block for clients that only read text:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "content":[{"type":"text","text":"{\"totalMatches\": 3, \"agents\": [...]}"}],
  "structuredContent":{"totalMatches":3,"agents":[...]},
  "isError":false}}
```

An unknown tool, invalid arguments (for example `top: 100`) or an AI Search failure return a result with `isError: true` and a text explanation.

## Tools

### `search_agents`

| Argument | Type | Default | Notes |
|---|---|---|---|
| `query` | string | `"*"` | Full-text search over name, short description and long description; `*` matches all |
| `publisher` | string | none | Exact, case-sensitive match on the publisher name |
| `min_active_users`, `max_active_users` | integer ≥ 0 | none | Inclusive bounds on active users in the last 30 days |
| `order_by` | `"relevance"`, `"activeUsers"` or `"lastModified"` | `"relevance"` | The last two sort in descending order |
| `top` | integer, 1 to 50 | `10` | Number of agents to return |
| `include_blocked` | boolean | `false` | Whether to include agents an admin has blocked |
| `include_non_agents` | boolean | `false` | Whether to also return catalog packages that aren't agents (`isAgent` false), such as plain Teams apps and Office add-ins |
| `entra_managed` | boolean | none | `true`: only agents whose access control is managed by Microsoft Entra ID (they have an Entra agent identity). `false`: only agents that aren't. Omit for both |

Returns:

```json
{"totalMatches": 42,
 "agents": [{"packageId": "...", "displayName": "...", "shortDescription": "...",
             "longDescription": "first 1000 chars…", "publisher": "...", "activeUsers": 57,
             "supportedHosts": ["Copilot"], "categories": ["..."],
             "lastModifiedDateTime": "2026-08-06T00:07:20Z",
             "agentIdentityId": "00000000-0000-0000-0000-000000000000", "entraManaged": true,
             "isAgent": true, "score": 3.21}]}
```

`totalMatches` counts all matching agents, not just those returned. `score` is the relevance score from AI Search.

`agentIdentityId` is the agent's Microsoft Entra agent identity, or `null` if the agent isn't managed in Entra ID. `entraManaged` is `true` exactly when `agentIdentityId` is set.

`isAgent` is `true` for agents and `false` for other catalog packages, which appear only with `include_non_agents`. See "How the indexer works" in the README for what counts as an agent.

#### `blueprint` (with the `mcp.requiredpermissions` scope)

When the caller's access token has the `mcp.requiredpermissions` scope, each agent with an `agentIdentityId` also gets a `blueprint` entry. Agents without an `agentIdentityId` don't get one:

```json
"blueprint": {
  "blueprintAppId": "00001111-aaaa-2222-bbbb-3333cccc4444",
  "blueprintDisplayName": "Sales agent blueprint",
  "requiredDelegatedPermissions": [
    {"resourceAppId": "00000003-0000-0000-c000-000000000000", "resourceDisplayName": "Microsoft Graph",
     "scopes": ["Mail.Read", "User.Read"]}
  ]
}
```

- **Contents:** `requiredDelegatedPermissions` lists the delegated (`Scope`) entries of the blueprint's `requiredResourceAccess`, grouped by resource API. Application permissions aren't included.
- **Unresolved names:** a scope ID that the resource API's service principal doesn't define is returned as the raw ID. If the resource has no service principal in the tenant, `resourceDisplayName` is `null`.
- **Failures:** if the agent identity or blueprint can't be read, the entry is `{"error": "..."}` instead. The rest of the result is unaffected.

### `get_agent`

- **Argument:** `package_id` (string, required), the `packageId` from a search result.
- **Returns:** everything the index holds for that agent: the fields above with the full `longDescription`, plus `elementTypes`, `type`, `platform`, `availableTo`, `isBlocked` and `version`. It includes `agentIdentityId` and `entraManaged` as well.
- **Not found:** it returns `{"error": "No agent with packageId '...'"}` as a normal result, not with `isError` set.
- **Blocked agents and non-agents:** neither is filtered out here, so any catalog package can be looked up. Check `isAgent` to tell them apart.
- **Blueprint:** with the `mcp.requiredpermissions` scope, it includes the `blueprint` entry described above.

### `list_publishers`

- **Arguments:** `top` (integer 1 to 1000, default `100`); `include_non_agents` (boolean, default `false`) to also count packages that aren't agents.
- **Returns:** `[{"publisher": "Contoso", "agentCount": 12}, ...]`, ordered by agent count, highest first.
- **Output shape:** because the result is a list, `structuredContent` wraps it as `{"result": [...]}`.
- **Blocked agents:** the counts include them.

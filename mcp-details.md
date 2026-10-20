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

Returns:

```json
{"totalMatches": 42,
 "agents": [{"packageId": "...", "displayName": "...", "shortDescription": "...",
             "longDescription": "first 1000 chars…", "publisher": "...", "activeUsers": 57,
             "supportedHosts": ["Copilot"], "categories": ["..."],
             "lastModifiedDateTime": "2026-08-06T00:07:20Z", "score": 3.21}]}
```

`totalMatches` counts all matching agents, not just those returned. `score` is the relevance score from AI Search.

### `get_agent`

- **Argument:** `package_id` (string, required), the `packageId` from a search result.
- **Returns:** everything the index holds for that agent: the fields above with the full `longDescription`, plus `elementTypes`, `type`, `platform`, `availableTo`, `isBlocked` and `version`.
- **Not found:** it returns `{"error": "No agent with packageId '...'"}` as a normal result, not with `isError` set.
- **Blocked agents:** they aren't filtered out here.

### `list_publishers`

- **Argument:** `top` (integer 1 to 1000, default `100`).
- **Returns:** `[{"publisher": "Contoso", "agentCount": 12}, ...]`, ordered by agent count, highest first.
- **Output shape:** because the result is a list, `structuredContent` wraps it as `{"result": [...]}`.
- **Blocked agents:** the counts include them.

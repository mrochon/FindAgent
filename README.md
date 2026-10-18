# FindAgent

Lets agents discover the Microsoft 365 Copilot agents available in a tenant.

```
Microsoft Graph                 indexer/                     Azure AI Search              mcp-server/
/copilot/admin/catalog/  ──►  Function App (timer)  ──►  index "agents"  ◄──  Container App (MCP, /mcp)  ◄── agents
packages                      watermark in blob                                read-only identity
```

| Component | Hosting | Identity / permissions |
|---|---|---|
| [indexer](indexer/) | Azure Functions (Flex Consumption, Python 3.12), timer trigger | User-assigned MI: Graph `CopilotPackages.Read.All` (application), Search Service Contributor + Search Index Data Contributor, Storage Blob Data Owner |
| [mcp-server](mcp-server/) | Azure Container Apps (scale to zero), image in ACR | User-assigned MI: Search Index Data Reader, AcrPull. Callers: Entra ID token with the `agent.find.all` app role |

The Package Management API requires a **Microsoft Agent 365** license in the tenant.

## How the indexer works

1. Reads the watermark (start time of the last successful run) from `indexer-state/watermark.json`.
2. Lists packages with `$filter=lastModifiedDateTime ge <watermark − 10 min>` (no filter on the first run), following `@odata.nextLink`.
3. Keeps only agents (packages whose `supportedHosts` include `Copilot`; set `AGENTS_ONLY=false` to index everything).
4. Fetches each package's details, because `longDescription` and `activeUsers` are only returned by `GET /packages/{id}`.
5. Creates or updates the index schema, merge-uploads the documents, then saves the new watermark. A failed run doesn't advance the watermark, so the next run retries it.

Index fields:

| Field | Use |
|---|---|
| `displayName`, `longDescription`, `shortDescription` | Full-text search (`en.microsoft` analyzer, `displayName` boosted) |
| `publisher` | Filterable, facetable (exact match) |
| `activeUsers` | Filterable, sortable (active users in last 30 days) |
| `supportedHosts`, `categories`, `elementTypes`, `type`, `platform`, `isBlocked`, `lastModifiedDateTime` | Extra filters |

To force a full re-index, delete `watermark.json` from the `indexer-state` container.

The index doesn't exist straight after deployment. Bicep can only create the search *service*; indexes are created through the search service's data API. The function creates or updates the index at the start of every run, so it appears after the first scheduled run (by default at the top of the next hour). To run the indexer straight away, call the function's admin endpoint with the app's master key:

```powershell
$rg = 'rg-findagent'
$app = az functionapp list -g $rg --query "[?contains(name,'indexer')].name | [0]" -o tsv
$appHost = az functionapp show -g $rg -n $app --query defaultHostName -o tsv
$key = az functionapp keys list -g $rg -n $app --query masterKey -o tsv
Invoke-RestMethod -Method Post -Uri "https://$appHost/admin/functions/index_agents" `
    -Headers @{ 'x-functions-key' = $key } -ContentType 'application/json' -Body '{}'
```

The call returns `202 Accepted` immediately. Check the run's progress and result in Application Insights (**Transaction search** or **Logs**).

## MCP tools

Endpoint: `https://<container-app-fqdn>/mcp` (Streamable HTTP). Every request needs `Authorization: Bearer <Entra ID access token>`; see "MCP server authentication".

| Tool | Purpose |
|---|---|
| `search_agents(query, publisher?, min_active_users?, max_active_users?, order_by?, top?, include_blocked?)` | Full-text search with filters; blocked agents are excluded by default |
| `get_agent(package_id)` | Full indexed record for one agent |
| `list_publishers(top?)` | Publisher values and agent counts, for the `publisher` filter |

## Deploy

Prerequisites: Azure CLI (with Bicep), Azure Functions Core Tools v4, PowerShell 7.

```powershell
# 1. Indexer + search service (see "Permissions needed to deploy" below).
./indexer/deploy.ps1 -ResourceGroup rg-findagent -Location eastus

# 2. MCP server, in the same resource group, pointed at the search service from step 1.
./mcp-server/deploy.ps1 -ResourceGroup rg-findagent -SearchServiceName <searchServiceName output>
```

The MCP server script also creates the Entra ID app registrations and gives you, the signed-in user, the `agent.find.all` role. At the end it prints a ready-to-use `.mcp.json` for Claude Code. Pass `-SkipRoleAssignment` to leave the role unassigned.

### Permissions needed to deploy

The deployment touches two separate permission systems, and the account that runs it needs rights in both:

| What the deployment does | Permission system | Role needed by the deploying account |
|---|---|---|
| Creates the Azure resources and the Azure role assignments for the managed identities | Azure RBAC, on the subscription or resource group | **Owner**, or Contributor plus Role Based Access Control Administrator (or User Access Administrator) |
| Grants the indexer's managed identity the Graph application permission `CopilotPackages.Read.All` | Microsoft Entra ID, at tenant level | **Privileged Role Administrator** or **Global Administrator** |
| Creates the MCP server's app registrations, grants tenant-wide consent for its scope, and assigns you its app role | Microsoft Entra ID, at tenant level | **Cloud Application Administrator** or **Global Administrator** |

**Global Administrator alone is not enough.** It is an Entra ID role and covers the Entra ID steps, but it gives no access to Azure subscriptions. You also need an Azure role such as Owner on the subscription or resource group. If you don't have one, either:

- ask a subscription Owner to assign you Owner on the subscription or resource group, or
- as Global Administrator, turn on **Microsoft Entra ID > Properties > Access management for Azure resources**. This gives you User Access Administrator at root scope, which you can use to assign yourself Owner. Turn the setting off again afterwards.

Check which Azure roles you have with:

```powershell
az role assignment list --assignee (az ad signed-in-user show --query id -o tsv) --all --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

The tenant also needs a **Microsoft Agent 365** license. Without it the grant succeeds, but the indexer's Graph calls fail.

#### How the Graph permission is granted

`deploy.ps1` deploys the Bicep with `assignGraphPermission=false` and then makes the Graph grant itself. A newly created managed identity can take a few minutes to replicate in Entra ID, and until then Graph rejects the grant with `Request_BadRequest: Not a valid reference update`. The script retries every 30 seconds for up to 5 minutes and skips the grant if it already exists.

If you deploy `main.bicep` directly with `assignGraphPermission=true` (the default), the first deployment can fail with that error. Run the deployment again, or grant the permission as shown below.

#### Granting the Graph permission separately

If the person deploying the Azure resources isn't an Entra ID admin, deploy with `-SkipGraphPermission`. Then have a Privileged Role Administrator or Global Administrator run the following, using the `indexerPrincipalId` output of the deployment:

```powershell
$miId = '<indexerPrincipalId output>'
$graphSpId = az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv
$roleId = az ad sp show --id 00000003-0000-0000-c000-000000000000 --query "appRoles[?value=='CopilotPackages.Read.All'].id | [0]" -o tsv
@{ principalId = $miId; resourceId = $graphSpId; appRoleId = $roleId } | ConvertTo-Json | Set-Content grant.json
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$miId/appRoleAssignments" --body '@grant.json'
Remove-Item grant.json
```

Application permissions granted this way don't need a separate admin consent step: the assignment is the consent. The grant takes effect on the identity's next token, which can take a few minutes.

### MCP server authentication

Every request to the MCP server needs a Microsoft Entra ID access token that:

- was issued by your tenant for the MCP server's API (audience = the `apiClientId` output, a v2.0 token), and
- has `agent.find.all` in its `roles` claim.

The token can be **delegated** (a signed-in user) or an **application** token (client credentials). Entra ID puts app roles in the `roles` claim in both cases, so the check is the same. A missing, expired or wrongly addressed token gets `401`; a valid token without the role gets `403`.

The deployment creates two app registrations:

| App registration | Purpose |
|---|---|
| `<baseName> MCP server` | The API. Defines the app role `agent.find.all` (assignable to users and applications) and the delegated scope `access_as_user`. App ID URI: the `apiAppIdUri` output |
| `<baseName> MCP test client` | Public client (no secret) for interactive sign-in from Claude Code, with redirect URI `http://localhost:33418/callback` |

Redeploying reuses the same app registrations. Bicep identifies each one by its `uniqueName`, which is derived from the resource group and `baseName`. So the same resource group and `baseName` always update the existing apps, keeping their client IDs, role and scope IDs, and the users and apps assigned to the role. A different resource group or `baseName` creates a new pair. Properties set in the Bicep (roles, scopes, redirect URI) are reset to the Bicep values on every deployment, so make lasting changes there rather than in the portal. Don't change `requiredRole` on an existing deployment: Entra ID won't remove an enabled app role, so the deployment fails. Disable the old role in the portal first.

The `access_as_user` scope exists only because a client needs *some* scope to request a user token; it grants nothing by itself. Access is decided by the role. "User assignment required" is turned on for the API, so users without a role assignment can't get a token for it at all.

The server does **not** publish OAuth protected resource metadata (RFC 9728) by default. MCP clients that discover it send its `resource` URL to the authorization server as the RFC 8707 `resource` parameter, and Entra ID rejects that token request with `AADSTS9010010` (the resource doesn't match the requested `api://` scope). Claude Code shows this as a sign-in that succeeds in the browser but never finishes connecting. Instead, clients are configured with the authorization server and scope directly, as in the `.mcp.json` that `deploy.ps1` prints. To publish the metadata anyway (for example, for a client that needs discovery and doesn't send `resource`), deploy with the Bicep parameter `publishResourceMetadata=true`, which sets `MCP_PUBLISH_RESOURCE_METADATA=true` on the container. The server then serves it at `/.well-known/oauth-protected-resource` and points its `401` responses to it.

#### Giving users access

Assign the `agent.find.all` role in **Microsoft Entra ID > Enterprise applications > `<baseName> MCP server` > Users and groups > Add user/group**. Assigning groups requires Entra ID P1 or P2. Or use Graph:

```powershell
$apiSpId = '<apiServicePrincipalId output>'
$appRoleId = '<appRoleId output>'
$userId = az ad user show --id someone@contoso.com --query id -o tsv
@{ principalId = $userId; resourceId = $apiSpId; appRoleId = $appRoleId } | ConvertTo-Json | Set-Content grant.json
az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$apiSpId/appRoleAssignedTo" --body '@grant.json'
Remove-Item grant.json
```

`deploy.ps1` does this for you, the signed-in user. A new assignment shows up in the next token the user gets.

#### Giving an application (agent) access

An agent that calls the server as itself uses the client credentials flow:

1. Assign the role to the calling app's service principal with the same Graph call as above, setting `principalId` to `az ad sp show --id <calling app's client id> --query id -o tsv`. This is the equivalent of granting an application permission with admin consent; in the portal it shows under the calling app's **API permissions > Add a permission > APIs my organization uses > `<baseName> MCP server` > Application permissions**.
2. The app requests a token with scope `<apiAppIdUri output>/.default` and sends it as `Authorization: Bearer <token>`.

#### Getting a token for testing

The deployment grants the Azure CLI consent for the `access_as_user` scope, so once you have the role:

```powershell
$token = az account get-access-token --scope '<userScope output>' --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token"; Accept = 'application/json, text/event-stream' }
$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
Invoke-RestMethod -Method Post -Uri '<mcpEndpoint output>' -Headers $headers -ContentType 'application/json' -Body $body
```

To check which roles a token carries, paste it into https://jwt.ms (it is decoded in your browser).

## Delete the deployment

Both components deploy into one resource group, so deleting it removes everything: the search service and index, the Function App, the Container App, the container registry, storage, the managed identities, and the monitoring resources.

```powershell
az group delete --name rg-findagent --yes
```

Add `--no-wait` to return immediately instead of waiting for the deletion to finish.

- **Graph permission:** the `CopilotPackages.Read.All` grant belongs to the indexer's managed identity, so it is removed when the identity is deleted.
- **App registrations are not deleted.** The MCP server's two app registrations live in Microsoft Entra ID, not in the resource group. Delete them separately, then purge them from Entra ID's deleted items so a later deployment doesn't collide with them (otherwise they are kept for 30 days):

  ```powershell
  az ad app list --filter "startswith(displayName,'findagent MCP')" --query "[].{name:displayName, id:id}" -o table
  az ad app list --filter "startswith(displayName,'findagent MCP')" --query "[].id" -o tsv | ForEach-Object {
    az ad app delete --id $_
    az rest --method DELETE --uri "https://graph.microsoft.com/v1.0/directory/deletedItems/$_"
  }
  ```

  Check the list first: the filter matches any app whose name starts with `findagent MCP`. Deleting the apps also removes their role assignments and consent grants.
- **Data:** the search index and the watermark blob are deleted permanently and can't be recovered.
- **Log Analytics:** the workspaces are soft-deleted and kept for 14 days. Redeploying to a resource group with the same name in that period restores them. To remove them immediately, delete them before the resource group:

  ```powershell
  az monitor log-analytics workspace list --resource-group rg-findagent --query "[].name" -o tsv |
    ForEach-Object { az monitor log-analytics workspace delete --resource-group rg-findagent --workspace-name $_ --force --yes }
  ```

To remove only the MCP server and keep the indexer running, delete its resources individually. The names come from [mcp-server/infra/main.bicep](mcp-server/infra/main.bicep):

```powershell
$rg = 'rg-findagent'
az containerapp delete --resource-group $rg --name ca-findagent-mcp --yes
az resource list --resource-group $rg --query "[?contains(name, 'findagent-mcp') || starts_with(name, 'cae-') || starts_with(name, 'crfindagent')].id" -o tsv |
  ForEach-Object { az resource delete --ids $_ }
```

That also deletes the MCP server's managed identity. Its role assignment on the search service stays behind and shows as "Identity not found" in the portal. It does no harm, and you can remove it under the search service's **Access control (IAM)** page.

## Local development

Only the code runs on your machine. It still talks to Microsoft Graph and to the Azure resources, so deploy the infrastructure first with `./indexer/deploy.ps1`. The unit tests (`python -m pytest tests` in `indexer/`) are the exception: they need nothing in Azure.

### What each component needs

| | Indexer | MCP server |
|---|---|---|
| Microsoft Graph | Application permission `CopilotPackages.Read.All` | none |
| AI Search service | Search Service Contributor + Search Index Data Contributor | Search Index Data Reader, and an index that the indexer has filled |
| Storage account (watermark) | Storage Blob Data Contributor on the deployed account | none |
| Functions host storage | [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) running locally | none |
| Tools | Python 3.12, Azure Functions Core Tools v4 | Python 3.12 |

### How credentials are picked up

The code has no keys or connection strings for Azure. It signs in with `DefaultAzureCredential`, which tries several sources in order and uses the first one that works. Two of them matter here:

1. **Environment variables:** `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` sign in as an app registration (service principal).
2. **Azure CLI:** if those aren't set, the account you signed in with using `az login` is used.

In Azure, the deployed apps instead use their managed identities.

The search service has key-based access turned off, and the storage account doesn't allow shared keys. Whichever identity you run as therefore needs the Azure roles in the table above. Role assignments can take a few minutes to take effect.

### Indexer

The Azure CLI sign-in can't get a Graph token with `CopilotPackages.Read.All`, so run the indexer as an app registration:

1. In Microsoft Entra ID, create an app registration with a client secret.
2. Add the **application** permission `Microsoft Graph > CopilotPackages.Read.All`, and have an admin grant consent.
3. Give its service principal the Search and Storage roles:

   ```powershell
   $rg = 'rg-findagent'
   $appId = '<app registration client id>'
   $search = az search service list -g $rg --query "[0].id" -o tsv
   $storage = az storage account list -g $rg --query "[0].id" -o tsv
   az role assignment create --assignee $appId --role 'Search Service Contributor' --scope $search
   az role assignment create --assignee $appId --role 'Search Index Data Contributor' --scope $search
   az role assignment create --assignee $appId --role 'Storage Blob Data Contributor' --scope $storage
   ```

4. Copy `indexer/local.settings.sample.json` to `indexer/local.settings.json`. The Functions host loads every entry under `Values` as an environment variable, which is how settings and credentials reach the code:

   | Setting | Value |
   |---|---|
   | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | The app registration from step 1 |
   | `AZURE_SEARCH_ENDPOINT` | `searchEndpoint` output of the indexer deployment |
   | `AZURE_SEARCH_INDEX` | A separate index such as `agents-dev`, so you don't change the production index (created automatically) |
   | `STATE_STORAGE_ACCOUNT_URL` | `https://<storage account>.blob.core.windows.net/` |
   | `STATE_CONTAINER` | A separate container such as `indexer-state-dev`, so you don't move the deployed watermark (created automatically) |
   | `AzureWebJobsStorage` | Leave as `UseDevelopmentStorage=true`, which points at Azurite |
   | `INDEXER_SCHEDULE`, `GRAPH_BASE_URL`, `AGENTS_ONLY` | As in the sample |

   `local.settings.json` contains a secret, so keep it out of source control.

5. Start Azurite in one terminal and the function in another:

   ```powershell
   npx azurite --silent --location .azurite
   ```

   ```powershell
   cd indexer
   python -m venv .venv; .venv/Scripts/Activate.ps1
   pip install -r requirements.txt
   func start
   ```

6. Trigger a run straight away instead of waiting for the schedule:

   ```powershell
   Invoke-RestMethod -Method Post -Uri http://localhost:7071/admin/functions/index_agents -ContentType 'application/json' -Body '{}'
   ```

### MCP server

Your own `az login` account is enough, because the MCP server only reads the index:

```powershell
$me = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $me --role 'Search Index Data Reader' --scope $search
```

Settings are plain environment variables:

| Variable | Value |
|---|---|
| `AZURE_SEARCH_ENDPOINT` | `searchEndpoint` output of the indexer deployment (required) |
| `AZURE_SEARCH_INDEX` | Index to query (default `agents`; use `agents-dev` to read what your local indexer wrote) |
| `MCP_AUTH_TENANT_ID` | `tenantId` output of the MCP server deployment |
| `MCP_AUTH_CLIENT_ID` | `apiClientId` output (the API app registration, used as the token audience) |
| `MCP_AUTH_APP_ID_URI` | `apiAppIdUri` output. Optional; defaults to `api://<MCP_AUTH_CLIENT_ID>` |
| `MCP_AUTH_REQUIRED_ROLE` | Optional. Default `agent.find.all` |
| `MCP_RESOURCE_URL` | Optional. Public URL of the endpoint, advertised to clients. Default `http://localhost:8000/mcp` |
| `MCP_AUTH_DISABLED` | Set to `true` to skip authentication entirely, for local testing only |
| `PORT` | Optional. Default `8000` |

The server refuses to start without `MCP_AUTH_TENANT_ID` and `MCP_AUTH_CLIENT_ID` unless `MCP_AUTH_DISABLED=true`. With the auth settings, the local server accepts the same tokens as the deployed one, because the app registrations are shared.

```powershell
cd mcp-server
python -m venv .venv; .venv/Scripts/Activate.ps1
pip install -r requirements.txt
$env:AZURE_SEARCH_ENDPOINT = 'https://<search>.search.windows.net'
$env:MCP_AUTH_TENANT_ID = '<tenantId>'
$env:MCP_AUTH_CLIENT_ID = '<apiClientId>'
$env:MCP_AUTH_APP_ID_URI = '<apiAppIdUri>'
python server.py
```

Connect a client to `http://localhost:8000/mcp`, for example the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`npx @modelcontextprotocol/inspector`) with a bearer token from "Getting a token for testing", or Claude Code as described below.

The auth tests (`python -m pytest tests` in `mcp-server/`) sign their own tokens and need nothing in Azure.

If you set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` in the same shell, the server signs in to AI Search as that app registration instead of your `az login` account. That app registration then needs Search Index Data Reader as well. These variables are separate from the `MCP_AUTH_*` ones, which describe the tokens the server *accepts*.

## Testing the MCP server from Claude Code (VS Code panel or CLI)

Claude Code loads MCP servers from `.mcp.json` in the workspace root, in both the VS Code Claude panel and the `claude` CLI. `.mcp.json` is gitignored because it holds tenant-specific values; [.mcp.example.json](.mcp.example.json) is the template.

### Option 1: sign in with Entra ID (OAuth)

Claude Code signs you in through the browser, using the test client app registration. Entra ID doesn't support dynamic client registration, so the client ID, callback port and scope are configured in advance:

1. Make sure the index has data: the indexer must have run at least once (see "How the indexer works").
2. Copy the `.mcp.json` printed at the end of `./mcp-server/deploy.ps1` into the workspace root. Or copy `.mcp.example.json` and fill in the deployment outputs. Claude Code doesn't expand environment variables inside `oauth`, so the values must be literal. To test a local server, change `url` to `http://localhost:8000/mcp`.
3. Open the `FindAgent` folder in VS Code and start a new conversation in the Claude panel. Approve the `agent-finder` project server when asked.
4. Type `/mcp`, select `agent-finder` and choose to authenticate. Sign in in the browser with an account that has the `agent.find.all` role.
5. Ask in plain language, for example:
   - "Use agent-finder to find agents that help with HR questions."
   - "Which publishers have agents? List the top 5 by agent count."
   - "Find agents from Contoso with at least 50 active users, sorted by active users."
   - "Show the full details of the first result."

### Option 2: a bearer token from the Azure CLI

If OAuth sign-in fails, pass a token directly. Tokens expire after about an hour, so this suits short tests:

```json
{
  "mcpServers": {
    "agent-finder": {
      "type": "http",
      "url": "<mcpEndpoint output or http://localhost:8000/mcp>",
      "headers": { "Authorization": "Bearer ${FINDAGENT_MCP_TOKEN}" }
    }
  }
}
```

```powershell
[Environment]::SetEnvironmentVariable('FINDAGENT_MCP_TOKEN', (az account get-access-token --scope '<userScope output>' --query accessToken -o tsv), 'User')
```

Claude Code reads `${...}` values from its process environment only. It doesn't load `.env` files, and VS Code only sees variables that existed when it started, so restart VS Code after setting the token, and again whenever you refresh it.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Connection refused | Local server isn't running, or `url` points at the wrong address |
| Browser sign-in shows `AADSTS50105` | Your account isn't assigned the `agent.find.all` role (user assignment is required) |
| Browser sign-in shows `AADSTS50011` (redirect URI mismatch) | `callbackPort` doesn't match the test client's redirect URI (`33418` by default) |
| Browser sign-in shows `AADSTS9010010` | Entra ID rejected the `resource` parameter Claude Code sends; use option 2 |
| `401` with `invalid_token` | Token expired, issued for another API, or from another tenant |
| `403` with `insufficient_scope` | Token is valid but has no `agent.find.all` role. Check it at https://jwt.ms; sign in again after the role is assigned |
| Tools fail with authorization errors from AI Search | The server's own identity lacks Search Index Data Reader |
| Tools return "index not found" | The indexer hasn't run yet |

## Known limitations

- **Stale usage numbers:** `activeUsers` changes without `lastModifiedDateTime` changing, so incremental runs don't refresh it. Do a periodic full re-index (delete the watermark) if you need current figures.
- **Deletions:** agents removed from the catalog stay in the index until it's rebuilt.
- **Same resource group:** the MCP server Bicep assumes the search service is in the same resource group as the MCP server.

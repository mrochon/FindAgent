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
| [mcp-server](mcp-server/) | Azure Container Apps (scale to zero), image in ACR | User-assigned MI: Search Index Data Reader, AcrPull |

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

Endpoint: `https://<container-app-fqdn>/mcp` (Streamable HTTP). Send the API key as `x-api-key: <key>` or `Authorization: Bearer <key>`.

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
./mcp-server/deploy.ps1 -ResourceGroup rg-findagent -SearchServiceName <searchServiceName output> -ApiKey <secret>
```

### Permissions needed to deploy

The deployment touches two separate permission systems, and the account that runs it needs rights in both:

| What the deployment does | Permission system | Role needed by the deploying account |
|---|---|---|
| Creates the Azure resources and the Azure role assignments for the managed identities | Azure RBAC, on the subscription or resource group | **Owner**, or Contributor plus Role Based Access Control Administrator (or User Access Administrator) |
| Grants the indexer's managed identity the Graph application permission `CopilotPackages.Read.All` | Microsoft Entra ID, at tenant level | **Privileged Role Administrator** or **Global Administrator** |

**Global Administrator alone is not enough.** It is an Entra ID role and covers the Graph grant, but it gives no access to Azure subscriptions. You also need an Azure role such as Owner on the subscription or resource group. If you don't have one, either:

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

### The MCP API key

The API key is not issued by Azure. You create it yourself: it's a shared secret that you choose, pass to `deploy.ps1`, and give to the agents that call the MCP server. Generate a long random value, for example:

```powershell
$apiKey = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
./mcp-server/deploy.ps1 -ResourceGroup rg-findagent -SearchServiceName <searchServiceName> -ApiKey $apiKey
```

The deployment stores it as the Container App secret `mcp-api-key`, which the server reads as `MCP_API_KEY`. Keep a copy somewhere safe, such as Azure Key Vault or your password manager. To read it back from the deployed app:

```powershell
az containerapp secret show --resource-group rg-findagent --name ca-findagent-mcp --secret-name mcp-api-key --query value -o tsv
```

To rotate the key, run `deploy.ps1` again with a new `-ApiKey` value, then update the callers.

Pass the same `-ApiKey` on every redeploy. The script refuses to run without it unless you pass `-AllowAnonymous`, which removes the key so the server no longer requires one.

## Delete the deployment

Both components deploy into one resource group, so deleting it removes everything: the search service and index, the Function App, the Container App, the container registry, storage, the managed identities, and the monitoring resources.

```powershell
az group delete --name rg-findagent --yes
```

Add `--no-wait` to return immediately instead of waiting for the deletion to finish.

- **Graph permission:** the `CopilotPackages.Read.All` grant belongs to the indexer's managed identity, so it is removed when the identity is deleted. Nothing is left behind in Microsoft Entra ID.
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
| `MCP_API_KEY` | Optional. If set, clients must send it; if not set, no key is required |
| `PORT` | Optional. Default `8000` |

```powershell
cd mcp-server
python -m venv .venv; .venv/Scripts/Activate.ps1
pip install -r requirements.txt
$env:AZURE_SEARCH_ENDPOINT = 'https://<search>.search.windows.net'
python server.py
```

Connect a client to `http://localhost:8000/mcp`, either with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`npx @modelcontextprotocol/inspector`) or by adding the server to your agent. In VS Code, for example, add it to `.vscode/mcp.json`:

```json
{
  "servers": {
    "agent-finder": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "x-api-key": "<MCP_API_KEY, if set>" }
    }
  }
}
```

If you set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` in the same shell, the server signs in as that app registration instead of your `az login` account. That app registration then needs Search Index Data Reader as well.

## Testing the MCP server from Claude Code (VS Code panel or CLI)

Claude Code loads MCP servers from `.mcp.json` in the workspace root, in both the VS Code Claude panel and the `claude` CLI. `.mcp.json` is gitignored because it can hold the API key. The repository contains the template [.mcp.example.json](.mcp.example.json) instead, which registers the server as `agent-finder`. Copy it to `.mcp.json`, then either put the URL and key directly into your copy or leave the template's variables in place. Those variables are:

| Variable | Default | Set it to |
|---|---|---|
| `FINDAGENT_MCP_URL` | `http://localhost:8000/mcp` | The `mcpEndpoint` output of the MCP server deployment, to test the deployed server |
| `FINDAGENT_MCP_KEY` | empty | The API key, if the server requires one |

Steps:

1. Make sure the index has data: the indexer must have run at least once (see "How the indexer works").
2. Start the server locally (see "MCP server" above), or deploy it.
3. If you're using the deployed server, or a local one with `MCP_API_KEY` set, set the variables as user environment variables. VS Code only sees variables that existed when it started, so restart VS Code afterwards:

   ```powershell
   [Environment]::SetEnvironmentVariable('FINDAGENT_MCP_URL', 'https://<fqdn>/mcp', 'User')
   [Environment]::SetEnvironmentVariable('FINDAGENT_MCP_KEY', '<api key>', 'User')
   ```

   Claude Code reads these from its process environment only. It doesn't load `.env` files, so a gitignored `.env` won't work. If you'd rather not set user environment variables, register a private copy of the server instead. It's stored in your `~/.claude.json`, outside the repository, and takes precedence over the `.mcp.json` entry of the same name:

   ```powershell
   claude mcp add --transport http agent-finder https://<fqdn>/mcp --header "x-api-key: <api key>"
   ```

4. Open the `FindAgent` folder in VS Code and start a new conversation in the Claude panel. The first time, Claude Code asks you to approve the project's MCP server; approve `agent-finder`.
5. Type `/mcp` in the panel to check that `agent-finder` is connected and lists its three tools.
6. Ask in plain language, for example:
   - "Use agent-finder to find agents that help with HR questions."
   - "Which publishers have agents? List the top 5 by agent count."
   - "Find agents from Contoso with at least 50 active users, sorted by active users."
   - "Show the full details of the first result."

If `/mcp` shows the server as failed:

| Symptom | Cause |
|---|---|
| Connection refused | Local server isn't running, or `FINDAGENT_MCP_URL` points at the wrong address |
| 401 Unauthorized | `FINDAGENT_MCP_KEY` is missing or doesn't match the server's `MCP_API_KEY` (or VS Code wasn't restarted after setting it) |
| Tools work but return `403` or authorization errors | The server's identity lacks Search Index Data Reader |
| Tools return "index not found" | The indexer hasn't run yet |

## Known limitations

- **Stale usage numbers:** `activeUsers` changes without `lastModifiedDateTime` changing, so incremental runs don't refresh it. Do a periodic full re-index (delete the watermark) if you need current figures.
- **Deletions:** agents removed from the catalog stay in the index until it's rebuilt.
- **Same resource group:** the MCP server Bicep assumes the search service is in the same resource group as the MCP server.

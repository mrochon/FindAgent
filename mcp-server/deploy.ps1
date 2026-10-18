<#
.SYNOPSIS
  Provisions the MCP server's Azure resources and Entra ID app registrations, builds the image in ACR,
  deploys it, and grants the signed-in user the agent.find.all role.
  Run the indexer deployment first; pass its searchServiceName output here.
#>
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$SearchServiceName,
    [string]$BaseName = 'findagent',
    [string]$IndexName = 'agents',
    # Subscription name or ID. Defaults to the Azure CLI's current subscription.
    [string]$Subscription,
    # Don't assign the role to the signed-in user (assign it to others later; see README).
    [switch]$SkipRoleAssignment
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../scripts/Graph.ps1"

$azureCliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'

$account = Resolve-Subscription $Subscription
$sub = $account.id
$GraphSubscriptionId = $sub

function Grant-AppRole([string]$PrincipalId, [string]$ApiSpId, [string]$AppRoleId) {
    $path = "servicePrincipals/$ApiSpId/appRoleAssignedTo"
    $existing = try { (Invoke-Graph GET $path).value | Where-Object { $_.principalId -eq $PrincipalId -and $_.appRoleId -eq $AppRoleId } } catch { $null }
    if ($existing) { Write-Host 'Role already assigned to the signed-in user.'; return }
    Invoke-GraphPostWithRetry $path @{ principalId = $PrincipalId; resourceId = $ApiSpId; appRoleId = $AppRoleId } 'assign role to signed-in user'
}

# Tenant-wide consent so the client can request the delegated scope without a consent prompt.
function Grant-Consent([string]$ClientSpId, [string]$ApiSpId, [string]$ClientName) {
    $filter = [uri]::EscapeDataString("clientId eq '$ClientSpId' and resourceId eq '$ApiSpId'")
    $existing = try { (Invoke-Graph GET "oauth2PermissionGrants?`$filter=$filter").value } catch { $null }
    if ($existing) { Write-Host "Consent already granted for $ClientName."; return }
    Invoke-GraphPostWithRetry 'oauth2PermissionGrants' `
        @{ clientId = $ClientSpId; consentType = 'AllPrincipals'; resourceId = $ApiSpId; scope = 'access_as_user' } "consent for $ClientName"
}

$template = "$PSScriptRoot/infra/main.bicep"
$common = @("baseName=$BaseName", "searchServiceName=$SearchServiceName", "indexName=$IndexName")

# 1. Registry, environment and app registrations (no app yet, since there is no image to run).
$outputs = az deployment group create --subscription $sub --resource-group $ResourceGroup --template-file $template `
    --parameters @common --query properties.outputs --output json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Infrastructure deployment failed.' }

# 2. Build the image in ACR.
$image = "$($outputs.registryLoginServer.value)/agent-search-mcp:$(Get-Date -Format 'yyyyMMddHHmmss')"
az acr build --subscription $sub --registry $outputs.registryName.value --image ($image -split '/', 2)[1] $PSScriptRoot
if ($LASTEXITCODE) { throw 'Image build failed.' }

# 3. Deploy the container app.
$outputs = az deployment group create --subscription $sub --resource-group $ResourceGroup --template-file $template `
    --parameters @common "containerImage=$image" --query properties.outputs --output json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Container app deployment failed.' }

# 4. Entra ID: consent for the test client and Azure CLI, and the role for the signed-in user.
$apiSpId = $outputs.apiServicePrincipalId.value
Grant-Consent $outputs.testClientServicePrincipalId.value $apiSpId 'test client'
$azureCliSp = Get-ServicePrincipalByAppId $azureCliAppId
if ($azureCliSp) { Grant-Consent $azureCliSp.id $apiSpId 'Azure CLI' }
if (-not $SkipRoleAssignment) {
    Grant-AppRole (Invoke-Graph GET 'me?$select=id').id $apiSpId $outputs.appRoleId.value
}

$mcpJson = [ordered]@{
    mcpServers = [ordered]@{
        'agent-finder' = [ordered]@{
            type  = 'http'
            url   = $outputs.mcpEndpoint.value
            oauth = [ordered]@{
                clientId              = $outputs.testClientId.value
                callbackPort          = $outputs.testClientCallbackPort.value
                scopes                = $outputs.userScope.value
                authServerMetadataUrl = "https://login.microsoftonline.com/$($outputs.tenantId.value)/v2.0/.well-known/openid-configuration"
            }
        }
    }
} | ConvertTo-Json -Depth 5

Write-Host "`nMCP endpoint: $($outputs.mcpEndpoint.value)"
Write-Host "API (audience): $($outputs.apiClientId.value)  scope: $($outputs.userScope.value)"
Write-Host "`n.mcp.json for Claude Code:`n$mcpJson"

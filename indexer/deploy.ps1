<#
.SYNOPSIS
  Provisions the indexer's Azure resources, grants the Graph permission and publishes the function code.
#>
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [string]$Location = 'eastus',
    [string]$BaseName = 'findagent',
    # Subscription name or ID. Defaults to the Azure CLI's current subscription.
    [string]$Subscription,
    [switch]$SkipGraphPermission
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../scripts/Graph.ps1"

$graphAppId = '00000003-0000-0000-c000-000000000000'
$graphRole = 'CopilotPackages.Read.All'

$account = Resolve-Subscription $Subscription
$sub = $account.id
$GraphSubscriptionId = $sub

function Grant-GraphPermission([string]$PrincipalId) {
    $graphSp = Get-ServicePrincipalByAppId $graphAppId
    $roleId = ($graphSp.appRoles | Where-Object value -eq $graphRole).id
    if (-not $roleId) { throw "Graph app role $graphRole not found in tenant $($account.tenantId)." }

    $path = "servicePrincipals/$PrincipalId/appRoleAssignments"
    $existing = try { (Invoke-Graph GET $path).value | Where-Object appRoleId -eq $roleId } catch { $null }
    if ($existing) { Write-Host "$graphRole already granted."; return }
    Invoke-GraphPostWithRetry $path @{ principalId = $PrincipalId; resourceId = $graphSp.id; appRoleId = $roleId } "grant $graphRole to the indexer identity"
}

az group create --subscription $sub --name $ResourceGroup --location $Location --output none
if ($LASTEXITCODE) { throw 'Resource group creation failed.' }

# The Graph grant is done below rather than in Bicep, so it can wait for the new identity to replicate.
$outputs = az deployment group create `
    --subscription $sub `
    --resource-group $ResourceGroup `
    --template-file "$PSScriptRoot/infra/main.bicep" `
    --parameters baseName=$BaseName assignGraphPermission=false `
    --query properties.outputs --output json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Bicep deployment failed.' }

if (-not $SkipGraphPermission) {
    Grant-GraphPermission $outputs.indexerPrincipalId.value
}

Push-Location $PSScriptRoot
try {
    # Core Tools publishes to the CLI's current subscription, so make sure that's the one we deployed to.
    az account set --subscription $sub
    func azure functionapp publish $outputs.functionAppName.value --python
    if ($LASTEXITCODE) { throw 'Function publish failed.' }
}
finally {
    Pop-Location
}

Write-Host "Search service: $($outputs.searchServiceName.value)"
Write-Host "Index '$($outputs.indexName.value)' is created by the function on its first scheduled run (default: top of every hour)."

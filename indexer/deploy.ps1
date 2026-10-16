<#
.SYNOPSIS
  Provisions the indexer's Azure resources, grants the Graph permission and publishes the function code.
#>
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [string]$Location = 'eastus',
    [string]$BaseName = 'findagent',
    [switch]$SkipGraphPermission
)
$ErrorActionPreference = 'Stop'

$graphAppId = '00000003-0000-0000-c000-000000000000'
$graphRole = 'CopilotPackages.Read.All'

function Grant-GraphPermission([string]$PrincipalId) {
    $graphSpId = az ad sp show --id $graphAppId --query id -o tsv
    $roleId = az ad sp show --id $graphAppId --query "appRoles[?value=='$graphRole'].id | [0]" -o tsv
    if (-not $roleId) { throw "Graph app role $graphRole not found in this tenant." }

    $uri = "https://graph.microsoft.com/v1.0/servicePrincipals/$PrincipalId/appRoleAssignments"
    $body = Join-Path ([IO.Path]::GetTempPath()) "grant-$PrincipalId.json"
    @{ principalId = $PrincipalId; resourceId = $graphSpId; appRoleId = $roleId } | ConvertTo-Json | Set-Content $body
    try {
        # A newly created managed identity can take a few minutes to replicate in Entra ID;
        # until then Graph rejects the assignment ("Not a valid reference update").
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            $existing = az rest --method GET --uri $uri --query "value[?appRoleId=='$roleId'] | length(@)" -o tsv 2>$null
            if ($existing -gt 0) { Write-Host "$graphRole already granted."; return }

            az rest --method POST --uri $uri --body "@$body" --output none 2>$null
            if (-not $LASTEXITCODE) { Write-Host "Granted $graphRole."; return }

            Write-Host "Identity not ready in Entra ID yet; retrying in 30s ($attempt/10)..."
            Start-Sleep -Seconds 30
        }
        throw "Could not grant $graphRole to $PrincipalId. See 'Granting the Graph permission separately' in README.md."
    }
    finally {
        Remove-Item $body -ErrorAction SilentlyContinue
    }
}

az group create --name $ResourceGroup --location $Location --output none
if ($LASTEXITCODE) { throw 'Resource group creation failed.' }

# The Graph grant is done below rather than in Bicep, so it can wait for the new identity to replicate.
$outputs = az deployment group create `
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
    func azure functionapp publish $outputs.functionAppName.value --python
    if ($LASTEXITCODE) { throw 'Function publish failed.' }
}
finally {
    Pop-Location
}

Write-Host "Search service: $($outputs.searchServiceName.value)"
Write-Host "Index '$($outputs.indexName.value)' is created by the function on its first scheduled run (default: top of every hour)."

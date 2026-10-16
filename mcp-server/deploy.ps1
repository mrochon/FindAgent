<#
.SYNOPSIS
  Provisions the MCP server's Azure resources, builds the image in ACR and deploys it.
  Run the indexer deployment first; pass its searchServiceName output here.
#>
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$SearchServiceName,
    [string]$BaseName = 'findagent',
    [string]$IndexName = 'agents',
    [string]$ApiKey,
    [switch]$AllowAnonymous
)
$ErrorActionPreference = 'Stop'

if (-not $ApiKey -and -not $AllowAnonymous) {
    throw 'Pass -ApiKey <key> to protect the endpoint, or -AllowAnonymous to deploy without authentication.'
}

$template = "$PSScriptRoot/infra/main.bicep"
$common = @("baseName=$BaseName", "searchServiceName=$SearchServiceName", "indexName=$IndexName")

# 1. Registry and environment (no app yet, since there is no image to run).
$outputs = az deployment group create --resource-group $ResourceGroup --template-file $template `
    --parameters @common --query properties.outputs --output json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Infrastructure deployment failed.' }

# 2. Build the image in ACR.
$image = "$($outputs.registryLoginServer.value)/agent-search-mcp:$(Get-Date -Format 'yyyyMMddHHmmss')"
az acr build --registry $outputs.registryName.value --image ($image -split '/', 2)[1] $PSScriptRoot
if ($LASTEXITCODE) { throw 'Image build failed.' }

# 3. Deploy the container app.
$outputs = az deployment group create --resource-group $ResourceGroup --template-file $template `
    --parameters @common "containerImage=$image" "mcpApiKey=$ApiKey" `
    --query properties.outputs --output json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Container app deployment failed.' }

Write-Host "MCP endpoint: $($outputs.mcpEndpoint.value)"

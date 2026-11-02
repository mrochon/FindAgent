# Install-Module Microsoft.Graph.Authentication -Scope CurrentUser   # once
az account show --query tenantId -o tsv | ForEach-Object { Connect-MgGraph -TenantId $_ -Scopes 'CopilotPackages.ReadWrite.All' }

$copilotAgentId = "T_fa953482-bcf0-454e-cafe-a3a72be688f2"
$now = (Get-Date).ToUniversalTime().ToString('o')
$body = @{
    shortDescription           = 'MR Sales Agent'
    supportedHosts             = @("SAP")
    publisher                  = "MR Store"
    elementTypes               = @('declarativeAgent')
    governanceMetadata         = "PromptAgent"
}
$resp = Invoke-MgGraphRequest PATCH "beta/copilot/admin/catalog/packages/$copilotAgentId" -Body ($body | ConvertTo-Json -Depth 6) -ContentType 'application/json'
$resp | ConvertTo-Json -Depth 10 | Write-Output

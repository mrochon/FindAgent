# Install-Module Microsoft.Graph.Authentication -Scope CurrentUser   # once
az account show --query tenantId -o tsv | ForEach-Object { Connect-MgGraph -TenantId $_ -Scopes 'AgentRegistration.ReadWrite.All' }

$me  = (Invoke-MgGraphRequest GET 'v1.0/me?$select=id').id
$now = (Get-Date).ToUniversalTime().ToString('o')
$body = @{
    displayName                = 'MR Sales Agent'
    description                = 'Helps users search for customer sales related data. Includes sales data, customer data, current product data, order data and recommends future purchases.'
    createdBy                  = $me
    ownerIds                   = @($me)
    sourceCreatedDateTime      = $now
    sourceLastModifiedDateTime = $now
    agentCard = @{
        name               = 'MR Sales Agent'
        version            = '1.0.0'
        description        = 'Helps users search for customer sales related data. Includes sales data, customer data, current product data, order data and recommends future purchases.'
        provider           = 'MRStore'
        capabilities       = @{ streaming = $false; pushNotifications = $false }
        defaultInputModes  = @('text')
        defaultOutputModes = @('text')
        skills = @(@{ id = 'get-customer'; name = 'Get Customer Data'; description = 'Search for customer sales data' })
    }
}
Invoke-MgGraphRequest POST 'beta/copilot/agentRegistrations' -Body ($body | ConvertTo-Json -Depth 6) -ContentType 'application/json'

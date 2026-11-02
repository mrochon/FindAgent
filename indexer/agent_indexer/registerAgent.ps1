Install-Module Microsoft.Graph.Authentication -Scope CurrentUser   # once
Connect-MgGraph -TenantId 7d1abfb9-9f4e-4ec6-8280-722dd7bf9b50 -Scopes 'AgentRegistration.ReadWrite.All'

$me  = (Invoke-MgGraphRequest GET 'v1.0/me?$select=id').id
$now = (Get-Date).ToUniversalTime().ToString('o')
$body = @{
    displayName                = 'Contoso Travel Booking Agent'
    description                = 'Helps users search and book flights and hotels'
    createdBy                  = $me
    ownerIds                   = @($me)
    sourceCreatedDateTime      = $now
    sourceLastModifiedDateTime = $now
    agentCard = @{
        name               = 'Contoso Travel Booking Agent'
        version            = '1.0.0'
        description        = 'Helps users search and book flights and hotels'
        provider           = 'Contoso'
        capabilities       = @{ streaming = $false; pushNotifications = $false }
        defaultInputModes  = @('text')
        defaultOutputModes = @('text')
        skills = @(@{ id = 'book-flight'; name = 'Book Flight'; description = 'Search and book flights' })
    }
}
Invoke-MgGraphRequest POST 'beta/copilot/agentRegistrations' -Body ($body | ConvertTo-Json -Depth 6) -ContentType 'application/json'

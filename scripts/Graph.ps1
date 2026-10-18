<#
  Microsoft Graph helpers shared by the deploy scripts. Dot-source this file and set $GraphSubscriptionId first.

  Graph tokens are requested for the deployment subscription, not the Azure CLI's default one. That gets
  a token for the subscription's tenant, signed in as the account that has access to that subscription.
  (`--tenant` alone isn't enough: it uses the default account, which may not exist in that tenant.)
#>

function Invoke-Graph([string]$Method, [string]$Path, [hashtable]$Body) {
    $token = az account get-access-token --subscription $GraphSubscriptionId --resource-type ms-graph --query accessToken -o tsv
    if ($LASTEXITCODE -or -not $token) {
        throw "Could not get a Microsoft Graph token for subscription $GraphSubscriptionId. Run az login with an account in its tenant."
    }
    $request = @{
        Method  = $Method
        Uri     = "https://graph.microsoft.com/v1.0/$Path"
        Headers = @{ Authorization = "Bearer $token" }
    }
    if ($Body) {
        $request.Body = $Body | ConvertTo-Json -Depth 5
        $request.ContentType = 'application/json'
    }
    Invoke-RestMethod @request
}

# Newly created identities and app registrations take a few minutes to replicate in Entra ID.
# Until then Graph reports them as invalid references, so only those errors are retried.
function Invoke-GraphPostWithRetry([string]$Path, [hashtable]$Body, [string]$What) {
    for ($attempt = 1; ; $attempt++) {
        try {
            Invoke-Graph POST $Path $Body | Out-Null
            Write-Host "Done: $What."
            return
        }
        catch {
            $message = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
            $replicating = $message -match 'Not a valid reference|does not exist|Request_ResourceNotFound'
            if (-not $replicating -or $attempt -ge 10) { throw "Failed: $What. $message" }
            Write-Host "Waiting for Entra ID replication ($What); retrying in 30s ($attempt/10)..."
            Start-Sleep -Seconds 30
        }
    }
}

function Get-ServicePrincipalByAppId([string]$AppId) {
    (Invoke-Graph GET "servicePrincipals?`$filter=appId eq '$AppId'&`$select=id,appRoles").value | Select-Object -First 1
}

# Resolves the subscription to deploy to (the CLI default if none is given) and returns it with its tenant.
function Resolve-Subscription([string]$Subscription) {
    $json = if ($Subscription) { az account show --subscription $Subscription -o json } else { az account show -o json }
    if ($LASTEXITCODE) { throw 'Not signed in to the Azure CLI, or the subscription was not found. Run az login.' }
    $account = $json | ConvertFrom-Json
    Write-Host "Subscription: $($account.name) ($($account.id))  tenant: $($account.tenantId)  account: $($account.user.name)"
    $account
}

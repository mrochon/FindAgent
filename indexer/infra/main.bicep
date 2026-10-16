// Agent indexer: Azure AI Search service + Flex Consumption Function App that indexes
// Copilot agent packages from Microsoft Graph on a schedule.
extension microsoftGraphV1

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Short lowercase prefix used in resource names.')
@minLength(3)
@maxLength(12)
param baseName string = 'findagent'

@description('AI Search SKU. Basic or higher is recommended.')
@allowed(['free', 'basic', 'standard', 'standard2', 'standard3'])
param searchSku string = 'basic'

@description('Name of the search index the indexer creates and fills.')
param indexName string = 'agents'

@description('NCRONTAB schedule for the indexer (default: hourly).')
param indexerSchedule string = '0 0 * * * *'

@description('Graph API root. Use https://graph.microsoft.com/beta for preview properties.')
param graphBaseUrl string = 'https://graph.microsoft.com/v1.0'

@description('Only index packages hosted in Copilot (agents), skipping plain Teams apps and Office add-ins.')
param agentsOnly bool = true

@description('Grant the CopilotPackages.Read.All Graph application permission to the indexer identity. Requires the deployer to be able to grant app roles (e.g. Privileged Role Administrator). On a first deployment this can fail with "Not a valid reference update" while the new identity replicates in Entra ID; rerun, or leave false and grant it afterwards (deploy.ps1 does this).')
param assignGraphPermission bool = true

var suffix = uniqueString(resourceGroup().id, baseName)
var name = toLower(baseName)
var deploymentContainer = 'deploymentpackage'
var stateContainer = 'indexer-state'

var roles = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}-indexer'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}-indexer-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}-indexer-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${suffix}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }

  resource blobService 'blobServices' = {
    name: 'default'

    resource deployment 'containers' = {
      name: deploymentContainer
    }

    resource state 'containers' = {
      name: stateContainer
    }
  }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: 'srch-${name}-${suffix}'
  location: location
  sku: { name: searchSku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    // Entra ID (RBAC) only; no admin/query keys.
    disableLocalAuth: true
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${name}-indexer-${suffix}'
  location: location
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-${name}-indexer-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
  }

  resource settings 'config' = {
    name: 'appsettings'
    properties: {
      AzureWebJobsStorage__accountName: storage.name
      AzureWebJobsStorage__credential: 'managedidentity'
      AzureWebJobsStorage__clientId: identity.properties.clientId
      APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
      APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identity.properties.clientId};Authorization=AAD'
      AZURE_CLIENT_ID: identity.properties.clientId
      AZURE_SEARCH_ENDPOINT: 'https://${search.name}.search.windows.net'
      AZURE_SEARCH_INDEX: indexName
      STATE_STORAGE_ACCOUNT_URL: storage.properties.primaryEndpoints.blob
      STATE_CONTAINER: stateContainer
      GRAPH_BASE_URL: graphBaseUrl
      INDEXER_SCHEDULE: indexerSchedule
      AGENTS_ONLY: string(agentsOnly)
    }
  }
}

// --- Azure RBAC for the indexer identity ---

resource storageBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, roles.storageBlobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataOwner)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource searchDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identity.id, roles.searchIndexDataContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Needed to create/update the index definition.
resource searchServiceContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identity.id, roles.searchServiceContributor)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, identity.id, roles.monitoringMetricsPublisher)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// --- Microsoft Graph application permission ---

resource graphSp 'Microsoft.Graph/servicePrincipals@v1.0' existing = if (assignGraphPermission) {
  appId: '00000003-0000-0000-c000-000000000000'
}

resource copilotPackagesRead 'Microsoft.Graph/appRoleAssignedTo@v1.0' = if (assignGraphPermission) {
  principalId: identity.properties.principalId
  resourceId: graphSp!.id
  appRoleId: filter(graphSp!.appRoles, role => role.value == 'CopilotPackages.Read.All')[0].id
}

output functionAppName string = functionApp.name
output searchServiceName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output indexName string = indexName
output indexerPrincipalId string = identity.properties.principalId

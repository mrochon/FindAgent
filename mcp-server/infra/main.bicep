// Agent search MCP server: Container Apps app (+ registry) with read-only access to the
// AI Search index created by the indexer deployment.

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Short lowercase prefix used in resource names.')
@minLength(3)
@maxLength(12)
param baseName string = 'findagent'

@description('Name of the existing AI Search service (output of the indexer deployment). Must be in this resource group.')
param searchServiceName string

@description('Name of the search index to query.')
param indexName string = 'agents'

@description('Container image to run. Leave empty on the first deployment to create only the registry.')
param containerImage string = ''

@description('API key clients must send as x-api-key or a bearer token. Empty disables authentication.')
@secure()
param mcpApiKey string = ''

var suffix = uniqueString(resourceGroup().id, baseName)
var name = toLower(baseName)

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}-mcp'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}-mcp-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${name}${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: searchServiceName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, roles.acrPull)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource searchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identity.id, roles.searchIndexDataReader)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${name}-${suffix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (!empty(containerImage)) {
  name: 'ca-${name}-mcp'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: empty(mcpApiKey) ? [] : [{ name: 'mcp-api-key', value: mcpApiKey }]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            [
              { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
              { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
              { name: 'AZURE_SEARCH_INDEX', value: indexName }
            ],
            empty(mcpApiKey) ? [] : [{ name: 'MCP_API_KEY', secretRef: 'mcp-api-key' }]
          )
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 5
      }
    }
  }
  dependsOn: [acrPull]
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output mcpEndpoint string = empty(containerImage) ? '' : 'https://${app!.properties.configuration.ingress.fqdn}/mcp'

// Agent search MCP server: Container Apps app (+ registry) with read-only access to the
// AI Search index created by the indexer deployment, protected by Microsoft Entra ID.
extension microsoftGraphV1

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

@description('App role callers must hold. Users and applications can both be assigned it.')
param requiredRole string = 'agent.find.all'

@description('Localhost port registered as redirect URI (http://localhost:<port>/callback) for the interactive test client, e.g. Claude Code.')
param testClientCallbackPort int = 33418

@description('Serve the OAuth protected resource metadata (RFC 9728). Off by default: MCP clients that discover it send its resource URL as the RFC 8707 resource parameter, which Entra ID rejects (AADSTS9010010).')
param publishResourceMetadata bool = false

var suffix = uniqueString(resourceGroup().id, baseName)
var name = toLower(baseName)

var appName = 'ca-${name}-mcp'
var appIdUri = 'api://${tenant().tenantId}/${name}-mcp-${suffix}'
var userScope = 'access_as_user'
var userScopeId = guid(resourceGroup().id, baseName, 'scope', userScope)
var appRoleId = guid(resourceGroup().id, baseName, 'role', requiredRole)

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

// --- Microsoft Entra ID ---

// The MCP server's API. Tokens are issued for it, and the app role is defined on it.
resource apiApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-mcp-api-${suffix}'
  displayName: '${name} MCP server'
  signInAudience: 'AzureADMyOrg'
  identifierUris: [appIdUri]
  api: {
    requestedAccessTokenVersion: 2
    // Interactive clients need a delegated scope to request a user token; access itself is decided by the role.
    oauth2PermissionScopes: [
      {
        id: userScopeId
        value: userScope
        type: 'User'
        isEnabled: true
        adminConsentDisplayName: 'Search the agent catalog'
        adminConsentDescription: 'Allows the app to search the agent catalog on behalf of the signed-in user.'
        userConsentDisplayName: 'Search the agent catalog'
        userConsentDescription: 'Allows the app to search the agent catalog on your behalf.'
      }
    ]
  }
  appRoles: [
    {
      id: appRoleId
      value: requiredRole
      displayName: 'Find agents'
      description: 'Search the catalog of Copilot agents through the MCP server.'
      allowedMemberTypes: ['User', 'Application']
      isEnabled: true
    }
  ]
}

resource apiSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: apiApp.appId
  // Only users assigned a role can get a token for the API.
  appRoleAssignmentRequired: true
}

// Public client for interactive sign-in from MCP clients such as Claude Code (no secret, PKCE).
resource testClient 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${name}-mcp-client-${suffix}'
  displayName: '${name} MCP test client'
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: true
  publicClient: {
    redirectUris: ['http://localhost:${testClientCallbackPort}/callback']
  }
  requiredResourceAccess: [
    {
      resourceAppId: apiApp.appId
      resourceAccess: [{ id: userScopeId, type: 'Scope' }]
    }
  ]
}

resource testClientSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: testClient.appId
}

// --- Container Apps ---

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
  name: appName
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
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
            { name: 'AZURE_SEARCH_INDEX', value: indexName }
            { name: 'MCP_AUTH_TENANT_ID', value: tenant().tenantId }
            { name: 'MCP_AUTH_CLIENT_ID', value: apiApp.appId }
            { name: 'MCP_AUTH_APP_ID_URI', value: appIdUri }
            { name: 'MCP_AUTH_REQUIRED_ROLE', value: requiredRole }
            { name: 'MCP_RESOURCE_URL', value: 'https://${appName}.${environment.properties.defaultDomain}/mcp' }
            { name: 'MCP_PUBLISH_RESOURCE_METADATA', value: string(publishResourceMetadata) }
          ]
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
output tenantId string = tenant().tenantId
output apiClientId string = apiApp.appId
output apiAppIdUri string = appIdUri
output apiServicePrincipalId string = apiSp.id
output appRoleId string = appRoleId
output userScope string = '${appIdUri}/${userScope}'
output testClientId string = testClient.appId
output testClientServicePrincipalId string = testClientSp.id
output testClientCallbackPort int = testClientCallbackPort

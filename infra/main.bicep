param location string = 'australiaeast'
param appName string = 'hyperxen-pricing-bot'
param sku string = 'B1'

var appServicePlanName = '${appName}-plan'
var webAppName = '${appName}-${uniqueString(resourceGroup().id)}'

resource appServicePlan 'Microsoft.Web/serverfarms@2022-03-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: sku
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2022-03-01' = {
  name: webAppName
  location: location
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      appCommandLine: 'bash startup.sh'
      appSettings: [
        {
          name: 'AZURE_OPENAI_ENDPOINT'
          value: 'https://hyperxen-foundry-presales1.services.ai.azure.com'
        }
        {
          name: 'AZURE_OPENAI_KEY'
          value: '6UZTVNG4WD2jj1chucsycs64cYBHR0coNkmJuwd6gJFqRFEyp9qgJQQJ99CDACHYHv6XJ3w3AAAAACOGZvNQ'
        }
        {
          name: 'AZURE_OPENAI_DEPLOYMENT'
          value: 'gpt-4o'
        }
        {
          name: 'ANTHROPIC_API_KEY'
          value: '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'
        }
        {
          name: 'AZURE_SUBSCRIPTION_ID'
          value: 'dd5a4d29-50b0-4330-b83a-37094699272c'
        }
        {
          name: 'ENVIRONMENT'
          value: 'production'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
      ]
    }
    httpsOnly: true
  }
  tags: {
    project: 'hyperxen'
    env: 'dev'
  }
}

output webAppUrl string = 'https://${webApp.properties.defaultHostName}'
output webAppName string = webApp.name

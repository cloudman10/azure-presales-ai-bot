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
          value: '<SET_IN_AZURE_APP_SETTINGS>'
        }
        {
          name: 'AZURE_SUBSCRIPTION_ID'
          value: 'dd5a4d29-50b0-4330-b83a-37094699272c'
        }
        {
          name: 'AZURE_TENANT_ID'
          value: 'ceba3126-eb69-4216-9b6f-623fdd3f19de'
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: '5ee843ba-9942-488b-92da-80a79eb266a4'
        }
        {
          name: 'AZURE_CLIENT_SECRET'
          value: '<SET_IN_AZURE_APP_SETTINGS>'
        }
        {
          name: 'AZURE_SEARCH_ENDPOINT'
          value: 'https://hyperxen-search.search.windows.net'
        }
        {
          name: 'AZURE_SEARCH_API_KEY'
          value: '<SET_IN_AZURE_APP_SETTINGS>'
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
          name: 'ORYX_DISABLE_COMPRESS_DEST'
          value: 'true'
        }
        {
          name: 'GUNICORN_CMD_ARGS'
          value: '--worker-class=uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'WEBSITE_HTTPLOGGING_RETENTION_DAYS'
          value: '3'
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

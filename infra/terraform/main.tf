terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.9" # storage_account_id on azurerm_storage_container
    }
  }
  # Remote state lives in backend.tf. Delete that file to keep state locally for a personal trial.
}

provider "azurerm" {
  features {}
  subscription_id     = var.subscription_id
  storage_use_azuread = true
}

data "azurerm_client_config" "current" {}

locals {
  principal_id = coalesce(var.user_object_id, data.azurerm_client_config.current.object_id)
  tags = merge({
    app         = "meetcap"
    owner       = var.owner
    cost_center = var.cost_center
    managed_by  = "terraform"
  }, var.tags)
}

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

# ---------------------------------------------------------------- storage
resource "azurerm_storage_account" "this" {
  name                            = var.storage_account_name
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  access_tier                     = "Hot"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = var.allow_shared_key
  default_to_oauth_authentication = true

  dynamic "network_rules" {
    for_each = length(var.allowed_ip_ranges) > 0 ? [1] : []
    content {
      default_action = "Deny"
      ip_rules       = var.allowed_ip_ranges
      bypass         = ["AzureServices"]
    }
  }

  blob_properties {
    delete_retention_policy {
      days = 14
    }
    container_delete_retention_policy {
      days = 14
    }
  }

  tags = local.tags
}

resource "azurerm_storage_container" "meetings" {
  name                  = var.container_name
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "lifecycle" {
  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "meetings-tiering"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${var.container_name}/"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = var.cool_after_days
        tier_to_cold_after_days_since_modification_greater_than = var.cold_after_days
        delete_after_days_since_modification_greater_than       = var.delete_after_days
      }
    }
  }
}

# ---------------------------------------------------------------- speech
resource "azurerm_cognitive_account" "speech" {
  name                  = var.speech_name
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.speech_location != "" ? var.speech_location : azurerm_resource_group.this.location
  kind                  = "SpeechServices"
  sku_name              = "S0"
  custom_subdomain_name = var.speech_name # required for Entra ID auth and the *.cognitiveservices.azure.com endpoint
  local_auth_enabled    = var.speech_local_auth
  tags                  = local.tags
}

# ---------------------------------------------------------------- azure openai (optional; reuse an existing one if you have it)
resource "azurerm_cognitive_account" "openai" {
  count                 = var.create_openai ? 1 : 0
  name                  = var.openai_name
  resource_group_name   = azurerm_resource_group.this.name
  location              = var.openai_location != "" ? var.openai_location : azurerm_resource_group.this.location
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = var.openai_name
  tags                  = local.tags
}

resource "azurerm_cognitive_deployment" "chat" {
  count                = var.create_openai ? 1 : 0
  name                 = var.openai_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai[0].id
  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }
  sku {
    name     = "GlobalStandard"
    capacity = var.openai_capacity_k_tpm
  }
}

# ---------------------------------------------------------------- RBAC for keyless (az login) access
resource "azurerm_role_assignment" "blob" {
  count                = var.assign_roles ? 1 : 0
  scope                = azurerm_storage_account.this.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.principal_id
}

resource "azurerm_role_assignment" "speech" {
  count                = var.assign_roles ? 1 : 0
  scope                = azurerm_cognitive_account.speech.id
  role_definition_name = "Cognitive Services User"
  principal_id         = local.principal_id
}

resource "azurerm_role_assignment" "openai" {
  count                = var.assign_roles && var.create_openai ? 1 : 0
  scope                = azurerm_cognitive_account.openai[0].id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = local.principal_id
}

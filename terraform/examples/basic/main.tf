# How a platform team would call the module. Not applied anywhere; CI runs
# `terraform validate` and `tflint` on it. The subscription would come from
# ARM_SUBSCRIPTION_ID.
terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.13"
    }
  }
}

provider "azurerm" {
  features {}
}

module "observability_platform" {
  source = "../../modules/aks-observability"

  name                = "obs-platform-dev"
  location            = "westeurope"
  resource_group_name = "rg-obs-platform-dev"
  # Placeholder: the object ID of the platform team's Entra ID group.
  admin_group_object_ids = ["00000000-0000-0000-0000-000000000000"]
  sku_tier               = "Free"
  system_node_count      = 1
  zones                  = ["1"]
  log_daily_quota_gb     = 2

  tags = {
    environment = "dev"
    owner       = "observability-platform"
  }
}

output "cluster_name" {
  value = module.observability_platform.cluster_name
}

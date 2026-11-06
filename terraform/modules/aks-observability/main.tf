# An AKS cluster that sends container logs and control plane logs to a Log
# Analytics workspace. Reference only: validated in CI, never applied.

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name}-logs"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = var.tags
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = var.name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.name
  kubernetes_version  = var.kubernetes_version
  sku_tier            = var.sku_tier

  # Entra ID for authentication and Azure RBAC for authorisation; no static
  # admin kubeconfig.
  local_account_disabled = true
  azure_active_directory_role_based_access_control {
    azure_rbac_enabled     = true
    admin_group_object_ids = var.admin_group_object_ids
  }

  # Lets workloads such as a Thanos sidecar reach Azure storage with a
  # federated identity instead of a stored key.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  identity {
    type = "SystemAssigned"
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.system_node_vm_size
    node_count                   = var.system_node_count
    zones                        = var.zones
    only_critical_addons_enabled = true

    upgrade_settings {
      max_surge = "33%"
    }
  }

  # Node pools are declared explicitly below rather than auto-provisioned.
  node_provisioning_profile {
    mode = "Manual"
  }

  # Cilium enforces NetworkPolicy, which the local kind cluster does not.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
  }

  oms_agent {
    log_analytics_workspace_id      = azurerm_log_analytics_workspace.this.id
    msi_auth_for_monitoring_enabled = true
  }

  tags = var.tags
}

resource "azurerm_kubernetes_cluster_node_pool" "workloads" {
  name                  = "workloads"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = var.workload_node_vm_size
  zones                 = var.zones
  auto_scaling_enabled  = true
  min_count             = var.workload_node_min_count
  max_count             = var.workload_node_max_count

  upgrade_settings {
    max_surge = "33%"
  }

  tags = var.tags
}

resource "azurerm_monitor_diagnostic_setting" "control_plane" {
  name                           = "${var.name}-control-plane"
  target_resource_id             = azurerm_kubernetes_cluster.this.id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.this.id
  log_analytics_destination_type = "Dedicated"

  dynamic "enabled_log" {
    for_each = toset(var.control_plane_log_categories)
    content {
      category = enabled_log.value
    }
  }

  enabled_metric {
    category = "AllMetrics"
  }
}

variable "name" {
  description = "Name of the AKS cluster; also the prefix for the other resources."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,38}[a-z0-9]$", var.name))
    error_message = "Use 3-40 lowercase letters, digits and '-', starting with a letter."
  }
}

variable "location" {
  description = "Azure region, for example westeurope."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create for the cluster and its workspace."
  type        = string
}

variable "admin_group_object_ids" {
  description = "Entra ID groups whose members are cluster admins. Required because local accounts are disabled."
  type        = list(string)

  validation {
    condition     = length(var.admin_group_object_ids) > 0
    error_message = "Give at least one admin group, or nobody can administer the cluster."
  }
}

variable "kubernetes_version" {
  description = "AKS Kubernetes version. null uses the region's current default."
  type        = string
  default     = null
}

variable "sku_tier" {
  description = "AKS control plane tier: Free for test clusters, Standard for the uptime SLA."
  type        = string
  default     = "Standard"

  validation {
    condition     = contains(["Free", "Standard", "Premium"], var.sku_tier)
    error_message = "sku_tier must be Free, Standard or Premium."
  }
}

variable "system_node_vm_size" {
  description = "VM size of the system node pool, which runs only cluster add-ons."
  type        = string
  default     = "Standard_D4ds_v5"
}

variable "system_node_count" {
  description = "Number of system nodes; three spreads them over availability zones."
  type        = number
  default     = 3
}

variable "workload_node_vm_size" {
  description = "VM size of the autoscaled pool for the platform's workloads."
  type        = string
  default     = "Standard_D8ds_v5"
}

variable "workload_node_min_count" {
  description = "Minimum nodes in the workload pool."
  type        = number
  default     = 2
}

variable "workload_node_max_count" {
  description = "Maximum nodes in the workload pool."
  type        = number
  default     = 10

  validation {
    condition     = var.workload_node_max_count >= var.workload_node_min_count
    error_message = "workload_node_max_count must not be lower than workload_node_min_count."
  }
}

variable "zones" {
  description = "Availability zones for both node pools."
  type        = list(string)
  default     = ["1", "2", "3"]
}

variable "log_retention_days" {
  description = "Retention of the Log Analytics workspace."
  type        = number
  default     = 30

  validation {
    condition     = var.log_retention_days >= 30 && var.log_retention_days <= 730
    error_message = "log_retention_days must be between 30 and 730."
  }
}

variable "log_daily_quota_gb" {
  description = "Daily ingestion cap for the workspace in GB, as a cost guard. -1 means no cap."
  type        = number
  default     = 10
}

variable "control_plane_log_categories" {
  description = "AKS resource log categories sent to the workspace."
  type        = list(string)
  default     = ["kube-apiserver", "kube-audit-admin", "kube-controller-manager", "guard"]
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

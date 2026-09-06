variable "replica_sets" {
  description = "ReplicaSets, databases, and account lifecycle state managed by terraformController"

  type = map(object({
    display_name                = string
    created_at                  = string
    members                     = number
    version                     = string
    persistent                  = bool
    storage_class               = string
    storage_size                = string
    storage_mode                = optional(string, "static-local")
    controller_password_version = number

    databases = map(object({
      display_name      = string
      created_at        = string
      owner_disabled    = bool
      owner_disabled_at = optional(string, "")
      rotation_version  = number
      rotated_at        = string
    }))
  }))

  default = {}
}

variable "operation" {
  description = "One-shot lifecycle operation requested by terraformController"
  type = object({
    action      = string
    replica_set = string
    database    = string
    members     = number
    nonce       = string
  })
  default = {
    action      = "none"
    replica_set = ""
    database    = ""
    members     = 0
    nonce       = ""
  }

  validation {
    condition = contains([
      "none",
      "prepare_replica_set_storage",
      "cleanup_replica_set_storage",
      "create_database",
      "delete_database",
      "validate_replica_set_empty",
      "rotate_passwords",
      "disable_owner",
      "verify_database_accounts",
      "verify_database_accounts_owner_disabled",
      "verify_database_users_absent"
    ], var.operation.action)
    error_message = "operation.action is not supported."
  }
}

variable "rotation_days" {
  description = "Password rotation interval in days"
  type        = number
  default     = 30

  validation {
    condition     = var.rotation_days >= 1
    error_message = "rotation_days must be at least 1."
  }
}

variable "vault_address" {
  description = "Vault server address"
  type        = string
}

variable "vault_mount" {
  description = "Vault KV v2 mount"
  type        = string
  default     = "secret"
}

variable "vault_base_path" {
  description = "Base Vault path for terraformController-managed MongoDB resources"
  type        = string
  default     = "mongodb"
}

variable "mongodb_namespace" {
  description = "Kubernetes namespace that contains MongoDB resources"
  type        = string
  default     = "mongodb"
}

variable "ops_manager_config_map" {
  description = "Existing working Ops Manager ConfigMap used as the source for baseUrl and orgId"
  type        = string
  default     = "my-project"
}

variable "ops_manager_credentials_secret" {
  description = "Ops Manager organization credentials Secret used by MongoDB resources"
  type        = string
  default     = "organization-secret"
}

variable "mongodb_auth_database" {
  description = "Authentication database for generated role accounts"
  type        = string
  default     = "admin"
}

variable "kubeconfig_path" {
  description = "Path to kubeconfig"
  type        = string
}

variable "kube_context" {
  description = "Kubeconfig context"
  type        = string
  default     = ""
}

variable "mongo_image" {
  description = "MongoDB image used by Terraform-driven runtime Jobs"
  type        = string
  default     = "mongo:8.0"
}

variable "placeholder_collection" {
  description = "Internal collection used to materialize an otherwise empty MongoDB database"
  type        = string
  default     = "__dbaas_metadata"
}

variable "default_members" {
  description = "Member count used by storage preparation for a new ReplicaSet"
  type        = number
  default     = 3
}

variable "default_storage_class" {
  description = "StorageClass used by storage preparation for a new ReplicaSet"
  type        = string
}

variable "default_storage_size" {
  description = "Storage size used by storage preparation for a new ReplicaSet"
  type        = string
}

variable "storage_base_path" {
  description = "Host/node path used for static local MongoDB volumes"
  type        = string
}

variable "storage_node_name" {
  description = "Kubernetes node and K3D container name that hosts local MongoDB volumes"
  type        = string
}

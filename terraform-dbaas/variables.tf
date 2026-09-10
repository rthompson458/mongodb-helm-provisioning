variable "deployments" {
  description = "ReplicaSets, ShardedClusters, databases, and account lifecycle state managed by privateWorkerReplacement"

  type = map(object({
    display_name                = string
    deployment_type             = string
    created_at                  = string
    members                     = number
    version                     = string
    persistent                  = bool
    storage_class               = string
    storage_size                = string
    storage_mode                = optional(string, "static-local")
    storage_base_path           = optional(string, "")
    storage_node_name           = optional(string, "")
    controller_password_version = number
    shard_count                 = optional(number, 0)
    storage_shard_count         = optional(number, 0)
    members_per_shard           = optional(number, 0)
    mongos_count                = optional(number, 0)
    config_server_count         = optional(number, 0)

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

  validation {
    condition = alltrue([
      for deployment in values(var.deployments) :
      contains(["ReplicaSet", "ShardedCluster"], deployment.deployment_type)
    ])
    error_message = "deployment_type must be ReplicaSet or ShardedCluster."
  }

  validation {
    condition = alltrue([
      for deployment in values(var.deployments) :
      contains(["static-local", "dynamic"], deployment.storage_mode)
    ])
    error_message = "Each deployment storage_mode must be static-local or dynamic."
  }

  validation {
    condition = alltrue([
      for deployment in values(var.deployments) :
      deployment.storage_mode != "static-local" ||
      (deployment.storage_base_path != "" && deployment.storage_node_name != "")
    ])
    error_message = "static-local deployments require storage_base_path and storage_node_name."
  }

  validation {
    condition = alltrue([
      for deployment in values(var.deployments) :
      deployment.deployment_type != "ShardedCluster" ||
      (
        deployment.shard_count >= 1 &&
        deployment.storage_shard_count >= deployment.shard_count &&
        deployment.members_per_shard >= 1 &&
        deployment.mongos_count >= 1 &&
        deployment.config_server_count >= 1
      )
    ])
    error_message = "ShardedCluster deployments require at least one shard, member per shard, mongos, and config server."
  }

  validation {
    condition = alltrue(flatten([
      for deployment in values(var.deployments) : [
        for database in values(deployment.databases) :
        length(trimspace(database.display_name)) > 0 &&
        !contains(["admin", "config", "local"], lower(trimspace(database.display_name)))
      ]
    ]))
    error_message = "Managed database names must be non-empty and cannot be admin, config, or local."
  }
}

variable "operation" {
  description = "One-shot lifecycle operation requested by privateWorkerReplacement"
  type = object({
    action          = string
    deployment      = string
    deployment_type = string
    database        = string
    members         = number
    lock_category   = string
    lock_action     = string
    operation_id    = string
    start_shards    = number
    target_shards   = number
    nonce           = string
  })
  default = {
    action          = "none"
    deployment      = ""
    deployment_type = ""
    database        = ""
    members         = 0
    lock_category   = ""
    lock_action     = ""
    operation_id    = ""
    start_shards    = 0
    target_shards   = 0
    nonce           = ""
  }

  validation {
    condition = contains([
      "none",
      "create_database",
      "delete_database",
      "validate_deployment_empty",
      "rotate_passwords",
      "disable_owner",
      "verify_database_accounts",
      "verify_database_accounts_owner_disabled",
      "verify_database_users_absent",
      "verify_controller_admin",
      "acquire_deployment_lock",
      "release_deployment_lock"
    ], var.operation.action)
    error_message = "operation.action is not supported."
  }

  validation {
    condition = !contains(["create_database", "delete_database"], var.operation.action) || (
      length(trimspace(var.operation.database)) > 0 &&
      !contains(["admin", "config", "local"], lower(trimspace(var.operation.database)))
    )
    error_message = "Database lifecycle operations require a non-empty user database and cannot target admin, config, or local."
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
  description = "Base Vault path for privateWorkerReplacement-managed MongoDB resources"
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

variable "mongodb_management_timeout_seconds" {
  description = "Timeout in seconds for Helm MongoDB database-management hooks"
  type        = number
  default     = 600

  validation {
    condition     = var.mongodb_management_timeout_seconds >= 30
    error_message = "mongodb_management_timeout_seconds must be at least 30."
  }
}

variable "allow_destructive_mongodb_operations" {
  description = "Explicit approval gate for destructive MongoDB management operations"
  type        = bool
  default     = false
}

variable "default_members" {
  description = "Fallback member count used by lifecycle operations"
  type        = number
  default     = 3
}

variable "default_storage_class" {
  description = "Fallback StorageClass for lifecycle operations"
  type        = string
}

variable "default_storage_size" {
  description = "Fallback storage size for lifecycle operations"
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

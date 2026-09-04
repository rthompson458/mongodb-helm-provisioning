variable "replica_sets" {
  description = "Replica sets, databases, and account lifecycle state managed by terraformController"

  type = map(object({
    display_name                = string
    created_at                  = string
    members                     = number
    version                     = string
    persistent                  = bool
    storage_class               = string
    storage_size                = string
    controller_password_version = number

    databases = map(object({
      display_name     = string
      created_at       = string
      owner_disabled   = bool
      rotation_version = number
      rotated_at       = string
    }))
  }))

  default = {}
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
  description = "Ops Manager project ConfigMap used by MongoDB resources"
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

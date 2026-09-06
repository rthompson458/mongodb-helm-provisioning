locals {
  account_types = {
    owner = {
      suffix = "owner"
      role   = "dbOwner"
    }

    readwrite = {
      suffix = "readWrite"
      role   = "readWrite"
    }

    read = {
      suffix = "read"
      role   = "read"
    }
  }

  databases = {
    for database in flatten([
      for replica_set_key, replica_set in var.replica_sets : [
        for database_key, database in replica_set.databases : {
          key               = "${replica_set_key}/${database_key}"
          replica_set_key   = replica_set_key
          replica_set_name  = replica_set.display_name
          database_key      = database_key
          database_name     = database.display_name
          created_at        = database.created_at
          owner_disabled    = database.owner_disabled
          owner_disabled_at = database.owner_disabled_at
          rotation_version  = database.rotation_version
          rotated_at        = database.rotated_at
        }
      ]
    ]) : database.key => database
  }

  accounts = {
    for account in flatten([
      for database_key, database in local.databases : [
        for account_key, account_type in local.account_types : {
          key              = "${database_key}/${account_key}"
          replica_set_key  = database.replica_set_key
          replica_set_name = database.replica_set_name
          database_key     = database.database_key
          database_name    = database.database_name
          account_key      = account_key
          account_type     = account_type.suffix
          username         = "${database.database_name}_${account_type.suffix}"
          role             = account_type.role
          enabled          = account_key != "owner" || !database.owner_disabled
          rotation_version = database.rotation_version
          rotated_at       = database.rotated_at
          resource_name    = "tc-${substr(database.replica_set_key, 0, 8)}-${substr(database.database_key, 0, 10)}-${account_key}-${substr(md5("${database_key}/${account_key}"), 0, 6)}"
        }
      ]
    ]) : account.key => account
  }

  enabled_accounts = {
    for key, account in local.accounts : key => account
    if account.enabled
  }
}

# Source the existing working Ops Manager connection information.
data "kubernetes_config_map_v1" "ops_manager_source" {
  metadata {
    name      = var.ops_manager_config_map
    namespace = var.mongodb_namespace
  }
}

# One MongoDB resource per Ops Manager project. Omitting projectName lets the
# Operator create/use a distinct Ops Manager project for each ReplicaSet.
resource "kubernetes_config_map_v1" "controller_ops_manager_projects" {
  metadata {
    name      = "tc-ops-manager-projects"
    namespace = var.mongodb_namespace

    labels = {
      "app.kubernetes.io/managed-by" = "terraformController"
    }
  }

  data = {
    baseUrl = data.kubernetes_config_map_v1.ops_manager_source.data["baseUrl"]
    orgId   = data.kubernetes_config_map_v1.ops_manager_source.data["orgId"]
  }
}

resource "kubernetes_manifest" "replica_set" {
  for_each = var.replica_sets

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDB"

    metadata = {
      name      = each.key
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "terraformController"
        "dbaas.replica-set"            = each.key
      }
    }

    spec = merge(
      {
        type       = "ReplicaSet"
        members    = each.value.members
        version    = each.value.version
        persistent = each.value.persistent

        security = {
          authentication = {
            enabled            = true
            modes              = ["SCRAM"]
            ignoreUnknownUsers = false
          }
        }

        opsManager = {
          configMapRef = {
            name = kubernetes_config_map_v1.controller_ops_manager_projects.metadata[0].name
          }
        }

        credentials = var.ops_manager_credentials_secret
      },
      each.value.persistent ? {
        podSpec = {
          persistence = {
            single = {
              storage      = each.value.storage_size
              storageClass = each.value.storage_class
              labelSelector = {
                matchLabels = {
                  "dbaas.replica-set" = each.key
                }
              }
            }
          }
        }
      } : {}
    )
  }
}

# Human-facing Vault hierarchy:
#   mongodb/RS1/_metadata
#   mongodb/RS1/HouseInfo/_metadata
#   mongodb/RS1/HouseInfo/HouseInfo_owner
#   mongodb/RS1/HouseInfo/HouseInfo_readWrite
#   mongodb/RS1/HouseInfo/HouseInfo_read
resource "vault_kv_secret_v2" "replica_set_metadata" {
  for_each = var.replica_sets

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.display_name}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    display_name                = each.value.display_name
    resource_name               = each.key
    created_at                  = each.value.created_at
    members                     = tostring(each.value.members)
    version                     = each.value.version
    persistent                  = tostring(each.value.persistent)
    storage_class               = each.value.storage_class
    storage_size                = each.value.storage_size
    controller_password_version = tostring(each.value.controller_password_version)
    managed_by                  = "terraformController"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type       = "replica-set-metadata"
      managed_by = "terraformController"
    }
  }
}

resource "vault_kv_secret_v2" "database_metadata" {
  for_each = local.databases

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.replica_set_name}/${each.value.database_name}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    replica_set       = each.value.replica_set_name
    display_name      = each.value.database_name
    created_at        = each.value.created_at
    owner_disabled    = tostring(each.value.owner_disabled)
    owner_disabled_at = each.value.owner_disabled_at
    rotation_version  = tostring(each.value.rotation_version)
    rotated_at        = each.value.rotated_at
    managed_by        = "terraformController"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type        = "database-metadata"
      managed_by  = "terraformController"
      replica_set = each.value.replica_set_name
    }
  }
}

# Each managed ReplicaSet gets one hidden controller administrator. It is used
# only by Terraform-driven runtime Jobs for DB materialization and validation.
ephemeral "random_password" "controller_admin" {
  for_each = var.replica_sets

  length           = 32
  special          = true
  override_special = "!#$%&*+-=?@_"
}

resource "vault_kv_secret_v2" "controller_admin" {
  for_each = var.replica_sets

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.display_name}/_internal/controller-admin"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    replica_set = each.value.display_name
    username    = "tc_${each.key}_admin"
    password    = ephemeral.random_password.controller_admin[each.key].result
  })

  data_json_wo_version = each.value.controller_password_version
}

resource "kubernetes_secret_v1" "controller_admin_password" {
  for_each = var.replica_sets

  metadata {
    name      = "tc-${each.key}-admin-password"
    namespace = var.mongodb_namespace

    labels = {
      "app.kubernetes.io/managed-by" = "terraformController"
      "dbaas.replica-set"            = each.key
      "dbaas.account-type"           = "controller-admin"
    }
  }

  data_wo = {
    password = ephemeral.random_password.controller_admin[each.key].result
  }

  data_wo_revision = each.value.controller_password_version
  type             = "Opaque"
}

resource "kubernetes_manifest" "controller_admin" {
  for_each = var.replica_sets

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDBUser"

    metadata = {
      name      = "tc-${each.key}-admin"
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "terraformController"
        "dbaas.replica-set"            = each.key
        "dbaas.account-type"           = "controller-admin"
      }
    }

    spec = {
      username = "tc_${each.key}_admin"
      db       = var.mongodb_auth_database

      mongodbResourceRef = {
        name = each.key
      }

      passwordSecretKeyRef = {
        name = kubernetes_secret_v1.controller_admin_password[each.key].metadata[0].name
        key  = "password"
      }

      connectionStringSecretName = "tc-${each.key}-admin-connection"

      roles = [
        {
          db   = "admin"
          name = "root"
        }
      ]
    }
  }

  depends_on = [
    kubernetes_manifest.replica_set,
    kubernetes_secret_v1.controller_admin_password
  ]
}

# Passwords are ephemeral Terraform values. They are written to Vault and
# Kubernetes through write-only fields, so plaintext passwords are not stored
# in Terraform state.
ephemeral "random_password" "database_account" {
  for_each = local.accounts

  length           = 24
  special          = true
  override_special = "!#$%&*+-=?@_"
}

resource "vault_kv_secret_v2" "database_account" {
  for_each = local.accounts

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.replica_set_name}/${each.value.database_name}/${each.value.username}"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    replica_set  = each.value.replica_set_name
    database     = each.value.database_name
    account_type = each.value.account_type
    username     = each.value.username
    password     = ephemeral.random_password.database_account[each.key].result
    rotated_at   = each.value.rotated_at
  })

  data_json_wo_version = each.value.rotation_version

  custom_metadata {
    max_versions = 5

    data = {
      managed_by   = "terraformController"
      replica_set  = each.value.replica_set_name
      database     = each.value.database_name
      account_type = each.value.account_type
    }
  }
}

resource "kubernetes_secret_v1" "database_account_password" {
  for_each = local.accounts

  metadata {
    name      = "${each.value.resource_name}-password"
    namespace = var.mongodb_namespace

    labels = {
      "app.kubernetes.io/managed-by" = "terraformController"
      "dbaas.replica-set"            = each.value.replica_set_key
      "dbaas.database"               = each.value.database_key
      "dbaas.account-type"           = each.value.account_key
    }
  }

  data_wo = {
    password = ephemeral.random_password.database_account[each.key].result
  }

  data_wo_revision = each.value.rotation_version
  type             = "Opaque"
}

# Disabled Owner = no Owner MongoDBUser. The Vault credential and password
# Secret remain, so the password keeps rotating while login stays disabled.
resource "kubernetes_manifest" "database_account" {
  for_each = local.enabled_accounts

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDBUser"

    metadata = {
      name      = each.value.resource_name
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "terraformController"
        "dbaas.replica-set"            = each.value.replica_set_key
        "dbaas.database"               = each.value.database_key
        "dbaas.account-type"           = each.value.account_key
      }
    }

    spec = {
      username                 = each.value.username
      db                       = var.mongodb_auth_database
      connectionStringDatabase = each.value.database_name

      mongodbResourceRef = {
        name = each.value.replica_set_key
      }

      passwordSecretKeyRef = {
        name = kubernetes_secret_v1.database_account_password[each.key].metadata[0].name
        key  = "password"
      }

      connectionStringSecretName = "${each.value.resource_name}-connection"

      roles = [
        {
          db   = each.value.database_name
          name = each.value.role
        }
      ]
    }
  }

  depends_on = [
    kubernetes_manifest.replica_set,
    kubernetes_secret_v1.database_account_password
  ]
}

# Terraform owns imperative lifecycle actions that cannot be represented as a
# long-lived MongoDB object: creating/dropping a logical DB, verifying that an
# RS is empty, and preparing/cleaning K3D static local storage. Python only
# supplies the operation and reports its result.
resource "terraform_data" "lifecycle_operation" {
  count = var.operation.action == "none" ? 0 : 1

  input            = var.operation
  triggers_replace = [var.operation.nonce]

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION                 = var.operation.action
      TC_REPLICA_SET            = var.operation.replica_set
      TC_DATABASE               = var.operation.database
      TC_MEMBERS                = tostring(var.operation.members > 0 ? var.operation.members : var.default_members)
      TC_NAMESPACE              = var.mongodb_namespace
      TC_KUBECONFIG             = pathexpand(var.kubeconfig_path)
      TC_KUBE_CONTEXT           = var.kube_context
      TC_MONGO_IMAGE            = var.mongo_image
      TC_PLACEHOLDER_COLLECTION = var.placeholder_collection
      TC_STORAGE_BASE_PATH      = var.storage_base_path
      TC_STORAGE_NODE_NAME      = var.storage_node_name
      TC_STORAGE_CLASS          = var.default_storage_class
      TC_STORAGE_SIZE           = var.default_storage_size
    }
  }
}

output "managed_replica_sets" {
  value = {
    for replica_set_key, replica_set in var.replica_sets : replica_set_key => {
      display_name = replica_set.display_name
      databases    = keys(replica_set.databases)
    }
  }
}

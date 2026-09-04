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
          key              = "${replica_set_key}/${database_key}"
          replica_set_key  = replica_set_key
          database_key     = database_key
          database_name    = database.display_name
          created_at       = database.created_at
          owner_disabled   = database.owner_disabled
          rotation_version = database.rotation_version
          rotated_at       = database.rotated_at
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
# The source ConfigMap currently contains baseUrl, orgId, and a fixed projectName.
data "kubernetes_config_map_v1" "ops_manager_source" {
  metadata {
    name      = var.ops_manager_config_map
    namespace = var.mongodb_namespace
  }
}

# MongoDB supports reusing one ConfigMap for multiple deployments when projectName
# is omitted. In that mode, the Operator creates/uses a distinct Ops Manager
# project whose name matches each MongoDB resource name.
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

resource "vault_kv_secret_v2" "replica_set_metadata" {
  for_each = var.replica_sets

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/replica-sets/${each.key}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    display_name   = each.value.display_name
    resource_name  = each.key
    created_at     = each.value.created_at
    members        = tostring(each.value.members)
    version        = each.value.version
    persistent     = tostring(each.value.persistent)
    storage_class  = each.value.storage_class
    storage_size   = each.value.storage_size
    managed_by     = "terraformController"
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
  name                = "${var.vault_base_path}/replica-sets/${each.value.replica_set_key}/databases/${each.value.database_key}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    replica_set      = each.value.replica_set_key
    display_name     = each.value.database_name
    created_at       = each.value.created_at
    owner_disabled   = tostring(each.value.owner_disabled)
    rotation_version = tostring(each.value.rotation_version)
    rotated_at       = each.value.rotated_at
    managed_by       = "terraformController"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type        = "database-metadata"
      managed_by  = "terraformController"
      replica_set = each.value.replica_set_key
    }
  }
}

# Each managed replica set gets one internal controller administrator.
# This account is not exposed by ListDatabase/ListDatabases. It is used only
# for controller runtime checks such as verifying that a replica set is empty.
ephemeral "random_password" "controller_admin" {
  for_each = var.replica_sets

  length           = 32
  special          = true
  override_special = "!#$%&*+-=?@_"
}

resource "vault_kv_secret_v2" "controller_admin" {
  for_each = var.replica_sets

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/replica-sets/${each.key}/_internal/controller-admin"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    replica_set = each.key
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

# Database role-account passwords. Passwords are ephemeral Terraform values.
# They are written to Vault and Kubernetes through write-only provider fields,
# so the plaintext passwords are not stored in Terraform state.
ephemeral "random_password" "database_account" {
  for_each = local.accounts

  length           = 24
  special          = true
  override_special = "!#$%&*+-=?@_"
}

resource "vault_kv_secret_v2" "database_account" {
  for_each = local.accounts

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/replica-sets/${each.value.replica_set_key}/databases/${each.value.database_key}/accounts/${each.value.account_key}"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    replica_set  = each.value.replica_set_key
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
      replica_set  = each.value.replica_set_key
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

# A disabled owner is represented by absence of its MongoDBUser resource.
# Its Vault credential and Kubernetes password Secret remain so RotatePassword
# can still rotate it while disabled and a future EnableOwner can reuse it.
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

output "managed_replica_sets" {
  value = {
    for replica_set_key, replica_set in var.replica_sets : replica_set_key => {
      display_name = replica_set.display_name
      databases    = keys(replica_set.databases)
    }
  }
}

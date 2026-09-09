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

  # Raw lifecycle state comes from Vault through var.deployments.
  # RotatePasswords and DisableOwner are expressed as Terraform operations.
  # Terraform, not Python, computes the resulting password revision and owner state.
  database_inputs = {
    for database in flatten([
      for deployment_key, replica_set in var.deployments : [
        for database_key, database in replica_set.databases : {
          key               = "${deployment_key}/${database_key}"
          deployment_key    = deployment_key
          deployment_name   = replica_set.display_name
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

  databases = {
    for key, database in local.database_inputs : key => merge(database, {
      rotation_version = (
        var.operation.action == "rotate_passwords" &&
        var.operation.deployment == database.deployment_key &&
        lower(var.operation.database) == database.database_key
      ) ? database.rotation_version + 1 : database.rotation_version

      rotated_at = (
        var.operation.action == "rotate_passwords" &&
        var.operation.deployment == database.deployment_key &&
        lower(var.operation.database) == database.database_key
      ) ? plantimestamp() : database.rotated_at

      owner_disabled = (
        database.owner_disabled ||
        (
          var.operation.action == "disable_owner" &&
          var.operation.deployment == database.deployment_key &&
          lower(var.operation.database) == database.database_key
        ) ||
        (
          var.operation.action == "rotate_passwords" &&
          var.operation.deployment == database.deployment_key &&
          lower(var.operation.database) == database.database_key &&
          timecmp(
            plantimestamp(),
            timeadd(database.created_at, format("%dh", var.rotation_days * 24))
          ) >= 0
        )
      )

      owner_disabled_at = (
        !database.owner_disabled &&
        (
          (
            var.operation.action == "disable_owner" &&
            var.operation.deployment == database.deployment_key &&
            lower(var.operation.database) == database.database_key
          ) ||
          (
            var.operation.action == "rotate_passwords" &&
            var.operation.deployment == database.deployment_key &&
            lower(var.operation.database) == database.database_key &&
            timecmp(
              plantimestamp(),
              timeadd(database.created_at, format("%dh", var.rotation_days * 24))
            ) >= 0
          )
        )
      ) ? plantimestamp() : database.owner_disabled_at
    })
  }

  accounts = {
    for account in flatten([
      for database_key, database in local.databases : [
        for account_key, account_type in local.account_types : {
          key              = "${database_key}/${account_key}"
          deployment_key   = database.deployment_key
          deployment_name  = database.deployment_name
          database_key     = database.database_key
          database_name    = database.database_name
          account_key      = account_key
          account_type     = account_type.suffix
          username         = "${database.database_name}_${account_type.suffix}"
          role             = account_type.role
          enabled          = account_key != "owner" || !database.owner_disabled
          rotation_version = database.rotation_version
          rotated_at       = database.rotated_at
          # MongoDB database names may contain underscores, but Kubernetes
          # object names may not. Keep the MongoDB/Vault display name unchanged
          # while converting only the Kubernetes resource-name segment.
          resource_name = "tc-${substr(database.deployment_key, 0, 8)}-${substr(replace(database.database_key, "_", "-"), 0, 10)}-${account_key}-${substr(md5("${database_key}/${account_key}"), 0, 6)}"
        }
      ]
    ]) : account.key => account
  }

  enabled_accounts = {
    for key, account in local.accounts : key => account
    if account.enabled
  }

  replica_sets = {
    for key, deployment in var.deployments : key => deployment
    if deployment.deployment_type == "ReplicaSet"
  }

  sharded_clusters = {
    for key, deployment in var.deployments : key => deployment
    if deployment.deployment_type == "ShardedCluster"
  }

  static_replica_sets = {
    for key, replica_set in local.replica_sets : key => replica_set
    if replica_set.persistent && replica_set.storage_mode == "static-local"
  }

  static_shard_volumes = merge(concat([{}], [
    for key, cluster in local.sharded_clusters : {
      for ordinal in range(cluster.storage_shard_count * cluster.members_per_shard) :
      "${key}/shard/${ordinal}" => {
        deployment        = key
        component         = "shard"
        ordinal           = ordinal
        pv_name           = "${key}-shard-${ordinal}"
        storage_base_path = cluster.storage_base_path
        storage_node_name = cluster.storage_node_name
        storage_class     = cluster.storage_class
        storage_size      = cluster.storage_size
      }
    } if cluster.persistent && cluster.storage_mode == "static-local"
  ])...)

  static_config_volumes = merge(concat([{}], [
    for key, cluster in local.sharded_clusters : {
      for ordinal in range(cluster.config_server_count) :
      "${key}/config/${ordinal}" => {
        deployment        = key
        component         = "config"
        ordinal           = ordinal
        pv_name           = "${key}-config-${ordinal}"
        storage_base_path = cluster.storage_base_path
        storage_node_name = cluster.storage_node_name
        storage_class     = cluster.storage_class
        storage_size      = cluster.storage_size
      }
    } if cluster.persistent && cluster.storage_mode == "static-local"
  ])...)
}

# Source the existing working Ops Manager connection information.
data "kubernetes_config_map_v1" "ops_manager_source" {
  metadata {
    name      = var.ops_manager_config_map
    namespace = var.mongodb_namespace
  }
}

# One MongoDB resource per Ops Manager project. Omitting projectName lets the
# Operator create/use a distinct Ops Manager project for each managed deployment.
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

# Static local storage is a Terraform-managed lifecycle resource.
# Its state survives controller process failures. The create provisioner prepares
# local directories/PVs before the MongoDB resource is created. The destroy
# provisioner runs only after the MongoDB resource has been destroyed because
# replica_set explicitly depends on this resource.
resource "terraform_data" "replica_set_storage" {
  for_each = local.static_replica_sets

  input = {
    replica_set       = each.key
    members           = each.value.members
    namespace         = var.mongodb_namespace
    kubeconfig        = pathexpand(var.kubeconfig_path)
    kube_context      = var.kube_context
    storage_base_path = each.value.storage_base_path
    storage_node_name = each.value.storage_node_name
    storage_class     = each.value.storage_class
    storage_size      = each.value.storage_size
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "prepare_replica_set_storage"
      TC_REPLICA_SET       = self.input.replica_set
      TC_MEMBERS           = tostring(self.input.members)
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "cleanup_replica_set_storage"
      TC_REPLICA_SET       = self.input.replica_set
      TC_MEMBERS           = tostring(self.input.members)
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }
}

resource "terraform_data" "sharded_cluster_shard_storage" {
  for_each = local.static_shard_volumes

  input = merge(each.value, {
    namespace    = var.mongodb_namespace
    kubeconfig   = pathexpand(var.kubeconfig_path)
    kube_context = var.kube_context
  })

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "prepare_sharded_cluster_volume"
      TC_DEPLOYMENT        = self.input.deployment
      TC_COMPONENT         = self.input.component
      TC_PV_NAME           = self.input.pv_name
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "cleanup_sharded_cluster_volume"
      TC_DEPLOYMENT        = self.input.deployment
      TC_COMPONENT         = self.input.component
      TC_PV_NAME           = self.input.pv_name
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }
}

resource "terraform_data" "sharded_cluster_config_storage" {
  for_each = local.static_config_volumes

  input = merge(each.value, {
    namespace    = var.mongodb_namespace
    kubeconfig   = pathexpand(var.kubeconfig_path)
    kube_context = var.kube_context
  })

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "prepare_sharded_cluster_volume"
      TC_DEPLOYMENT        = self.input.deployment
      TC_COMPONENT         = self.input.component
      TC_PV_NAME           = self.input.pv_name
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION            = "cleanup_sharded_cluster_volume"
      TC_DEPLOYMENT        = self.input.deployment
      TC_COMPONENT         = self.input.component
      TC_PV_NAME           = self.input.pv_name
      TC_NAMESPACE         = self.input.namespace
      TC_KUBECONFIG        = self.input.kubeconfig
      TC_KUBE_CONTEXT      = self.input.kube_context
      TC_STORAGE_BASE_PATH = self.input.storage_base_path
      TC_STORAGE_NODE_NAME = self.input.storage_node_name
      TC_STORAGE_CLASS     = self.input.storage_class
      TC_STORAGE_SIZE      = self.input.storage_size
    }
  }
}

resource "kubernetes_manifest" "replica_set" {
  for_each = local.replica_sets

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
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.replica-set" = each.key
                  }
                }
              } : {}
            )
          }
        }
      } : {}
    )
  }

  depends_on = [terraform_data.replica_set_storage]
}

# ShardedCluster topology is always controlled through Terraform desired state.
# In particular, DeleteShard lowers spec.shardCount here; Python never issues a
# direct MongoDB removeShard or directly deletes shard pods. The MongoDB
# Kubernetes Operator/Ops Manager reconciles the supported scale-down, and only
# after the removed shard StatefulSets are gone does Terraform clean old storage.
resource "kubernetes_manifest" "sharded_cluster" {
  for_each = local.sharded_clusters

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDB"

    metadata = {
      name      = each.key
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "terraformController"
        "dbaas.deployment"             = each.key
        "dbaas.deployment-type"        = "ShardedCluster"
      }
    }

    spec = merge(
      {
        type                 = "ShardedCluster"
        shardCount           = each.value.shard_count
        mongodsPerShardCount = each.value.members_per_shard
        mongosCount          = each.value.mongos_count
        configServerCount    = each.value.config_server_count
        version              = each.value.version
        persistent           = each.value.persistent

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
        shardPodSpec = {
          persistence = {
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.sharded-cluster" = each.key
                    "dbaas.component"       = "shard"
                  }
                }
              } : {}
            )
          }
        }

        configSrvPodSpec = {
          persistence = {
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.sharded-cluster" = each.key
                    "dbaas.component"       = "config"
                  }
                }
              } : {}
            )
          }
        }
      } : {}
    )
  }

  depends_on = [
    terraform_data.sharded_cluster_shard_storage,
    terraform_data.sharded_cluster_config_storage
  ]
}

# Human-facing Vault hierarchy:
#   mongodb/RS1/_metadata
#   mongodb/RS1/HouseInfo/_metadata
#   mongodb/RS1/HouseInfo/HouseInfo_owner
#   mongodb/RS1/HouseInfo/HouseInfo_readWrite
#   mongodb/RS1/HouseInfo/HouseInfo_read
resource "vault_kv_secret_v2" "replica_set_metadata" {
  for_each = var.deployments

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.display_name}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    display_name                = each.value.display_name
    deployment_type             = each.value.deployment_type
    resource_name               = each.key
    created_at                  = each.value.created_at
    members                     = tostring(each.value.members)
    version                     = each.value.version
    persistent                  = tostring(each.value.persistent)
    storage_class               = each.value.storage_class
    storage_size                = each.value.storage_size
    storage_mode                = each.value.storage_mode
    storage_base_path           = each.value.storage_base_path
    storage_node_name           = each.value.storage_node_name
    controller_password_version = tostring(each.value.controller_password_version)
    shard_count                 = tostring(each.value.shard_count)
    storage_shard_count         = tostring(each.value.storage_shard_count)
    members_per_shard           = tostring(each.value.members_per_shard)
    mongos_count                = tostring(each.value.mongos_count)
    config_server_count         = tostring(each.value.config_server_count)
    managed_by                  = "terraformController"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type       = "deployment-metadata"
      managed_by = "terraformController"
    }
  }

  # Do not advertise a deployment in Vault inventory until Terraform has
  # successfully created its MongoDB custom resource.
  depends_on = [
    kubernetes_manifest.replica_set,
    kubernetes_manifest.sharded_cluster
  ]
}

resource "vault_kv_secret_v2" "database_metadata" {
  for_each = local.databases

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.deployment_name}/${each.value.database_name}/_metadata"
  delete_all_versions = true

  data_json = jsonencode({
    deployment        = each.value.deployment_name
    display_name      = each.value.database_name
    deployment_type   = var.deployments[each.value.deployment_key].deployment_type
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
      type       = "database-metadata"
      managed_by = "terraformController"
      deployment = each.value.deployment_name
    }
  }
}

# Each managed deployment gets one hidden controller administrator. It is used
# only by Terraform-driven runtime Jobs for DB materialization and validation.
ephemeral "random_password" "controller_admin" {
  for_each = var.deployments

  length           = 32
  special          = true
  override_special = "!#$%&*+-=?@_"
}

resource "vault_kv_secret_v2" "controller_admin" {
  for_each = var.deployments

  mount               = var.vault_mount
  name                = "${var.vault_base_path}/${each.value.display_name}/_internal/controller-admin"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    deployment      = each.value.display_name
    deployment_type = each.value.deployment_type
    username        = "tc_${each.key}_admin"
    password        = ephemeral.random_password.controller_admin[each.key].result
  })

  data_json_wo_version = each.value.controller_password_version
}

resource "kubernetes_secret_v1" "controller_admin_password" {
  for_each = var.deployments

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
  for_each = var.deployments

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
    kubernetes_manifest.sharded_cluster,
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
  name                = "${var.vault_base_path}/${each.value.deployment_name}/${each.value.database_name}/${each.value.username}"
  disable_read        = true
  delete_all_versions = true

  data_json_wo = jsonencode({
    deployment      = each.value.deployment_name
    database        = each.value.database_name
    deployment_type = var.deployments[each.value.deployment_key].deployment_type
    account_type    = each.value.account_type
    username        = each.value.username
    password        = ephemeral.random_password.database_account[each.key].result
    rotated_at      = each.value.rotated_at
  })

  data_json_wo_version = each.value.rotation_version

  # Commit lifecycle metadata first. If a later credential write fails, the
  # next RotatePasswords attempt advances the revision again and forces both
  # Vault and Kubernetes to converge on one fresh password.
  depends_on = [vault_kv_secret_v2.database_metadata]

  custom_metadata {
    max_versions = 5

    data = {
      managed_by   = "terraformController"
      deployment   = each.value.deployment_name
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
      "dbaas.replica-set"            = each.value.deployment_key
      "dbaas.database"               = each.value.database_key
      "dbaas.account-type"           = each.value.account_key
    }
  }

  data_wo = {
    password = ephemeral.random_password.database_account[each.key].result
  }

  data_wo_revision = each.value.rotation_version
  type             = "Opaque"

  # See database_account above. Metadata is the committed rotation intent and
  # must advance before either write-only password sink is changed.
  depends_on = [vault_kv_secret_v2.database_metadata]
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
        "dbaas.replica-set"            = each.value.deployment_key
        "dbaas.database"               = each.value.database_key
        "dbaas.account-type"           = each.value.account_key
      }
    }

    spec = {
      username                 = each.value.username
      db                       = var.mongodb_auth_database
      connectionStringDatabase = each.value.database_name

      mongodbResourceRef = {
        name = each.value.deployment_key
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
    kubernetes_manifest.sharded_cluster,
    kubernetes_secret_v1.database_account_password
  ]
}

# Terraform owns imperative lifecycle actions that cannot be represented as a
# long-lived MongoDB object: creating/dropping a logical DB, verifying that an
# deployment is empty, and preparing/cleaning K3D static local storage. Python only
# supplies the operation and reports its result.
resource "terraform_data" "lifecycle_operation" {
  count = contains([
    "create_database",
    "delete_database",
    "validate_deployment_empty",
    "verify_database_accounts",
    "verify_database_accounts_owner_disabled",
    "verify_database_users_absent",
    "verify_controller_admin",
    "acquire_deployment_lock",
    "release_deployment_lock"
  ], var.operation.action) ? 1 : 0

  input            = var.operation
  triggers_replace = [var.operation.nonce]

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/lifecycle.sh"

    environment = {
      TC_ACTION                 = var.operation.action
      TC_DEPLOYMENT             = var.operation.deployment
      TC_DEPLOYMENT_TYPE        = var.operation.deployment_type
      TC_DATABASE               = var.operation.database
      TC_MEMBERS                = tostring(var.operation.members > 0 ? var.operation.members : var.default_members)
      TC_LOCK_CATEGORY          = var.operation.lock_category
      TC_LOCK_ACTION            = var.operation.lock_action
      TC_OPERATION_ID           = var.operation.operation_id
      TC_START_SHARDS           = tostring(var.operation.start_shards)
      TC_TARGET_SHARDS          = tostring(var.operation.target_shards)
      TC_NAMESPACE              = var.mongodb_namespace
      TC_KUBECONFIG             = pathexpand(var.kubeconfig_path)
      TC_KUBE_CONTEXT           = var.kube_context
      TC_MONGO_IMAGE            = var.mongo_image
      TC_AUTH_DATABASE          = var.mongodb_auth_database
      TC_PLACEHOLDER_COLLECTION = var.placeholder_collection
      TC_STORAGE_BASE_PATH      = var.storage_base_path
      TC_STORAGE_NODE_NAME      = var.storage_node_name
      TC_STORAGE_CLASS          = var.default_storage_class
      TC_STORAGE_SIZE           = var.default_storage_size
    }
  }
}

output "managed_deployments" {
  value = {
    for deployment_key, deployment in var.deployments : deployment_key => {
      display_name    = deployment.display_name
      deployment_type = deployment.deployment_type
      databases       = keys(deployment.databases)
    }
  }
}

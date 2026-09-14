# Derived desired state and normalized collections used by the root module.
#
# Terraform loads every .tf file in this directory as one module. This file
# contains only local-value transformations; moving these blocks out of main.tf
# does not change resource addresses or state ownership.

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

      # EnableOwner restores the Owner account without rotating its password.
      owner_disabled = (
        var.operation.action == "enable_owner" &&
        var.operation.deployment == database.deployment_key &&
        lower(var.operation.database) == database.database_key
        ) ? false : (
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
        var.operation.action == "enable_owner" &&
        var.operation.deployment == database.deployment_key &&
        lower(var.operation.database) == database.database_key
        ) ? "" : (
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

  # Database management through the integrated Helm chart. The controller's
  # existing var.deployments inventory remains the single source of truth.
  #
  # During AddDatabase, the requested database is temporarily added here before
  # Python commits it to Vault-backed inventory. That lets the Helm provisioner
  # materialize the database first. The later inventory apply then makes it
  # normal long-lived desired state.
  mongodb_databases = {
    for deployment_key, deployment in var.deployments : deployment_key => values(merge(
      {
        for database_key, database in deployment.databases :
        database_key => {
          name = database.display_name
        }
      },
      (
        var.operation.action == "create_database" &&
        var.operation.deployment == deployment_key
        ) ? {
        lower(var.operation.database) = {
          name = var.operation.database
        }
      } : {}
    ))
  }

  # Retain the mongodbDatabaseOperations interface from the parallel Helm work,
  # but this DBaaS effort supports only database deletion. Collection management
  # and runtime-audit operations are outside the current scope.
  mongodb_database_operations = {
    for deployment_key, deployment in var.deployments : deployment_key => (
      var.operation.action == "delete_database" &&
      var.operation.deployment == deployment_key
      ) ? [
      {
        id       = var.operation.operation_id != "" ? var.operation.operation_id : var.operation.nonce
        action   = "deleteDatabase"
        database = var.operation.database
      }
    ] : []
  }

  # A management release exists only while a deployment has managed databases
  # or while a create/delete database operation is actively being processed.
  mongodb_management_deployments = {
    for deployment_key, deployment in var.deployments : deployment_key => deployment
    if(
      length(local.mongodb_databases[deployment_key]) > 0 ||
      (
        contains(["create_database", "delete_database"], var.operation.action) &&
        var.operation.deployment == deployment_key
      )
    )
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
        shard_index       = floor(ordinal / cluster.members_per_shard)
        member_index      = ordinal % cluster.members_per_shard
        pv_name           = "${key}-shard-${ordinal}"
        pvc_name          = "data-${key}-${floor(ordinal / cluster.members_per_shard)}-${ordinal % cluster.members_per_shard}"
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
        member_index      = ordinal
        pv_name           = "${key}-config-${ordinal}"
        pvc_name          = "data-${key}-config-${ordinal}"
        storage_base_path = cluster.storage_base_path
        storage_node_name = cluster.storage_node_name
        storage_class     = cluster.storage_class
        storage_size      = cluster.storage_size
      }
    } if cluster.persistent && cluster.storage_mode == "static-local"
  ])...)
}


# Durable non-secret lifecycle metadata written to Vault.
#
# Vault inventory is the controller's recoverable desired-state record. These
# resources intentionally retain their existing Terraform addresses.

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
    managed_by                  = "privateWorkerReplacement"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type       = "deployment-metadata"
      managed_by = "privateWorkerReplacement"
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
    managed_by        = "privateWorkerReplacement"
  })

  custom_metadata {
    max_versions = 5

    data = {
      type       = "database-metadata"
      managed_by = "privateWorkerReplacement"
      deployment = each.value.deployment_name
    }
  }
}


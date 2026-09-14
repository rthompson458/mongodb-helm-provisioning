# Helm-based logical database materialization and deletion.
#
# This file contains the one management release per deployment. It does not own
# long-lived account resources; those remain in database-accounts.tf.

# Logical database materialization and deletion use the integrated Helm chart.
# This preserves the useful mongodb_management / mongodbDatabases /
# mongodbDatabaseOperations structure from the parallel Terraform work while
# keeping privateWorkerReplacement's Deployment -> Database -> Account model.
#
# The chart does not expose collection management. It creates only the internal
# placeholder collection needed for MongoDB to persist an otherwise empty DB.
resource "helm_release" "mongodb_management" {
  for_each = local.mongodb_management_deployments

  name      = "${each.key}-mongodb-management"
  chart     = "${path.module}/mongodb-chart"
  namespace = var.mongodb_namespace
  wait      = true
  timeout   = var.mongodb_management_timeout_seconds

  values = [
    yamlencode({
      mongodb = {
        name                        = each.key
        image                       = var.mongo_image
        provisionerConnectionSecret = "tc-${each.key}-admin-connection"
        adminConnectionSecret       = "tc-${each.key}-admin-connection"
        placeholderCollection       = var.placeholder_collection
      }

      mongodbDatabases          = local.mongodb_databases[each.key]
      mongodbDatabaseOperations = local.mongodb_database_operations[each.key]

      # The nonce makes a retried one-shot database operation produce a new
      # Helm release revision even when the desired database list is unchanged.
      mongodbManagementNonce = (
        contains(["create_database", "delete_database"], var.operation.action) &&
        var.operation.deployment == each.key
      ) ? var.operation.nonce : ""
    })
  ]

  depends_on = [
    kubernetes_manifest.controller_admin
  ]

  lifecycle {
    precondition {
      condition = (
        var.operation.action != "delete_database" ||
        var.operation.deployment != each.key ||
        var.allow_destructive_mongodb_operations
      )
      error_message = "Set allow_destructive_mongodb_operations=true to approve deleteDatabase."
    }

    precondition {
      condition = (
        !contains(["create_database", "delete_database"], var.operation.action) ||
        var.operation.deployment != each.key ||
        contains(
          [for database in local.mongodb_databases[each.key] : lower(database.name)],
          lower(var.operation.database)
        )
      )
      error_message = "MongoDB database operations may target only the selected deployment."
    }
  }
}


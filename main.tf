resource "helm_release" "mongodb_provisioning" {
  name      = "mongodb-provisioning"
  chart     = "./mongodb-chart"
  namespace = "mongodb"

  values = [
    yamlencode({
      mongodbDatabases          = var.mongodb_databases
      mongodbRequestUsers       = var.mongodb_request_users
      mongodbDatabaseOperations = var.mongodb_database_operations
    })
  ]
}

locals {
  # AuditState is represented as a temporary database operation.
  # Most normal Terraform runs do not contain an audit operation.
  mongodb_runtime_audit_requested = anytrue([
    for operation in var.mongodb_database_operations :
    try(operation.action, "") == "auditState"
  ])
}

# During an AuditState run, the Kubernetes Job publishes the MongoDB runtime
# inventory to a ConfigMap named mongodb-runtime-audit.
#
# count is zero during normal non-audit runs. This prevents Terraform from
# trying to read the ConfigMap when an audit was not requested.
data "kubernetes_resources" "mongodb_runtime_audit" {
  count = local.mongodb_runtime_audit_requested ? 1 : 0

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = "mongodb"
  field_selector = "metadata.name=mongodb-runtime-audit"

  depends_on = [helm_release.mongodb_provisioning]
}

# MongoControl.py reads this output from the normal Spacelift Terraform logs.
#
# IMPORTANT:
# On a normal non-audit run, the data source above has zero instances.
# Therefore, directly referencing [0] would cause Terraform's
# "Invalid index on empty tuple" error.
#
# try() safely returns an empty string when no audit result exists.
# During an actual AuditState run, the ConfigMap exists and the base64
# runtime-state payload is returned instead.
output "mongodb_runtime_audit_result" {
  value = try(
    "MONGOCONTROL_RUNTIME_STATE_B64=${data.kubernetes_resources.mongodb_runtime_audit[0].objects[0].binaryData["runtime-state.json"]}",
    ""
  )
}

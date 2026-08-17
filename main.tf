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
  mongodb_runtime_audit_requested = anytrue([
    for operation in var.mongodb_database_operations :
    try(operation.action, "") == "auditState"
  ])
}

# The audit Job writes its result to a short-lived ConfigMap. Reading that
# ConfigMap through Terraform avoids depending on kubectl being installed in
# the Spacelift runner container.
data "kubernetes_resources" "mongodb_runtime_audit" {
  count = local.mongodb_runtime_audit_requested ? 1 : 0

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = "mongodb"
  field_selector = "metadata.name=mongodb-runtime-audit"

  depends_on = [helm_release.mongodb_provisioning]
}

# Terraform prints root outputs at the end of apply. MongoControl.py reads this
# marker from the normal Spacelift Terraform logs and decodes the base64 JSON.
output "mongodb_runtime_audit_result" {
  value = local.mongodb_runtime_audit_requested && length(data.kubernetes_resources.mongodb_runtime_audit[0].objects) > 0 ? (
    "MONGOCONTROL_RUNTIME_STATE_B64=${try(data.kubernetes_resources.mongodb_runtime_audit[0].objects[0].binaryData["runtime-state.json"], "")}"
  ) : ""
}

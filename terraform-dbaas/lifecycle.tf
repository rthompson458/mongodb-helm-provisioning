# One-shot lifecycle checks and lock operations that are intentionally outside
# the Helm database-management path.
#
# Python supplies the requested operation; Terraform owns execution ordering and
# invokes lifecycle.sh with the normalized operation environment.

# Terraform still owns imperative lifecycle actions that remain outside the
# Helm database-management path: deployment-empty validation, authentication
# verification, deployment locking, and K3D static local storage. Python only
# supplies the operation and reports its result.
resource "terraform_data" "lifecycle_operation" {
  count = contains([
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


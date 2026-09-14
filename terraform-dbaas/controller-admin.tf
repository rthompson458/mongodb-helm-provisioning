# Hidden controller-administrator credential and MongoDBUser per deployment.
#
# The password is generated ephemerally and written only through write-only
# Vault/Kubernetes fields so plaintext is not stored in Terraform state.

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
      "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
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
        "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
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


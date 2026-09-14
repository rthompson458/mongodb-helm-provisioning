# Managed database credentials, password Secrets, and MongoDBUser resources.
#
# Each database owns exactly three account identities: Owner, ReadWrite, and
# Read. Owner disablement removes only its MongoDBUser; credential material is
# retained so the supported rotation/enable workflow remains deterministic.

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
      managed_by   = "privateWorkerReplacement"
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
      "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
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
        "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
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


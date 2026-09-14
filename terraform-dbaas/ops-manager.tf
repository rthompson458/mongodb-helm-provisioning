# Ops Manager connection data and the shared ConfigMap consumed by MongoDB CRs.
#
# This file owns only the Terraform-visible Ops Manager connection bridge.
# Per-deployment Ops Manager project cleanup remains controller-driven because
# those projects are created by the MongoDB Operator outside Terraform state.

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
      "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
    }
  }

  data = {
    baseUrl = data.kubernetes_config_map_v1.ops_manager_source.data["baseUrl"]
    orgId   = data.kubernetes_config_map_v1.ops_manager_source.data["orgId"]
  }
}


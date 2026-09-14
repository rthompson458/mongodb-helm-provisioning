# Terraform-managed static-local storage lifecycle for ReplicaSets and
# ShardedClusters.
#
# These resource names and for_each keys are intentionally unchanged from the
# former main.tf layout so this organizational refactor does not move state.

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
      TC_PVC_NAME          = self.input.pvc_name
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
      TC_PVC_NAME          = try(self.input.pvc_name, "")
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
      TC_PVC_NAME          = self.input.pvc_name
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
      TC_PVC_NAME          = try(self.input.pvc_name, "")
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


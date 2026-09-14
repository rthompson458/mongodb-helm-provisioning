# MongoDB Kubernetes Operator custom resources for managed deployments.
#
# ReplicaSet and ShardedCluster topology remain declarative Terraform desired
# state. The MongoDB Operator/Ops Manager perform runtime reconciliation.

resource "kubernetes_manifest" "replica_set" {
  for_each = local.replica_sets

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDB"

    metadata = {
      name      = each.key
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
        "dbaas.replica-set"            = each.key
      }
    }

    spec = merge(
      {
        type       = "ReplicaSet"
        members    = each.value.members
        version    = each.value.version
        persistent = each.value.persistent

        security = {
          authentication = {
            enabled            = true
            modes              = ["SCRAM"]
            ignoreUnknownUsers = false
          }
        }

        opsManager = {
          configMapRef = {
            name = kubernetes_config_map_v1.controller_ops_manager_projects.metadata[0].name
          }
        }

        credentials = var.ops_manager_credentials_secret
      },
      each.value.persistent ? {
        podSpec = {
          persistence = {
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.replica-set" = each.key
                  }
                }
              } : {}
            )
          }
        }
      } : {}
    )
  }

  depends_on = [terraform_data.replica_set_storage]
}

# ShardedCluster topology is always controlled through Terraform desired state.
# In particular, DeleteShard lowers spec.shardCount here; Python never issues a
# direct MongoDB removeShard or directly deletes shard pods. The MongoDB
# Kubernetes Operator/Ops Manager reconciles the supported scale-down, and only
# after the removed shard StatefulSets are gone does Terraform clean old storage.
resource "kubernetes_manifest" "sharded_cluster" {
  for_each = local.sharded_clusters

  manifest = {
    apiVersion = "mongodb.com/v1"
    kind       = "MongoDB"

    metadata = {
      name      = each.key
      namespace = var.mongodb_namespace
      labels = {
        "app.kubernetes.io/managed-by" = "privateWorkerReplacement"
        "dbaas.deployment"             = each.key
        "dbaas.deployment-type"        = "ShardedCluster"
      }
    }

    spec = merge(
      {
        type                 = "ShardedCluster"
        shardCount           = each.value.shard_count
        mongodsPerShardCount = each.value.members_per_shard
        mongosCount          = each.value.mongos_count
        configServerCount    = each.value.config_server_count
        version              = each.value.version
        persistent           = each.value.persistent

        security = {
          authentication = {
            enabled            = true
            modes              = ["SCRAM"]
            ignoreUnknownUsers = false
          }
        }

        opsManager = {
          configMapRef = {
            name = kubernetes_config_map_v1.controller_ops_manager_projects.metadata[0].name
          }
        }

        credentials = var.ops_manager_credentials_secret
      },
      each.value.persistent ? {
        shardPodSpec = {
          persistence = {
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.sharded-cluster" = each.key
                    "dbaas.component"       = "shard"
                  }
                }
              } : {}
            )
          }
        }

        configSrvPodSpec = {
          persistence = {
            single = merge(
              {
                storage      = each.value.storage_size
                storageClass = each.value.storage_class
              },
              each.value.storage_mode == "static-local" ? {
                labelSelector = {
                  matchLabels = {
                    "dbaas.sharded-cluster" = each.key
                    "dbaas.component"       = "config"
                  }
                }
              } : {}
            )
          }
        }
      } : {}
    )
  }

  depends_on = [
    terraform_data.sharded_cluster_shard_storage,
    terraform_data.sharded_cluster_config_storage
  ]
}


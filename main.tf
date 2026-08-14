resource "helm_release" "mongodb_provisioning" {
  name      = "mongodb-provisioning"
  chart     = "./mongodb-chart"
  namespace = "mongodb"

  values = [
    yamlencode({
      mongodbDatabases    = var.mongodb_databases
      mongodbRequestUsers = var.mongodb_request_users
    })
  ]
}

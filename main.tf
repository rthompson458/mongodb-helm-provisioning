resource "helm_release" "mongodb_provisioning" {
  name      = "mongodb-provisioning"
  chart     = "./mongodb-chart"
  namespace = "mongodb"
}

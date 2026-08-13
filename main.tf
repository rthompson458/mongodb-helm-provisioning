resource "helm_release" "spacelift_helm_test" {
  name      = "spacelift-helm-test"
  chart     = "./chart"
  namespace = "default"
}
resource "helm_release" "mongodb_provisioning" {
  name           = "mongodb-provisioning"
  chart          = "./mongodb-chart"
  namespace      = "mongodb"
  take_ownership = true
}

resource "helm_release" "spacelift_helm_test" {
  name      = "spacelift-helm-test"
  chart     = "./chart"
  namespace = "default"
}

provider "helm" {
  kubernetes = {
    config_path = "/mnt/workspace/.kube/config"
  }
}

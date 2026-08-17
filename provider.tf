provider "helm" {
  kubernetes = {
    config_path = "/mnt/workspace/.kube/config"
  }
}

provider "kubernetes" {
  config_path = "/mnt/workspace/.kube/config"
}

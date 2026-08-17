terraform {
  required_version = ">= 1.5.7"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }

    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

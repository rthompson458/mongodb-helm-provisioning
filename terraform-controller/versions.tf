terraform {
  required_version = ">= 1.11.0"

  backend "kubernetes" {}

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.2"
    }

    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }

    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.10"
    }
  }
}

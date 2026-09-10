terraform {
  required_version = ">= 1.11.0"

  backend "kubernetes" {}

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.2.1"
    }

    # Helm is used for short-lived MongoDB database management Jobs. This
    # provider/version follows the database-management implementation being
    # folded into privateWorkerReplacement.
    helm = {
      source  = "hashicorp/helm"
      version = "3.3.0"
    }

    random = {
      source  = "hashicorp/random"
      version = "3.9.0"
    }

    vault = {
      source  = "hashicorp/vault"
      version = "5.10.1"
    }
  }
}

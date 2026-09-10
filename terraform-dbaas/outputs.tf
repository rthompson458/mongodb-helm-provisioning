output "managed_deployments" {
  value = {
    for deployment_key, deployment in var.deployments : deployment_key => {
      display_name    = deployment.display_name
      deployment_type = deployment.deployment_type
      databases       = keys(deployment.databases)
    }
  }
}

output "mongodb_management_releases" {
  description = "Terraform-managed Helm release name for each deployment with managed databases or an active database operation"
  value = {
    for deployment_key, release in helm_release.mongodb_management :
    deployment_key => release.name
  }
}

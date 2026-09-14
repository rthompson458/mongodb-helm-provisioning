# Root-module outputs consumed by controller diagnostics and tests.

output "managed_deployments" {
  value = {
    for deployment_key, deployment in var.deployments : deployment_key => {
      display_name    = deployment.display_name
      deployment_type = deployment.deployment_type
      databases       = keys(deployment.databases)
    }
  }
}

# Root Terraform module organization
#
# Terraform loads all *.tf files in this directory together as one module.
# Resources were split from the former ~1,000-line main.tf by responsibility to
# make ownership, review, and troubleshooting easier. Resource block names,
# for_each keys, expressions, dependencies, and provider configuration were not
# intentionally changed by this structural refactor.
#
# File map:
#   locals.tf              - derived desired state and normalized collections
#   ops-manager.tf         - Ops Manager connection bridge
#   storage.tf             - static-local storage lifecycle
#   deployments.tf         - MongoDB ReplicaSet/ShardedCluster CRs
#   metadata.tf            - deployment/database lifecycle metadata in Vault
#   controller-admin.tf    - hidden controller-admin credential and MongoDBUser
#   database-management.tf - Helm database create/delete workflow
#   database-accounts.tf   - database credentials, Secrets, and MongoDBUsers
#   lifecycle.tf           - one-shot validation/authentication/lock operations
#   outputs.tf             - root-module outputs
#
# providers.tf, variables.tf, and versions.tf retain their existing purposes.
# Keep this file as the module index; production resources belong in the
# responsibility-specific files above.

# terraform-dbaas root module

This directory is one Terraform root module. Terraform loads every `*.tf` file
in the directory together; file names are for human organization and do not form
separate modules.

The former `main.tf` grew to roughly 1,000 lines and mixed desired-state
normalization, storage, MongoDB deployment resources, Vault metadata,
credentials, Helm database management, and one-shot lifecycle operations. The
configuration is now split by responsibility so a reviewer can find and defend
one behavior without reading the entire root module.

## File ownership

| File | Responsibility |
| --- | --- |
| `main.tf` | Human-readable module index only; no production resources |
| `locals.tf` | Derived desired state, normalized databases/accounts, deployment subsets, storage collections |
| `ops-manager.tf` | Existing Ops Manager connection source and shared controller ConfigMap |
| `storage.tf` | Static-local ReplicaSet and ShardedCluster storage lifecycle |
| `deployments.tf` | MongoDB Kubernetes Operator ReplicaSet and ShardedCluster custom resources |
| `metadata.tf` | Vault deployment/database lifecycle metadata |
| `controller-admin.tf` | Hidden controller administrator password, Vault record, Secret, and MongoDBUser |
| `database-management.tf` | Helm database materialization/deletion release |
| `database-accounts.tf` | Owner/ReadWrite/Read credentials, Secrets, and MongoDBUsers |
| `lifecycle.tf` | One-shot validation, authentication, and deployment-lock operations |
| `outputs.tf` | Root-module outputs |
| `variables.tf` | Root-module input contract and validation |
| `providers.tf` | Vault, Kubernetes, and Helm provider configuration |
| `versions.tf` | Terraform/backend/provider version constraints |

## State-safety rule

Moving a Terraform block from one `.tf` file to another does **not** change its
Terraform address. Resource addresses depend on the block type/name and instance
key, not on the source filename.

This refactor therefore intentionally preserves:

- every resource/data/ephemeral/output block name;
- every `for_each` and `count` expression;
- every key used to address resource instances;
- every dependency and provider reference;
- every lifecycle/provisioner expression.

No `moved` blocks are required for a file-only reorganization. If a future
change renames a Terraform block or changes an instance key, treat that as a
state migration and review it separately.

## Ownership boundaries

Terraform owns declared desired-state resources and the imperative lifecycle
operations represented by `terraform_data.lifecycle_operation`. The MongoDB
Kubernetes Operator/Ops Manager own runtime reconciliation of MongoDB CRs.
Python validates requests, coordinates asynchronous work, waits for convergence,
reports status, and performs narrowly documented cleanup of cross-plane artifacts
that are created outside Terraform state.

Vault secret paths are part of the controller's recovery contract. Passwords are
generated ephemerally and written through write-only provider fields so plaintext
passwords are not stored in Terraform state.

## Validation

At minimum, changes in this directory must pass:

```bash
terraform fmt -check -recursive terraform-dbaas
terraform -chdir=terraform-dbaas init -backend=false
terraform -chdir=terraform-dbaas validate
```

Repository CI runs these checks plus the Python unit/regression suite. For a
stateful behavior change, also run the appropriate live harness profile. A pure
file-organization change should produce no infrastructure changes because the
Terraform addresses and expressions are unchanged.

# Controller CLI Interface Separation

## Purpose

The MongoDB DBaaS controller exposes two intentionally separate command-line interfaces.

| Interface | Audience | Responsibility |
| --- | --- | --- |
| `terraformController.py` | DBaaS user / customer demo | Normal service lifecycle and status |
| `terraformControllerAdmin.py` | Platform administrator | Diagnostics, reconciliation, and exceptional recovery |

This separation keeps customer workflows simple while preserving the operational controls needed to support the service.

## Customer interface

The public interface exposes:

~~~text
AddReplicaSet
DeleteReplicaSet
ListReplicaSets
ListReplicaSet

AddShardedCluster
DeleteShardedCluster
ListShardedClusters
ListShardedCluster
ListShards

AddShard
DeleteShard

AddDatabase
DeleteDatabase
ListDatabases
ListDatabase

RotatePasswords
DisableOwner
~~~

Long-running requests are processed in the background, but the customer sees only service-oriented acknowledgement and normal resource status commands.

The public interface does not expose:

- internal Operation IDs;
- worker PIDs;
- operation journal paths;
- Terraform recovery commands;
- deployment-lock recovery;
- orphaned-state recovery;
- controller-wide Reconcile.

## Administrator interface

The administrator interface exposes:

~~~text
ListManagedResources
ListOperations
ListOperation OPERATION_ID
Reconcile
RecoverDeploymentLock SHARDED_CLUSTER --confirm
RecoverOrphanedResources --confirm
~~~

Administrator help text explicitly identifies these commands as operator/recovery tools.

`ListManagedResources` is the read-only administrator inventory used to verify
that Vault-backed deployment inventory, managed MongoDB resources, managed PVCs,
managed PVs, and deployment locks are all empty before a clean test or handoff.
The shorter name `ListResources` is intentionally not supported because it
could imply a cluster-wide resource listing.

## Shared implementation

The two CLIs are separate interfaces over shared controller modules.

~~~text
terraformController.py
  -> terraform_controller/cli.py

terraformControllerAdmin.py
  -> terraform_controller/admin_cli.py

Both use shared:
  terraform_controller/deployments.py
  terraform_controller/databases.py
  terraform_controller/maintenance.py
  terraform_controller/deployment_lock.py
  terraform_controller/terraform_runner.py
  terraform_controller/async_operations.py
  terraform_controller/kube.py
  terraform_controller/vault.py
~~~

This avoids duplicating lifecycle logic and keeps the Terraform-driven mutation boundary intact.

## Asynchronous operation model

Public long-running commands still create a private operation journal entry and launch a detached worker.

The public response intentionally hides those internal identifiers.

The acceptance harness is internal engineering tooling, so it correlates the private journal entry and polls detailed status through `terraformControllerAdmin.py ListOperation`.

This allows:

- a professional public interface;
- deterministic automated acceptance testing;
- deep administrator diagnostics;
- no loss of controller observability.

## Security boundary

Two executable names improve clarity and reduce accidental exposure, but they do not replace authorization.

Production should additionally restrict:

- who can execute the administrator CLI;
- Vault administrator policies;
- Kubernetes mutation permissions;
- Terraform backend access;
- host access;
- administrator logs and audit evidence.

The intended operating model is:

~~~text
DBaaS customer
  -> terraformController.py

Authorized service operator
  -> terraformControllerAdmin.py
  -> privileged supporting infrastructure
~~~

## Documentation ownership

Customer documentation:

~~~text
README-terraformController.md
~~~

Administrator runbook:

~~~text
README-terraformControllerAdmin.md
~~~

Testing documentation:

~~~text
tests/README.md
~~~

Keeping these audiences separate is part of the product design, not merely a documentation preference.

# terraformController

`terraformController.py` is the temporary local orchestration layer for the MongoDB DBaaS proof of concept.

The long-term execution path is Spacelift plus a private worker. The private worker will run the same Terraform code that this local controller runs now.

## Design rule

**Terraform performs all managed changes.**

Python does these tasks:

- validate command input,
- read Vault inventory,
- read Kubernetes/MongoDB status,
- pass desired state and lifecycle operations to Terraform,
- wait for status convergence,
- format results for the user.

Python does **not** directly create, delete, rotate, enable, or disable managed MongoDB/Vault resources.

Terraform performs:

- ReplicaSet creation and deletion,
- K3D static local-storage preparation and cleanup,
- database materialization and deletion,
- MongoDB role-account creation and deletion,
- password generation and rotation,
- owner-account disable/enable state enforcement,
- Vault credential and lifecycle metadata management.

## Managed hierarchy

```text
ReplicaSet
  Database
    <Database>_owner      -> dbOwner
    <Database>_readWrite  -> readWrite
    <Database>_read       -> read
```

A ReplicaSet can contain multiple application databases.

The three accounts above are the only application accounts created and managed by this controller.

## Commands

```text
AddReplicaSet REPLICASET
DeleteReplicaSet REPLICASET --confirm
ListReplicaSets
ListReplicaSet REPLICASET

AddDatabase REPLICASET DATABASE
DeleteDatabase REPLICASET DATABASE --confirm
ListDatabases [REPLICASET]
ListDatabase REPLICASET DATABASE
RotatePasswords REPLICASET DATABASE

DisableOwner REPLICASET DATABASE --confirm
Reconcile
```

Examples:

```bash
python3 terraformController.py AddReplicaSet RS1
python3 terraformController.py ListReplicaSets
python3 terraformController.py AddDatabase RS1 HouseInfo
python3 terraformController.py ListDatabases RS1
python3 terraformController.py ListDatabases
python3 terraformController.py ListDatabase RS1 HouseInfo
python3 terraformController.py RotatePasswords RS1 HouseInfo
python3 terraformController.py DisableOwner RS1 HouseInfo --confirm
python3 terraformController.py DeleteDatabase RS1 HouseInfo --confirm
python3 terraformController.py DeleteReplicaSet RS1 --confirm
```

Use help at every level:

```bash
python3 terraformController.py --help
python3 terraformController.py AddReplicaSet --help
python3 terraformController.py DeleteReplicaSet --help
python3 terraformController.py AddDatabase --help
python3 terraformController.py DeleteDatabase --help
python3 terraformController.py ListDatabases --help
python3 terraformController.py RotatePasswords --help
python3 terraformController.py DisableOwner --help
python3 terraformController.py Reconcile --help
```

## ReplicaSet rules

`AddReplicaSet RS1` creates one user-facing MongoDB ReplicaSet.

Default configuration:

```text
Members:       3
MongoDB:       8.0.29
Persistent:    true
StorageClass:  mongodb-data-local
Storage:       16Gi per member
```

Only ReplicaSets managed by `terraformController` appear in `ListReplicaSets`.

Ops Manager's own Application Database ReplicaSet and any other system/internal MongoDB resources are not listed as user-facing ReplicaSets.

`ListReplicaSets` reports the live Kubernetes/MongoDB phase, for example:

```text
REPLICA SET  K8S RESOURCE  PHASE     MEMBERS  MONGODB  DATABASES
-----------  ------------  --------  -------  -------  ---------
RS1          rs1           Running   3        8.0.29   2
RS2          rs2           Pending   3        8.0.29   0
```

`DeleteReplicaSet` requires `--confirm`.

Deletion is allowed only when there are zero application databases on the ReplicaSet.

The runtime validation ignores these MongoDB system databases:

```text
admin
config
local
```

Terraform performs the final MongoDB emptiness check before the ReplicaSet is removed.

## Database rules

A database must be created on a ReplicaSet that already exists and is in phase `Running`.

For example:

```bash
python3 terraformController.py AddDatabase RS1 HouseInfo
```

The controller refuses the request when RS1 is Absent, Pending, Creating, Failed, or any other state except `Running`.

MongoDB does not retain a truly empty user database. Terraform therefore runs a temporary Kubernetes Job that creates this internal collection:

```text
__dbaas_metadata
```

That collection materializes the database without inserting application data.

`DeleteDatabase` requires `--confirm`.

Before deletion, Terraform runs a temporary MongoDB Job that verifies there are no application collections other than `__dbaas_metadata` and MongoDB `system.*` collections. If application collections remain, Terraform fails and the database and credentials remain intact.

When deletion is allowed, Terraform removes:

- the MongoDB database,
- `<DB>_owner`,
- `<DB>_readWrite`,
- `<DB>_read`,
- their Kubernetes password resources,
- their Vault credentials,
- their database lifecycle metadata.

## Fixed database accounts

Creating `HouseInfo` creates exactly:

```text
HouseInfo_owner      -> dbOwner on HouseInfo
HouseInfo_readWrite  -> readWrite on HouseInfo
HouseInfo_read       -> read on HouseInfo
```

No arbitrary AddUser/DeleteUser/ChangeRole commands are part of this MVP.

The ReplicaSet is part of the security boundary. The same database or username text can exist on a different ReplicaSet with completely separate credentials and MongoDB security state.

## Vault layout

Human-facing Vault paths mirror ReplicaSet, database, and account ownership.

For `RS1/HouseInfo`:

```text
mongodb/RS1/HouseInfo/HouseInfo_owner
mongodb/RS1/HouseInfo/HouseInfo_readWrite
mongodb/RS1/HouseInfo/HouseInfo_read
```

Lifecycle metadata is stored at:

```text
mongodb/RS1/_metadata
mongodb/RS1/HouseInfo/_metadata
```

The metadata records include values needed for reliable automation, including:

```text
ReplicaSet creation time
Database creation time
MongoDB version
Member count
Storage settings
Password rotation version
Last rotation time
Owner enabled/disabled state
Owner disabled time
```

The controller can also read the previous `mongodb/replica-sets/...` layout so an existing environment can be migrated safely when Terraform is first reconciled with this version.

## Vault credential retrieval

After `AddDatabase` succeeds, the command prints:

- the Vault UI URL,
- the three Vault credential paths,
- the password rotation interval.

Default local Vault UI:

```text
http://127.0.0.1:8200/ui/
```

An authorized Vault administrator can log in and retrieve the **current** password for any managed account. Password rotation replaces the current credential in Vault.

The passwords are generated by Terraform as ephemeral values and are passed to Vault and Kubernetes through write-only provider fields. Plaintext passwords are not intentionally stored in Terraform state.

## Password rotation

Default rotation interval:

```text
30 days
```

For the local MVP, rotation is initiated with:

```bash
python3 terraformController.py RotatePasswords RS1 HouseInfo
```

The command rotates all three passwords together:

```text
HouseInfo_owner
HouseInfo_readWrite
HouseInfo_read
```

The production installation is expected to invoke the same Terraform workflow from an approved scheduler. Possible schedulers include:

- Spacelift scheduled runs,
- cron,
- AWS EventBridge/Lambda,
- another customer-approved automation platform.

The scheduler is outside the Terraform data model. The important point is that the scheduled process invokes the same Terraform password-rotation workflow.

## Owner account lifecycle

The owner password rotates every 30 days like the other two passwords.

At the first rotation at or after 30 days from database creation, Terraform also disables the owner in MongoDB.

Disabled means:

```text
The Owner MongoDBUser does not exist.
The account cannot authenticate to MongoDB.
The current Owner password still exists in Vault.
Future RotatePasswords runs continue rotating the stored Owner password.
```

Removing only the Vault secret would not be sufficient because a previously known MongoDB password could still authenticate. The actual enforcement point is MongoDB.

### Re-enabling Owner

Vault holds the lifecycle state.

An authorized administrator can change this field in:

```text
mongodb/<RS>/<DB>/_metadata
```

from:

```text
owner_disabled = true
```

to:

```text
owner_disabled = false
```

Then run:

```bash
python3 terraformController.py Reconcile
```

Terraform recreates the Owner `MongoDBUser` using the current password already stored by the managed lifecycle.

Vault itself does not directly change MongoDB merely because a KV value changed. A Terraform reconcile or future automated equivalent is required to apply the Vault lifecycle state to MongoDB.

If the owner is later re-enabled after day 30, the next scheduled `RotatePasswords` run disables it again while rotating its password.

`DisableOwner --confirm` is retained as a direct lifecycle/demo command so the customer can see the disabled behavior without waiting 30 days.

## ListDatabases behavior

One ReplicaSet:

```bash
python3 terraformController.py ListDatabases RS1
```

All managed ReplicaSets:

```bash
python3 terraformController.py ListDatabases
```

The listings show each database's three accounts, account status, and time until password rotation is due.

## Terraform lifecycle operations

Some MongoDB operations are imperative even though Terraform owns the workflow. These include:

- materializing an empty database,
- confirming a database is empty before deletion,
- dropping a database,
- confirming a ReplicaSet has no application databases,
- preparing K3D static local storage,
- cleaning K3D static local storage.

Terraform handles these with a one-shot `terraform_data.lifecycle_operation` resource that calls:

```text
terraform-controller/scripts/lifecycle.sh
```

The script creates short-lived Kubernetes MongoDB Jobs for MongoDB runtime actions. A failed Job causes the Terraform apply to fail, which prevents the Python controller from advancing to the destructive follow-up step.

This is deliberate. Python is not the component performing the mutation.

## Internal controller account

Each managed ReplicaSet has one hidden internal administrator:

```text
tc_<replica-set>_admin
```

This is not one of the three application database accounts and is never shown as an application user.

Terraform uses it only for controller runtime operations such as:

- creating the internal database collection,
- checking database contents before deletion,
- dropping an approved empty database,
- confirming that a ReplicaSet contains no application databases.

The internal credential is stored under:

```text
mongodb/<ReplicaSet>/_internal/controller-admin
```

## K3D static local storage

The current nix-k3d environment uses static local volumes.

Default configuration:

```text
StorageClass: mongodb-data-local
Node:         k3d-nix-dev-server-0
Base path:    /home/rich/mongodb-dbaas-dev/storage
```

Before Terraform creates a persistent ReplicaSet, Terraform runs the storage preparation lifecycle operation. It creates the local directories and PersistentVolumes required by the ReplicaSet.

For RS1 with three members:

```text
/home/rich/mongodb-dbaas-dev/storage/rs1-0
/home/rich/mongodb-dbaas-dev/storage/rs1-1
/home/rich/mongodb-dbaas-dev/storage/rs1-2
```

After an empty ReplicaSet is deleted, Terraform runs the cleanup operation for its retained PVCs, static PVs, and local directories.

## Ops Manager project isolation

MongoDB permits only one MongoDB resource per Ops Manager project in this Operator workflow.

The controller therefore reads `baseUrl` and `orgId` from the existing working Ops Manager ConfigMap and creates:

```text
tc-ops-manager-projects
```

That ConfigMap intentionally omits `projectName`.

The Operator can then use a distinct Ops Manager project for each MongoDB resource:

```text
rs1 -> Ops Manager project rs1
rs2 -> Ops Manager project rs2
```

The existing organization credentials Secret is reused for Ops Manager API authentication.

## Reconcile

```bash
python3 terraformController.py Reconcile
```

`Reconcile`:

1. reads managed desired state from Vault,
2. refreshes the Terraform module from GitHub,
3. applies the complete desired state,
4. waits for managed ReplicaSets and the internal controller accounts to converge.

Use it after:

- a supported lifecycle metadata change in Vault,
- a controller/Terraform upgrade,
- repairing a Pending deployment,
- re-enabling an Owner account through Vault.

## Vault token

Do not put the Vault token in `terraformController.config`.

Set it in the shell:

```bash
export VAULT_TOKEN='<current-vault-token>'
```

The local Vault port-forward must be running when using the default address.

## GitHub source of truth

The controller refreshes Terraform from:

```text
https://github.com/rthompson458/mongodb-helm-provisioning.git
```

and executes:

```text
terraform-controller/
```

The normal working copy is not Terraform's execution source. The controller uses its dedicated cache under:

```text
~/.cache/terraformController/
```

## Terraform state

Terraform uses the Kubernetes backend.

Default state location:

```text
namespace:     mongodb
secret suffix: mongodb-vault-controller
context:       k3d-nix-dev
```

## Relationship to Spacelift

The current local flow is:

```text
User
  -> terraformController.py
      -> Terraform from GitHub
          -> Kubernetes / MongoDB Operator / Ops Manager / Vault
```

The intended installed flow is:

```text
User or scheduled process
  -> Spacelift
      -> Private Worker
          -> same Terraform from GitHub
              -> Kubernetes / MongoDB Operator / Ops Manager / Vault
```

The Terraform module is the durable implementation. The local Python controller is the temporary execution/orchestration replacement for the private worker path.

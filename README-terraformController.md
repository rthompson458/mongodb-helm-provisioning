# terraformController

`terraformController.py` is the local command-line controller for the K3D MongoDB/Vault demo.

It manages this hierarchy:

```text
ReplicaSet
  Database
    <Database>_owner
    <Database>_readWrite
    <Database>_read
```

Vault keeps credentials unique by full path:

```text
secret/mongodb/replica-sets/<replica-set>/databases/<database>/accounts/<account-type>
```

Therefore `RS1/Tank/Owner` and `RS2/Tank/Owner` are different Vault credentials.

## Commands

```text
AddReplicaSet REPLICASET
DeleteReplicaSet REPLICASET
ListReplicaSets
ListReplicaSet REPLICASET

AddDatabase REPLICASET DATABASE
DeleteDatabase REPLICASET DATABASE
RotatePassword REPLICASET DATABASE
DisableOwner REPLICASET DATABASE
ListDatabases
ListDatabase REPLICASET DATABASE
RecoverPassword REPLICASET DATABASE ACCOUNT_TYPE

ResetVault --confirm
```

Examples:

```bash
python3 terraformController.py AddReplicaSet RS1
python3 terraformController.py AddDatabase RS1 Tank
python3 terraformController.py ListDatabase RS1 Tank
python3 terraformController.py RecoverPassword RS1 Tank ReadWrite
python3 terraformController.py RotatePassword RS1 Tank
python3 terraformController.py DisableOwner RS1 Tank
python3 terraformController.py DeleteDatabase RS1 Tank
python3 terraformController.py DeleteReplicaSet RS1
```

Use top-level or command-specific help:

```bash
python3 terraformController.py --help
python3 terraformController.py AddReplicaSet --help
python3 terraformController.py AddDatabase --help
python3 terraformController.py RecoverPassword --help
python3 terraformController.py ResetVault --help
```

## ReplicaSet rules

`AddReplicaSet` creates a new MongoDB Kubernetes Operator `MongoDB` resource.

The default config creates:

```text
3 members
MongoDB 8.0.29
persistent storage
local-path StorageClass
16Gi per member
```

Change those defaults in `terraformController.config` when needed.

`DeleteReplicaSet` has no force option. It is blocked when:

1. terraformController still has a managed database under that ReplicaSet, or
2. MongoDB itself reports any non-system database on that ReplicaSet.

The controller ignores MongoDB's built-in `admin`, `config`, and `local` databases for this check.

## Database rules

`AddDatabase RS1 Tank` creates an empty MongoDB database and these accounts:

```text
Tank_owner      -> dbOwner on Tank
Tank_readWrite  -> readWrite on Tank
Tank_read       -> read on Tank
```

MongoDB does not retain a truly empty user database, so the controller creates one empty internal collection named:

```text
__dbaas_metadata
```

This makes the database exist immediately without inserting application data.

`DeleteDatabase` is blocked if the database contains any non-system collection other than `__dbaas_metadata`. This prevents the controller from silently deleting application data.

## Owner disable behavior

`DisableOwner` removes the owner `MongoDBUser` resource from MongoDB. The Vault credential and Kubernetes password Secret remain.

This provides an actual disabled state instead of merely removing roles from an account that can still authenticate.

`RotatePassword` always rotates all three stored passwords. If Owner is disabled, its credential rotates but its MongoDBUser remains absent.

## Password rotation

The configured rotation interval is 30 days.

`ListDatabases`, `ListDatabase`, and `ListReplicaSet` report the time remaining from the last successful creation/rotation time.

`RotatePassword` is the manual demo action that rotates all three database passwords in one operation and restarts the 30-day countdown.

Automatic scheduling is intentionally not part of this first controller version. A scheduler can call the same command later without changing the data model.

## RecoverPassword

`RecoverPassword` is demo-only.

Account type is case-insensitive:

```bash
python3 terraformController.py RecoverPassword RS1 Tank Owner
python3 terraformController.py RecoverPassword RS1 Tank read
python3 terraformController.py RecoverPassword RS1 Tank READWRITE
```

It intentionally prints the selected Vault password to the terminal so password rotation can be demonstrated.

## Internal controller account

Each terraformController-created ReplicaSet has one hidden internal administrator:

```text
tc_<replica-set>_admin
```

This account is not one of the three application role accounts and is not shown by normal database listings.

It is used only for controller operations such as:

- materializing an empty database,
- verifying that a database is empty before deletion,
- verifying that a ReplicaSet is truly empty before deletion,
- performing `ResetVault --confirm`.

Its password is stored in Vault and a Kubernetes Secret through Terraform write-only fields.

## ResetVault

```bash
python3 terraformController.py ResetVault --confirm
```

This is intentionally destructive.

For every ReplicaSet created by terraformController, it drops all non-system MongoDB databases and then removes:

- managed database role accounts,
- internal controller accounts,
- managed Kubernetes Secrets,
- managed Vault records,
- managed MongoDB ReplicaSet resources.

ReplicaSets not created by terraformController, such as existing resources already in the cluster, are not touched.

## Vault token

Do not place the Vault token in `terraformController.config`.

Set it in the WSL shell:

```bash
export VAULT_TOKEN='<your-current-vault-token>'
```

Vault is configured at:

```text
http://127.0.0.1:8200
```

The existing Vault port-forward must be running.

## GitHub source of truth

The controller refreshes Terraform from:

```text
https://github.com/rthompson458/mongodb-helm-provisioning.git
```

and executes:

```text
terraform-controller/
```

from a dedicated cache under `~/.cache/terraformController/`.

The normal local working copy is not used as Terraform's execution source.

## Terraform state

The new module uses Terraform's Kubernetes backend.

Default state location:

```text
namespace: mongodb
secret suffix: mongodb-vault-controller
context: k3d-nix-dev
```

This keeps controller state in the K3D cluster instead of tying it to the temporary Git execution clone.

## Existing Helm/Spacelift path

The existing root Terraform and `mongodb-chart/` remain unchanged. They still support the earlier Spacelift/Helm workflow for `mongodb-development`.

The new `terraform-controller/` directory is separate so multi-ReplicaSet local development does not break the existing working chart.

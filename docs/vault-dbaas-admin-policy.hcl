# Example Vault ACL policy for MongoDB DBaaS administrators.
#
# Adjust "secret" if the KV v2 mount name changes.
# This policy is intentionally not attached to an identity by Terraform because
# the customer must choose the production auth method/group mapping.
#
# Capabilities:
# - Browse MongoDB DBaaS KV v2 metadata.
# - Read current application database credentials.
# - Read/update DB lifecycle _metadata so an authorized admin can re-enable Owner.
# - Never read the hidden controller root credential.

path "secret/metadata/mongodb/*" {
  capabilities = ["list", "read"]
}

# Application credentials have three path segments below mongodb:
#   <ReplicaSet>/<Database>/<Username>
path "secret/data/mongodb/+/+/+" {
  capabilities = ["read"]
}

# A more-specific rule permits lifecycle changes only on DB _metadata.
path "secret/data/mongodb/+/+/_metadata" {
  capabilities = ["read", "update"]
}

# Deny the hidden controller administrator credential explicitly.
# This more-specific rule takes precedence over the generic three-segment read.
path "secret/data/mongodb/+/_internal/controller-admin" {
  capabilities = ["deny"]
}

#!/usr/bin/env bash
set -euo pipefail

: "${TC_ACTION:?TC_ACTION is required}"
: "${TC_NAMESPACE:?TC_NAMESPACE is required}"
: "${TC_KUBECONFIG:?TC_KUBECONFIG is required}"

# ReplicaSet storage resources created by older Terraform state still pass
# TC_REPLICA_SET. All new generic lifecycle operations pass TC_DEPLOYMENT.
TC_DEPLOYMENT="${TC_DEPLOYMENT:-${TC_REPLICA_SET:-}}"
: "${TC_DEPLOYMENT:?TC_DEPLOYMENT or TC_REPLICA_SET is required}"

K=(kubectl --kubeconfig "${TC_KUBECONFIG}")
if [[ -n "${TC_KUBE_CONTEXT:-}" ]]; then
  K+=(--context "${TC_KUBE_CONTEXT}")
fi

run_mongo_job() {
  local connection_secret="${1:-tc-${TC_DEPLOYMENT}-admin-connection}"
  local job="tc-runtime-${TC_DEPLOYMENT:0:12}-$(date +%s)-${RANDOM}"
  local manifest

  manifest=$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: ${TC_NAMESPACE}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.deployment: ${TC_DEPLOYMENT}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 120
  template:
    metadata:
      labels:
        app.kubernetes.io/managed-by: terraformController
        dbaas.deployment: ${TC_DEPLOYMENT}
    spec:
      restartPolicy: Never
      containers:
        - name: mongosh
          image: ${TC_MONGO_IMAGE}
          env:
            - name: MONGODB_URI
              valueFrom:
                secretKeyRef:
                  name: ${connection_secret}
                  key: connectionString.standard
            - name: TC_JS
              value: ${TC_JS_JSON}
          command:
            - /bin/bash
            - -lc
            - 'mongosh "\$MONGODB_URI" --quiet --eval "\$TC_JS"'
EOF
)

  printf '%s\n' "$manifest" | "${K[@]}" apply -f - >/dev/null

  local succeeded=0
  local failed=0
  for _ in {1..150}; do
    succeeded=$("${K[@]}" -n "${TC_NAMESPACE}" get job "${job}" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
    failed=$("${K[@]}" -n "${TC_NAMESPACE}" get job "${job}" -o jsonpath='{.status.failed}' 2>/dev/null || true)
    [[ "${succeeded:-0}" -gt 0 ]] && break
    [[ "${failed:-0}" -gt 0 ]] && break
    sleep 2
  done

  local logs
  logs=$("${K[@]}" -n "${TC_NAMESPACE}" logs "job/${job}" 2>&1 || true)
  "${K[@]}" -n "${TC_NAMESPACE}" delete job "${job}" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  printf '%s\n' "$logs"

  if [[ "${succeeded:-0}" -gt 0 ]]; then
    return 0
  fi
  if [[ "${failed:-0}" -gt 0 ]]; then
    echo "Terraform-driven MongoDB runtime Job failed." >&2
  else
    echo "Terraform-driven MongoDB runtime Job timed out." >&2
  fi
  return 1
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

account_resource_name() {
  local account="$1"
  local db_key="${TC_DATABASE,,}"
  # MongoDB database names may contain underscores. Kubernetes resource
  # names may not, so use a DNS-safe copy only for the Kubernetes name.
  local db_resource_key="${db_key//_/-}"
  local digest
  digest=$(python3 -c 'import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:6])' "${TC_DEPLOYMENT}/${db_key}/${account}")
  printf 'tc-%s-%s-%s-%s' "${TC_DEPLOYMENT:0:8}" "${db_resource_key:0:10}" "${account}" "${digest}"
}

verify_account() {
  local account="$1"
  local secret
  secret="$(account_resource_name "${account}")-connection"
  local js="const r=db.runCommand({ping:1});if(!r||r.ok!==1)quit(42);print('TC_RESULT=AUTH_OK');"
  export TC_JS_JSON
  TC_JS_JSON=$(json_string "$js")

  local output=""
  for _ in {1..30}; do
    if output=$(run_mongo_job "$secret" 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$output" >&2
  echo "Credential verification failed for account type '${account}'." >&2
  return 1
}

verify_controller_admin() {
  # A MongoDBUser CR can report Updated slightly before the new SCRAM
  # credential is usable by a real client. Treat successful authentication,
  # not only Kubernetes phase, as the final deployment-readiness signal.
  local js="const r=db.runCommand({ping:1});if(!r||r.ok!==1)quit(42);print('TC_RESULT=CONTROLLER_AUTH_OK');"
  export TC_JS_JSON
  TC_JS_JSON=$(json_string "$js")

  local output=""
  for _ in {1..30}; do
    if output=$(run_mongo_job 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$output" >&2
  echo "Timed out waiting for controller administrator authentication." >&2
  return 1
}

verify_users_absent() {
  local owner="${TC_DATABASE}_owner"
  local readwrite="${TC_DATABASE}_readWrite"
  local read="${TC_DATABASE}_read"
  local owner_json readwrite_json read_json
  owner_json=$(json_string "$owner")
  readwrite_json=$(json_string "$readwrite")
  read_json=$(json_string "$read")
  local auth_db_json
  auth_db_json=$(json_string "${TC_AUTH_DATABASE:-admin}")
  local js="const a=db.getSiblingDB(${auth_db_json});const n=[${owner_json},${readwrite_json},${read_json}];const f=n.filter(x=>a.getUser(x)!==null);if(f.length){print('TC_BLOCKED='+JSON.stringify(f));quit(42);}print('TC_RESULT=USERS_ABSENT');"
  export TC_JS_JSON
  TC_JS_JSON=$(json_string "$js")

  local output=""
  for _ in {1..30}; do
    if output=$(run_mongo_job 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$output" >&2
  echo "Timed out waiting for MongoDB users to be absent." >&2
  return 1
}

verify_owner_absent() {
  local owner="${TC_DATABASE}_owner"
  local owner_json
  owner_json=$(json_string "$owner")
  local auth_db_json
  auth_db_json=$(json_string "${TC_AUTH_DATABASE:-admin}")
  local js="const a=db.getSiblingDB(${auth_db_json});const u=a.getUser(${owner_json});if(u!==null){print('TC_BLOCKED=OWNER_STILL_EXISTS');quit(42);}print('TC_RESULT=OWNER_ABSENT');"
  export TC_JS_JSON
  TC_JS_JSON=$(json_string "$js")

  local output=""
  for _ in {1..30}; do
    if output=$(run_mongo_job 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$output" >&2
  echo "Timed out waiting for the Owner account to be disabled in MongoDB." >&2
  return 1
}

prepare_local_pv() {
  local pv="$1"
  local component="$2"
  local expected_pvc="$3"
  : "${TC_STORAGE_BASE_PATH:?TC_STORAGE_BASE_PATH is required}"
  : "${TC_STORAGE_NODE_NAME:?TC_STORAGE_NODE_NAME is required}"
  : "${TC_STORAGE_CLASS:?TC_STORAGE_CLASS is required}"
  : "${TC_STORAGE_SIZE:?TC_STORAGE_SIZE is required}"
  : "${expected_pvc:?Expected PVC name is required}"

  local path="${TC_STORAGE_BASE_PATH%/}/${pv}"
  docker exec "${TC_STORAGE_NODE_NAME}" mkdir -p "$path"

  # Static-local shard/config PVs used to share only a broad label selector.
  # Kubernetes was therefore free to bind any matching PV to any shard member.
  # Pre-binding each PV to the exact PVC name makes the Terraform storage
  # ordinal deterministic and makes later shard cleanup safe.
  cat <<EOF | "${K[@]}" apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${pv}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.deployment: ${TC_DEPLOYMENT}
    dbaas.sharded-cluster: ${TC_DEPLOYMENT}
    dbaas.component: ${component}
    dbaas.pvc-name: ${expected_pvc}
spec:
  claimRef:
    namespace: ${TC_NAMESPACE}
    name: ${expected_pvc}
  accessModes:
    - ReadWriteOnce
  capacity:
    storage: ${TC_STORAGE_SIZE}
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ${TC_STORAGE_CLASS}
  volumeMode: Filesystem
  local:
    path: ${path}
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ${TC_STORAGE_NODE_NAME}
EOF
}

pvc_users() {
  local namespace="$1"
  local claim="$2"

  "${K[@]}" -n "$namespace" get pods -o json 2>/dev/null | python3 - "$claim" <<'PY'
import json
import sys

claim = sys.argv[1]
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

users = []
for pod in data.get("items", []):
    for volume in pod.get("spec", {}).get("volumes", []) or []:
        pvc = volume.get("persistentVolumeClaim") or {}
        if pvc.get("claimName") == claim:
            users.append(pod.get("metadata", {}).get("name", "<unknown>"))
            break

print(" ".join(users))
PY
}

cleanup_local_pv() {
  local pv="$1"
  local expected_pvc="${2:-}"
  : "${TC_STORAGE_BASE_PATH:?TC_STORAGE_BASE_PATH is required}"
  : "${TC_STORAGE_NODE_NAME:?TC_STORAGE_NODE_NAME is required}"

  # Do not let a kubectl --wait hang hold Terraform forever. Three minutes is
  # intentionally much shorter than the harness timeout and is plenty of time
  # for an unused local PVC/PV to disappear.
  local cleanup_timeout="${TC_STORAGE_CLEANUP_TIMEOUT:-180s}"
  local claim claim_ns target_claim pvc_volume users

  claim=$("${K[@]}" get pv "$pv" -o jsonpath='{.spec.claimRef.name}' 2>/dev/null || true)
  claim_ns=$("${K[@]}" get pv "$pv" -o jsonpath='{.spec.claimRef.namespace}' 2>/dev/null || true)
  claim_ns="${claim_ns:-${TC_NAMESPACE}}"

  if [[ -n "$expected_pvc" && -n "$claim" && "$claim" != "$expected_pvc" ]]; then
    echo "Refusing storage cleanup: PV '$pv' is bound to PVC '$claim', but Terraform expected '$expected_pvc'." >&2
    echo "No PVC/PV was deleted. This indicates legacy or drifted nondeterministic binding." >&2
    return 42
  fi

  target_claim="${expected_pvc:-$claim}"

  if [[ -n "$target_claim" ]] && "${K[@]}" -n "$claim_ns" get pvc "$target_claim" >/dev/null 2>&1; then
    pvc_volume=$("${K[@]}" -n "$claim_ns" get pvc "$target_claim" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
    if [[ -n "$pvc_volume" && "$pvc_volume" != "$pv" ]]; then
      echo "Refusing storage cleanup: PVC '$target_claim' is bound to PV '$pvc_volume', not '$pv'." >&2
      echo "No PVC/PV was deleted." >&2
      return 42
    fi

    users=$(pvc_users "$claim_ns" "$target_claim")
    if [[ -n "$users" ]]; then
      echo "Refusing storage cleanup: PVC '$target_claim' is still used by pod(s): $users" >&2
      echo "No PVC/PV was deleted. The removed shard must be fully absent before storage cleanup." >&2
      return 42
    fi

    "${K[@]}" -n "$claim_ns" delete pvc "$target_claim" --ignore-not-found=true --wait=false >/dev/null
    if "${K[@]}" -n "$claim_ns" get pvc "$target_claim" >/dev/null 2>&1; then
      if ! "${K[@]}" -n "$claim_ns" wait --for=delete "pvc/$target_claim" --timeout="$cleanup_timeout" >/dev/null 2>&1; then
        echo "Timed out waiting $cleanup_timeout for PVC '$target_claim' to be deleted." >&2
        "${K[@]}" -n "$claim_ns" get pvc "$target_claim" -o wide >&2 || true
        return 42
      fi
    fi
  fi

  "${K[@]}" delete pv "$pv" --ignore-not-found=true --wait=false >/dev/null
  if "${K[@]}" get pv "$pv" >/dev/null 2>&1; then
    if ! "${K[@]}" wait --for=delete "pv/$pv" --timeout="$cleanup_timeout" >/dev/null 2>&1; then
      echo "Timed out waiting $cleanup_timeout for PV '$pv' to be deleted." >&2
      "${K[@]}" get pv "$pv" -o wide >&2 || true
      return 42
    fi
  fi

  docker exec "${TC_STORAGE_NODE_NAME}" rm -rf "${TC_STORAGE_BASE_PATH%/}/${pv}"
}

case "${TC_ACTION}" in
  prepare_replica_set_storage)
    : "${TC_STORAGE_BASE_PATH:?TC_STORAGE_BASE_PATH is required}"
    : "${TC_STORAGE_NODE_NAME:?TC_STORAGE_NODE_NAME is required}"
    : "${TC_STORAGE_CLASS:?TC_STORAGE_CLASS is required}"
    : "${TC_STORAGE_SIZE:?TC_STORAGE_SIZE is required}"
    : "${TC_MEMBERS:?TC_MEMBERS is required}"

    for ((i=0; i<TC_MEMBERS; i++)); do
      pv="${TC_DEPLOYMENT}-${i}"
      path="${TC_STORAGE_BASE_PATH%/}/${pv}"
      docker exec "${TC_STORAGE_NODE_NAME}" mkdir -p "$path"

      cat <<EOF | "${K[@]}" apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${pv}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.replica-set: ${TC_DEPLOYMENT}
    dbaas.member: "${i}"
spec:
  accessModes:
    - ReadWriteOnce
  capacity:
    storage: ${TC_STORAGE_SIZE}
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ${TC_STORAGE_CLASS}
  volumeMode: Filesystem
  local:
    path: ${path}
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ${TC_STORAGE_NODE_NAME}
EOF
    done
    ;;

  cleanup_replica_set_storage)
    : "${TC_STORAGE_BASE_PATH:?TC_STORAGE_BASE_PATH is required}"
    : "${TC_STORAGE_NODE_NAME:?TC_STORAGE_NODE_NAME is required}"
    : "${TC_MEMBERS:?TC_MEMBERS is required}"

    for ((i=0; i<TC_MEMBERS; i++)); do
      "${K[@]}" -n "${TC_NAMESPACE}" delete pvc "data-${TC_DEPLOYMENT}-${i}" --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
    done
    for ((i=0; i<TC_MEMBERS; i++)); do
      pv="${TC_DEPLOYMENT}-${i}"
      "${K[@]}" delete pv "$pv" --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
      docker exec "${TC_STORAGE_NODE_NAME}" rm -rf "${TC_STORAGE_BASE_PATH%/}/${pv}" || true
    done
    ;;

  prepare_sharded_cluster_volume)
    : "${TC_PV_NAME:?TC_PV_NAME is required}"
    : "${TC_COMPONENT:?TC_COMPONENT is required}"
    if [[ "${TC_COMPONENT}" != "shard" && "${TC_COMPONENT}" != "config" ]]; then
      echo "TC_COMPONENT must be shard or config." >&2
      exit 2
    fi
    : "${TC_PVC_NAME:?TC_PVC_NAME is required}"
    prepare_local_pv "${TC_PV_NAME}" "${TC_COMPONENT}" "${TC_PVC_NAME}"
    ;;

  cleanup_sharded_cluster_volume)
    : "${TC_PV_NAME:?TC_PV_NAME is required}"
    cleanup_local_pv "${TC_PV_NAME}" "${TC_PVC_NAME:-}"
    ;;

  acquire_deployment_lock)
    : "${TC_LOCK_CATEGORY:?TC_LOCK_CATEGORY is required}"
    : "${TC_LOCK_ACTION:?TC_LOCK_ACTION is required}"
    : "${TC_OPERATION_ID:?TC_OPERATION_ID is required}"
    : "${TC_START_SHARDS:?TC_START_SHARDS is required}"
    : "${TC_TARGET_SHARDS:?TC_TARGET_SHARDS is required}"

    lock="tc-deployment-lock-${TC_DEPLOYMENT}"
    started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    cat <<EOF | "${K[@]}" -n "${TC_NAMESPACE}" create -f - >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${lock}
  namespace: ${TC_NAMESPACE}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.deployment: ${TC_DEPLOYMENT}
    dbaas.lock: deployment
data:
  operation_id: "${TC_OPERATION_ID}"
  category: "${TC_LOCK_CATEGORY}"
  action: "${TC_LOCK_ACTION}"
  database: "${TC_DATABASE:-}"
  start_shards: "${TC_START_SHARDS}"
  target_shards: "${TC_TARGET_SHARDS}"
  started_at: "${started_at}"
EOF
    ;;

  release_deployment_lock)
    : "${TC_OPERATION_ID:?TC_OPERATION_ID is required}"
    lock="tc-deployment-lock-${TC_DEPLOYMENT}"
    current_id=$("${K[@]}" -n "${TC_NAMESPACE}" get configmap "${lock}" -o jsonpath='{.data.operation_id}' 2>/dev/null || true)

    if [[ -z "${current_id}" ]]; then
      echo "Deployment lock is already absent."
      exit 0
    fi

    if [[ "${current_id}" != "${TC_OPERATION_ID}" ]]; then
      echo "Deployment lock operation ID does not match. Refusing to release another operation's lock." >&2
      exit 42
    fi

    "${K[@]}" -n "${TC_NAMESPACE}" delete configmap "${lock}" --wait=true >/dev/null
    ;;

  create_database)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    : "${TC_PLACEHOLDER_COLLECTION:?TC_PLACEHOLDER_COLLECTION is required}"
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    db_json=$(json_string "${TC_DATABASE}")
    placeholder_json=$(json_string "${TC_PLACEHOLDER_COLLECTION}")
    js="const d=${db_json},p=${placeholder_json},t=db.getSiblingDB(d),c=t.getCollectionNames();if(!c.includes(p))t.createCollection(p);print('TC_RESULT=OK');"
    export TC_JS_JSON
    TC_JS_JSON=$(json_string "$js")
    run_mongo_job
    ;;

  delete_database)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    db_json=$(json_string "${TC_DATABASE}")
    js="const d=${db_json};const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name);if(!n.includes(d)){print('TC_RESULT=ALREADY_ABSENT');quit(0);}const r=db.getSiblingDB(d).dropDatabase();if(!r||r.ok!==1)quit(43);print('TC_RESULT=DELETED');"
    export TC_JS_JSON
    TC_JS_JSON=$(json_string "$js")
    run_mongo_job
    ;;

  validate_deployment_empty)
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    js="const p=new Set(['admin','config','local']);const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name).filter(x=>!p.has(x)).sort();if(n.length){print('TC_BLOCKED='+JSON.stringify(n));quit(42);}print('TC_RESULT=EMPTY');"
    export TC_JS_JSON
    TC_JS_JSON=$(json_string "$js")
    run_mongo_job
    ;;

  verify_database_accounts)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    verify_account owner
    verify_account readwrite
    verify_account read
    ;;

  verify_database_accounts_owner_disabled)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    verify_account readwrite
    verify_account read
    verify_owner_absent
    ;;

  verify_database_users_absent)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    verify_users_absent
    ;;

  verify_controller_admin)
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    verify_controller_admin
    ;;

  *)
    echo "Unsupported TC_ACTION: ${TC_ACTION}" >&2
    exit 2
    ;;
esac

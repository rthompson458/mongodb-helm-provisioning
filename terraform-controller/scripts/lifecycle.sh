#!/usr/bin/env bash
set -euo pipefail

: "${TC_ACTION:?TC_ACTION is required}"
: "${TC_REPLICA_SET:?TC_REPLICA_SET is required}"
: "${TC_NAMESPACE:?TC_NAMESPACE is required}"
: "${TC_KUBECONFIG:?TC_KUBECONFIG is required}"

K=(kubectl --kubeconfig "${TC_KUBECONFIG}")
if [[ -n "${TC_KUBE_CONTEXT:-}" ]]; then
  K+=(--context "${TC_KUBE_CONTEXT}")
fi

run_mongo_job() {
  local connection_secret="${1:-tc-${TC_REPLICA_SET}-admin-connection}"
  local job="tc-runtime-${TC_REPLICA_SET:0:12}-$(date +%s)-${RANDOM}"
  local manifest

  manifest=$(cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: ${TC_NAMESPACE}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.replica-set: ${TC_REPLICA_SET}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 120
  template:
    metadata:
      labels:
        app.kubernetes.io/managed-by: terraformController
        dbaas.replica-set: ${TC_REPLICA_SET}
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
            - 'mongosh "$MONGODB_URI" --quiet --eval "$TC_JS"'
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
  local digest
  digest=$(python3 -c 'import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:6])' "${TC_REPLICA_SET}/${db_key}/${account}")
  printf 'tc-%s-%s-%s-%s' "${TC_REPLICA_SET:0:8}" "${db_key:0:10}" "${account}" "${digest}"
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

verify_users_absent() {
  local owner="${TC_DATABASE}_owner"
  local readwrite="${TC_DATABASE}_readWrite"
  local read="${TC_DATABASE}_read"
  local owner_json readwrite_json read_json
  owner_json=$(json_string "$owner")
  readwrite_json=$(json_string "$readwrite")
  read_json=$(json_string "$read")
  local js="const a=db.getSiblingDB('admin');const n=[${owner_json},${readwrite_json},${read_json}];const f=n.filter(x=>a.getUser(x)!==null);if(f.length){print('TC_BLOCKED='+JSON.stringify(f));quit(42);}print('TC_RESULT=USERS_ABSENT');"
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
  local js="const a=db.getSiblingDB('admin');const u=a.getUser(${owner_json});if(u!==null){print('TC_BLOCKED=OWNER_STILL_EXISTS');quit(42);}print('TC_RESULT=OWNER_ABSENT');"
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

case "${TC_ACTION}" in
  prepare_replica_set_storage)
    : "${TC_STORAGE_BASE_PATH:?TC_STORAGE_BASE_PATH is required}"
    : "${TC_STORAGE_NODE_NAME:?TC_STORAGE_NODE_NAME is required}"
    : "${TC_STORAGE_CLASS:?TC_STORAGE_CLASS is required}"
    : "${TC_STORAGE_SIZE:?TC_STORAGE_SIZE is required}"
    : "${TC_MEMBERS:?TC_MEMBERS is required}"

    for ((i=0; i<TC_MEMBERS; i++)); do
      pv="${TC_REPLICA_SET}-${i}"
      path="${TC_STORAGE_BASE_PATH%/}/${pv}"
      docker exec "${TC_STORAGE_NODE_NAME}" mkdir -p "$path"

      cat <<EOF | "${K[@]}" apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${pv}
  labels:
    app.kubernetes.io/managed-by: terraformController
    dbaas.replica-set: ${TC_REPLICA_SET}
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
      "${K[@]}" -n "${TC_NAMESPACE}" delete pvc "data-${TC_REPLICA_SET}-${i}" --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
    done
    for ((i=0; i<TC_MEMBERS; i++)); do
      pv="${TC_REPLICA_SET}-${i}"
      "${K[@]}" delete pv "$pv" --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
      docker exec "${TC_STORAGE_NODE_NAME}" rm -rf "${TC_STORAGE_BASE_PATH%/}/${pv}" || true
    done
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

  validate_replica_set_empty)
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

  *)
    echo "Unsupported TC_ACTION: ${TC_ACTION}" >&2
    exit 2
    ;;
esac

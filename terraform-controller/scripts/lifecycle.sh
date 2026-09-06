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
  local javascript="$1"
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
                  name: tc-${TC_REPLICA_SET}-admin-connection
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

  set +e
  "${K[@]}" -n "${TC_NAMESPACE}" wait --for=condition=complete "job/${job}" --timeout=300s >/dev/null 2>&1
  local rc=$?
  set -e

  local logs
  logs=$("${K[@]}" -n "${TC_NAMESPACE}" logs "job/${job}" 2>&1 || true)
  "${K[@]}" -n "${TC_NAMESPACE}" delete job "${job}" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true

  printf '%s\n' "$logs"
  if [[ $rc -ne 0 ]]; then
    return 1
  fi
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
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
    run_mongo_job "$js"
    ;;

  delete_database)
    : "${TC_DATABASE:?TC_DATABASE is required}"
    : "${TC_PLACEHOLDER_COLLECTION:?TC_PLACEHOLDER_COLLECTION is required}"
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    db_json=$(json_string "${TC_DATABASE}")
    placeholder_json=$(json_string "${TC_PLACEHOLDER_COLLECTION}")
    js="const d=${db_json},p=${placeholder_json};const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name);if(!n.includes(d)){print('TC_RESULT=ALREADY_ABSENT');quit(0);}const t=db.getSiblingDB(d);const b=t.getCollectionInfos().map(x=>x.name).filter(x=>x!==p&&!x.startsWith('system.'));if(b.length){print('TC_BLOCKED='+JSON.stringify(b.sort()));quit(42);}const r=t.dropDatabase();if(!r||r.ok!==1)quit(43);print('TC_RESULT=DELETED');"
    export TC_JS_JSON
    TC_JS_JSON=$(json_string "$js")
    run_mongo_job "$js"
    ;;

  validate_replica_set_empty)
    : "${TC_MONGO_IMAGE:?TC_MONGO_IMAGE is required}"
    js="const p=new Set(['admin','config','local']);const n=db.adminCommand({listDatabases:1,nameOnly:true}).databases.map(x=>x.name).filter(x=>!p.has(x)).sort();if(n.length){print('TC_BLOCKED='+JSON.stringify(n));quit(42);}print('TC_RESULT=EMPTY');"
    export TC_JS_JSON
    TC_JS_JSON=$(json_string "$js")
    run_mongo_job "$js"
    ;;

  *)
    echo "Unsupported TC_ACTION: ${TC_ACTION}" >&2
    exit 2
    ;;
esac

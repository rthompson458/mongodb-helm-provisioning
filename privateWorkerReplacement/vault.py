"""Read privateWorkerReplacement desired-state metadata and credentials from Vault.

Vault is the durable metadata source used to reconstruct Terraform input.  The
controller does not directly write lifecycle state here; Terraform owns those
writes.  This client is intentionally read-focused.

Current hierarchy:
    mongodb/<Deployment>/_metadata
    mongodb/<Deployment>/<Database>/_metadata
    mongodb/<Deployment>/<Database>/<Username>

A legacy ReplicaSet hierarchy is still readable so older POC state can migrate
without being silently lost.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .common import ControllerError, DEPLOYMENT_TYPES, normalize_database, normalize_deployment
from .logging_component import log_event


class VaultClient:
    """Small read-only Vault KV v2 client used by controller orchestration."""
    def __init__(self, config: dict[str, Any]):
        """Capture Vault/config defaults and require the token environment variable."""
        self.address = config["vault_address"]
        self.mount = config["vault_mount"]
        self.base = config["vault_base_path"]
        self.default_storage_mode = config.get("storage_mode", "static-local")
        self.default_storage_base_path = config.get("storage_base_path", "")
        self.default_storage_node_name = config.get("storage_node_name", "")
        self.default_shards = int(config.get("default_shards", 3))
        self.default_members_per_shard = int(config.get("default_members_per_shard", 3))
        self.default_mongos = int(config.get("default_mongos", 2))
        self.default_config_servers = int(config.get("default_config_servers", 3))
        env_name = config["vault_token_env"]
        self.token = os.getenv(env_name, "")
        if not self.token:
            raise ControllerError(f"Vault token environment variable '{env_name}' is not set.")

    def _request(self, api_path: str, *, list_request: bool = False) -> dict[str, Any] | None:
        """Perform one authenticated Vault GET/LIST-style request."""
        url = f"{self.address}/v1/{urllib.parse.quote(api_path.strip('/'), safe='/')}"
        if list_request:
            url += "?list=true"
        request = urllib.request.Request(url, method="GET", headers={"X-Vault-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            body = exc.read().decode("utf-8", errors="replace")
            raise ControllerError(f"Vault returned HTTP {exc.code} for '{api_path}': {body}") from exc
        except urllib.error.URLError as exc:
            raise ControllerError(f"Cannot connect to Vault at {self.address}: {exc.reason}") from exc

    def list_keys(self, path: str) -> list[str]:
        response = self._request(f"{self.mount}/metadata/{path.strip('/')}", list_request=True)
        return list(response.get("data", {}).get("keys", [])) if response else []

    def read_secret(self, path: str) -> dict[str, Any] | None:
        response = self._request(f"{self.mount}/data/{path.strip('/')}")
        return response.get("data", {}).get("data", {}) if response else None

    def account_secret(
        self, deployment_display: str, db_display: str, username: str
    ) -> dict[str, Any] | None:
        return self.read_secret(f"{self.base}/{deployment_display}/{db_display}/{username}")

    def _new_database(self, dbm: dict[str, Any]) -> dict[str, Any]:
        """Convert Vault string metadata into the typed database state Terraform expects."""
        return {
            "display_name": str(dbm["display_name"]),
            "created_at": str(dbm["created_at"]),
            "owner_disabled": str(dbm["owner_disabled"]).lower() == "true",
            "owner_disabled_at": str(dbm.get("owner_disabled_at", "")),
            "rotation_version": int(dbm["rotation_version"]),
            "rotated_at": str(dbm["rotated_at"]),
        }

    def _new_deployment(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Convert one deployment metadata secret into Terraform desired state."""
        deployment_type = str(meta.get("deployment_type", "ReplicaSet"))
        if deployment_type not in DEPLOYMENT_TYPES:
            raise ControllerError(f"Unsupported deployment_type '{deployment_type}' in Vault metadata.")
        members = int(meta.get("members", meta.get("members_per_shard", 3)))
        return {
            "display_name": str(meta["display_name"]),
            "deployment_type": deployment_type,
            "created_at": str(meta["created_at"]),
            "members": members,
            "version": str(meta["version"]),
            "persistent": str(meta["persistent"]).lower() == "true",
            "storage_class": str(meta["storage_class"]),
            "storage_size": str(meta["storage_size"]),
            "storage_mode": str(meta.get("storage_mode", self.default_storage_mode)),
            "storage_base_path": str(meta.get("storage_base_path", self.default_storage_base_path)),
            "storage_node_name": str(meta.get("storage_node_name", self.default_storage_node_name)),
            "controller_password_version": int(meta.get("controller_password_version", 1)),
            "shard_count": int(meta.get("shard_count", self.default_shards if deployment_type == "ShardedCluster" else 0)),
            "storage_shard_count": int(meta.get("storage_shard_count", meta.get("shard_count", self.default_shards if deployment_type == "ShardedCluster" else 0))),
            "members_per_shard": int(meta.get("members_per_shard", members if deployment_type == "ShardedCluster" else 0)),
            "mongos_count": int(meta.get("mongos_count", self.default_mongos if deployment_type == "ShardedCluster" else 0)),
            "config_server_count": int(meta.get("config_server_count", self.default_config_servers if deployment_type == "ShardedCluster" else 0)),
            "databases": {},
        }

    def _load_current_layout(self) -> dict[str, dict[str, Any]]:
        """Walk the current Deployment/Database Vault hierarchy."""
        inventory: dict[str, dict[str, Any]] = {}
        for item in self.list_keys(self.base):
            if not item.endswith("/") or item == "replica-sets/":
                continue
            display = item[:-1]
            try:
                key, _ = normalize_deployment(display)
            except ControllerError:
                continue

            meta = self.read_secret(f"{self.base}/{display}/_metadata")
            if not meta:
                continue
            try:
                deployment = self._new_deployment(meta)
            except (KeyError, TypeError, ValueError) as exc:
                raise ControllerError(f"Invalid deployment metadata for '{display}'.") from exc

            for db_item in self.list_keys(f"{self.base}/{display}"):
                if not db_item.endswith("/") or db_item == "_internal/":
                    continue
                db_display = db_item[:-1]
                try:
                    db_key, _ = normalize_database(db_display)
                except ControllerError:
                    continue
                dbm = self.read_secret(f"{self.base}/{display}/{db_display}/_metadata")
                if not dbm:
                    continue
                try:
                    deployment["databases"][db_key] = self._new_database(dbm)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerError(
                        f"Invalid database metadata for '{display}/{db_display}'."
                    ) from exc
            inventory[key] = deployment
        return inventory

    def _load_legacy_layout(self) -> dict[str, dict[str, Any]]:
        """Read the pre-redesign mongodb/replica-sets/... layout for safe migration.

        Legacy data is read only.  load_inventory() merges it underneath the new
        layout so a current record always wins when both describe the same key.
        """
        inventory: dict[str, dict[str, Any]] = {}
        root = f"{self.base}/replica-sets"
        for item in self.list_keys(root):
            if not item.endswith("/"):
                continue
            key = item[:-1]
            meta = self.read_secret(f"{root}/{key}/_metadata")
            if not meta:
                continue
            legacy_meta = dict(meta)
            legacy_meta["deployment_type"] = "ReplicaSet"
            try:
                deployment = self._new_deployment(legacy_meta)
            except (KeyError, TypeError, ValueError) as exc:
                raise ControllerError(f"Invalid legacy ReplicaSet metadata for '{key}'.") from exc

            db_root = f"{root}/{key}/databases"
            for db_item in self.list_keys(db_root):
                if not db_item.endswith("/"):
                    continue
                db_key = db_item[:-1]
                dbm = self.read_secret(f"{db_root}/{db_key}/_metadata")
                if not dbm:
                    continue
                try:
                    deployment["databases"][db_key] = self._new_database(dbm)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerError(
                        f"Invalid legacy database metadata for '{key}/{db_key}'."
                    ) from exc
            inventory[key] = deployment
        return inventory

    def load_inventory(self) -> dict[str, dict[str, Any]]:
        """Reconstruct Terraform desired state from Vault metadata.

        Current human-facing credential layout:
          mongodb/<Deployment>/<Database>/<Database>_owner
          mongodb/<Deployment>/<Database>/<Database>_readWrite
          mongodb/<Deployment>/<Database>/<Database>_read

        Deployment is either a ReplicaSet or a ShardedCluster.

        The old mongodb/replica-sets/... layout is read as a migration fallback.
        Current-layout records take precedence if both exist.
        """
        current = self._load_current_layout()
        legacy = self._load_legacy_layout()
        for key, value in legacy.items():
            current.setdefault(key, value)
        log_event(
            "vault.inventory.loaded",
            deployments=len(current),
            databases=sum(len(x["databases"]) for x in current.values()),
        )
        return current

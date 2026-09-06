from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .common import ControllerError, normalize_database, normalize_replica_set


class VaultClient:
    def __init__(self, config: dict[str, Any]):
        self.address = config["vault_address"]
        self.mount = config["vault_mount"]
        self.base = config["vault_base_path"]
        env_name = config["vault_token_env"]
        self.token = os.getenv(env_name, "")
        if not self.token:
            raise ControllerError(f"Vault token environment variable '{env_name}' is not set.")

    def _request(self, api_path: str, *, list_request: bool = False) -> dict[str, Any] | None:
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

    def account_secret(self, rs_display: str, db_display: str, username: str) -> dict[str, Any] | None:
        return self.read_secret(f"{self.base}/{rs_display}/{db_display}/{username}")

    def _new_database(self, dbm: dict[str, Any]) -> dict[str, Any]:
        return {
            "display_name": str(dbm["display_name"]),
            "created_at": str(dbm["created_at"]),
            "owner_disabled": str(dbm["owner_disabled"]).lower() == "true",
            "owner_disabled_at": str(dbm.get("owner_disabled_at", "")),
            "rotation_version": int(dbm["rotation_version"]),
            "rotated_at": str(dbm["rotated_at"]),
        }

    def _new_replica_set(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "display_name": str(meta["display_name"]),
            "created_at": str(meta["created_at"]),
            "members": int(meta["members"]),
            "version": str(meta["version"]),
            "persistent": str(meta["persistent"]).lower() == "true",
            "storage_class": str(meta["storage_class"]),
            "storage_size": str(meta["storage_size"]),
            "controller_password_version": int(meta.get("controller_password_version", 1)),
            "databases": {},
        }

    def _load_current_layout(self) -> dict[str, dict[str, Any]]:
        inventory: dict[str, dict[str, Any]] = {}
        for rs_item in self.list_keys(self.base):
            if not rs_item.endswith("/") or rs_item == "replica-sets/":
                continue
            rs_display = rs_item[:-1]
            try:
                rs_key, _ = normalize_replica_set(rs_display)
            except ControllerError:
                continue
            meta = self.read_secret(f"{self.base}/{rs_display}/_metadata")
            if not meta:
                continue
            try:
                rs = self._new_replica_set(meta)
            except (KeyError, TypeError, ValueError) as exc:
                raise ControllerError(f"Invalid ReplicaSet metadata for '{rs_display}'.") from exc

            for db_item in self.list_keys(f"{self.base}/{rs_display}"):
                if not db_item.endswith("/") or db_item == "_internal/":
                    continue
                db_display = db_item[:-1]
                try:
                    db_key, _ = normalize_database(db_display)
                except ControllerError:
                    continue
                dbm = self.read_secret(f"{self.base}/{rs_display}/{db_display}/_metadata")
                if not dbm:
                    continue
                try:
                    rs["databases"][db_key] = self._new_database(dbm)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerError(f"Invalid database metadata for '{rs_display}/{db_display}'.") from exc
            inventory[rs_key] = rs
        return inventory

    def _load_legacy_layout(self) -> dict[str, dict[str, Any]]:
        """Read the pre-redesign mongodb/replica-sets/... layout for safe migration."""
        inventory: dict[str, dict[str, Any]] = {}
        root = f"{self.base}/replica-sets"
        for rs_item in self.list_keys(root):
            if not rs_item.endswith("/"):
                continue
            rs_key = rs_item[:-1]
            meta = self.read_secret(f"{root}/{rs_key}/_metadata")
            if not meta:
                continue
            try:
                rs = self._new_replica_set(meta)
            except (KeyError, TypeError, ValueError) as exc:
                raise ControllerError(f"Invalid legacy ReplicaSet metadata for '{rs_key}'.") from exc

            db_root = f"{root}/{rs_key}/databases"
            for db_item in self.list_keys(db_root):
                if not db_item.endswith("/"):
                    continue
                db_key = db_item[:-1]
                dbm = self.read_secret(f"{db_root}/{db_key}/_metadata")
                if not dbm:
                    continue
                try:
                    rs["databases"][db_key] = self._new_database(dbm)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerError(f"Invalid legacy database metadata for '{rs_key}/{db_key}'.") from exc
            inventory[rs_key] = rs
        return inventory

    def load_inventory(self) -> dict[str, dict[str, Any]]:
        """Reconstruct Terraform desired state from Vault metadata.

        Current human-facing credential layout:
          mongodb/<ReplicaSet>/<Database>/<Database>_owner
          mongodb/<ReplicaSet>/<Database>/<Database>_readWrite
          mongodb/<ReplicaSet>/<Database>/<Database>_read

        The old mongodb/replica-sets/... layout is read as a migration fallback.
        Current-layout records take precedence if both exist.
        """
        current = self._load_current_layout()
        legacy = self._load_legacy_layout()
        for key, value in legacy.items():
            current.setdefault(key, value)
        return current

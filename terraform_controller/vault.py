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

    def load_inventory(self) -> dict[str, dict[str, Any]]:
        """Reconstruct Terraform desired state from Vault metadata.

        Human-facing credential layout:
          mongodb/<ReplicaSet>/<Database>/<Database>_owner
          mongodb/<ReplicaSet>/<Database>/<Database>_readWrite
          mongodb/<ReplicaSet>/<Database>/<Database>_read

        _metadata entries at the ReplicaSet and database levels hold lifecycle state.
        """
        inventory: dict[str, dict[str, Any]] = {}

        for rs_item in self.list_keys(self.base):
            if not rs_item.endswith("/"):
                continue
            rs_display = rs_item[:-1]
            rs_key, _ = normalize_replica_set(rs_display)
            meta = self.read_secret(f"{self.base}/{rs_display}/_metadata")
            if not meta:
                continue
            try:
                rs = {
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
            except (KeyError, TypeError, ValueError) as exc:
                raise ControllerError(f"Invalid ReplicaSet metadata for '{rs_display}'.") from exc

            for db_item in self.list_keys(f"{self.base}/{rs_display}"):
                if not db_item.endswith("/"):
                    continue
                db_display = db_item[:-1]
                db_key, _ = normalize_database(db_display)
                dbm = self.read_secret(f"{self.base}/{rs_display}/{db_display}/_metadata")
                if not dbm:
                    continue
                try:
                    rs["databases"][db_key] = {
                        "display_name": str(dbm["display_name"]),
                        "created_at": str(dbm["created_at"]),
                        "owner_disabled": str(dbm["owner_disabled"]).lower() == "true",
                        "owner_disabled_at": str(dbm.get("owner_disabled_at", "")),
                        "rotation_version": int(dbm["rotation_version"]),
                        "rotated_at": str(dbm["rotated_at"]),
                    }
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerError(f"Invalid database metadata for '{rs_display}/{db_display}'.") from exc
            inventory[rs_key] = rs

        return inventory

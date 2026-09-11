"""Shared Vault credential-path and browser-URL presentation helpers.

Database lifecycle code and read-only account-status code both need to show the
same three Vault credential locations. Keeping that presentation logic here
prevents the status module from importing private helpers from databases.py and
keeps lifecycle orchestration focused on lifecycle work.

This module is read-only presentation logic. It never reads or writes a Vault
secret value.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


def vault_paths(
    config: dict[str, Any],
    deployment: dict[str, Any],
    database: dict[str, Any],
) -> list[str]:
    """Return the Owner, ReadWrite, and Read Vault paths for one database."""

    base = config["vault_base_path"].strip("/")
    root = f"{base}/{deployment['display_name']}/{database['display_name']}"
    name = database["display_name"]
    return [
        f"{root}/{name}_owner",
        f"{root}/{name}_readWrite",
        f"{root}/{name}_read",
    ]


def vault_browser_url(config: dict[str, Any], secret_path: str) -> str:
    """Build the Vault UI URL for one logical KV secret path.

    Vault's UI route identifies the KV mount separately from the path inside
    that mount. Each path segment is URL-encoded so valid database names remain
    safe when the URL is pasted into a browser.
    """

    base = config["vault_address"].rstrip("/")
    mount = quote(config["vault_mount"].strip("/"), safe="")
    encoded_path = "/".join(
        quote(part, safe="")
        for part in secret_path.strip("/").split("/")
    )
    return f"{base}/ui/vault/secrets/{mount}/show/{encoded_path}"


def print_vault_credentials(
    config: dict[str, Any],
    deployment: dict[str, Any],
    database: dict[str, Any],
) -> None:
    """Print logical Vault paths and browser-ready URLs for all three accounts."""

    print("Vault credentials:")
    for path in vault_paths(config, deployment, database):
        print(f"  Path: {path}")
        print(f"  URL:  {vault_browser_url(config, path)}")

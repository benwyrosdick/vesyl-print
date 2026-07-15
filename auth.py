"""Device credentials: load/save with mode 0600. Never log device_token."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Credentials:
    node_id: str
    device_token: str
    name: str | None = None
    hostname: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    organization_slug: str | None = None
    warehouse_id: str | None = None
    warehouse_name: str | None = None
    warehouse_code: str | None = None
    # Full list from claim/whoami when the node serves multiple warehouses.
    # Each item: {"id", "name", "code"} (any key may be missing).
    warehouses: list[dict[str, str | None]] | None = None

    def public_dict(self) -> dict[str, Any]:
        """Safe for status/CLI — never includes device_token."""
        return {
            "node_id": self.node_id,
            "name": self.name,
            "hostname": self.hostname,
            "organization_name": self.organization_name,
            "warehouse_name": self.warehouse_name,
            "warehouse_code": self.warehouse_code,
            "warehouses": self.warehouses,
            "warehouse_label": self.warehouse_label(),
        }

    def warehouse_label(self) -> str:
        """LCD / status text for warehouse(s).

        - Multiple warehouses with codes → comma-separated codes (e.g. \"DFD, MAIN\")
        - Otherwise prefer the primary name, then code.
        """
        items = self.warehouses or []
        codes = [
            str(w["code"]).strip()
            for w in items
            if w.get("code") and str(w["code"]).strip()
        ]
        if len(items) > 1 and len(codes) > 1:
            return ", ".join(codes)
        if len(items) > 1 and codes:
            # Multiple warehouses but only some have codes — still prefer codes.
            return ", ".join(codes)
        if self.warehouse_name:
            return self.warehouse_name
        if self.warehouse_code:
            return self.warehouse_code
        if items:
            w0 = items[0]
            return (w0.get("name") or w0.get("code") or "—") or "—"
        return "—"


def _nested_name(obj: Any, *keys: str) -> str | None:
    if not isinstance(obj, dict):
        return None
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    return str(cur)


def _parse_warehouses(data: dict[str, Any]) -> list[dict[str, str | None]]:
    """Accept warehouses[] and/or legacy single warehouse object."""
    out: list[dict[str, str | None]] = []
    raw_list = data.get("warehouses")
    if isinstance(raw_list, list):
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "id": str(item["id"]) if item.get("id") is not None else None,
                    "name": str(item["name"]) if item.get("name") is not None else None,
                    "code": str(item["code"]) if item.get("code") is not None else None,
                }
            )
    if out:
        return out
    wh = data.get("warehouse")
    if isinstance(wh, dict) and (wh.get("id") or wh.get("name") or wh.get("code")):
        return [
            {
                "id": str(wh["id"]) if wh.get("id") is not None else None,
                "name": str(wh["name"]) if wh.get("name") is not None else None,
                "code": str(wh["code"]) if wh.get("code") is not None else None,
            }
        ]
    return []


def credentials_from_pair_response(data: dict[str, Any]) -> Credentials:
    """Parse claim/enroll/whoami JSON into Credentials.

    claim/enroll include device_token; whoami does not (caller keeps existing).

    Warehouse payload (either form):
      "warehouse":  { "id", "name", "code" }
      "warehouses": [ { "id", "name", "code" }, ... ]
    """
    node_id = data.get("node_id") or data.get("id")
    if not node_id:
        raise ValueError("pair response missing node_id")
    token = data.get("device_token") or ""
    org = data.get("organization") if isinstance(data.get("organization"), dict) else {}
    warehouses = _parse_warehouses(data)
    primary = warehouses[0] if warehouses else {}
    return Credentials(
        node_id=str(node_id),
        device_token=str(token) if token else "",
        name=_nested_name(data, "name"),
        hostname=_nested_name(data, "hostname"),
        organization_id=_nested_name(org, "id"),
        organization_name=_nested_name(org, "name"),
        organization_slug=_nested_name(org, "slug"),
        warehouse_id=primary.get("id"),
        warehouse_name=primary.get("name"),
        warehouse_code=primary.get("code"),
        warehouses=warehouses or None,
    )


def merge_whoami(creds: Credentials, data: dict[str, Any]) -> Credentials:
    """Update public fields from whoami without clearing device_token."""
    updated = credentials_from_pair_response({**data, "device_token": creds.device_token})
    if not updated.device_token:
        updated.device_token = creds.device_token
    return updated


def load_credentials(path: Path) -> Credentials | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    node_id = data.get("node_id")
    token = data.get("device_token")
    if not node_id or not token:
        return None
    warehouses = data.get("warehouses")
    if not isinstance(warehouses, list):
        warehouses = None
    else:
        warehouses = [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "code": w.get("code"),
            }
            for w in warehouses
            if isinstance(w, dict)
        ] or None
    return Credentials(
        node_id=str(node_id),
        device_token=str(token),
        name=data.get("name"),
        hostname=data.get("hostname"),
        organization_id=data.get("organization_id"),
        organization_name=data.get("organization_name"),
        organization_slug=data.get("organization_slug"),
        warehouse_id=data.get("warehouse_id"),
        warehouse_name=data.get("warehouse_name"),
        warehouse_code=data.get("warehouse_code"),
        warehouses=warehouses,
    )


def save_credentials(path: Path, creds: Credentials) -> None:
    """Atomically write credentials.json with mode 0600. Never logs token."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(creds)
    raw = json.dumps(payload, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def clear_credentials(path: Path) -> bool:
    """Delete credentials file. Returns True if a file was removed."""
    path = Path(path)
    if not path.is_file():
        return False
    path.unlink()
    return True


def credentials_mode(path: Path) -> int | None:
    """Return file mode bits (e.g. 0o600) or None if missing."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None

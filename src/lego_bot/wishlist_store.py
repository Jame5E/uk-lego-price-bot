from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_wishlist_entries(path: str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON array")
    return data


def save_wishlist_entries(path: str, entries: list[dict[str, Any]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def upsert_wishlist_entry(path: str, entry: dict[str, Any]) -> None:
    entries = load_wishlist_entries(path)
    set_num = str(entry.get("set_number") or "").strip()
    name = str(entry.get("name") or "").strip()

    for idx, existing in enumerate(entries):
        if set_num and str(existing.get("set_number") or "").strip() == set_num:
            entries[idx] = {**existing, **entry}
            save_wishlist_entries(path, entries)
            return
        if not set_num and str(existing.get("name") or "").strip().lower() == name.lower():
            entries[idx] = {**existing, **entry}
            save_wishlist_entries(path, entries)
            return

    entries.append(entry)
    save_wishlist_entries(path, entries)


def remove_wishlist_entry(path: str, set_number: str) -> bool:
    entries = load_wishlist_entries(path)
    clean = set_number.strip().split("-")[0]
    filtered = [
        e for e in entries
        if str(e.get("set_number") or "").strip().split("-")[0] != clean
    ]
    if len(filtered) == len(entries):
        return False
    save_wishlist_entries(path, filtered)
    return True

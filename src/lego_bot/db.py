from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class TrackedItem:
    id: int
    name: str
    set_number: str | None
    rrp_gbp: float
    target_price_gbp: float | None
    min_discount_pct: float
    url: str | None
    retailer: str
    is_retiring: bool
    last_alert_price_gbp: float | None
    last_alert_at: str | None
    created_at: str


@dataclass(frozen=True)
class PriceRecord:
    id: int
    item_id: int
    price_gbp: float
    shipping_gbp: float
    total_gbp: float
    retailer: str
    recorded_at: str


class PriceDatabase:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS tracked_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                set_number TEXT,
                rrp_gbp REAL NOT NULL,
                target_price_gbp REAL,
                min_discount_pct REAL NOT NULL DEFAULT 15.0,
                url TEXT,
                retailer TEXT NOT NULL DEFAULT 'other',
                is_retiring INTEGER NOT NULL DEFAULT 0,
                last_alert_price_gbp REAL,
                last_alert_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                price_gbp REAL NOT NULL,
                shipping_gbp REAL NOT NULL DEFAULT 0.0,
                total_gbp REAL NOT NULL,
                retailer TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (item_id) REFERENCES tracked_items(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_item_time
            ON price_history (item_id, recorded_at DESC);
            """
        )
        self._conn.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> TrackedItem:
        return TrackedItem(
            id=row["id"],
            name=row["name"],
            set_number=row["set_number"],
            rrp_gbp=row["rrp_gbp"],
            target_price_gbp=row["target_price_gbp"],
            min_discount_pct=row["min_discount_pct"],
            url=row["url"],
            retailer=row["retailer"],
            is_retiring=bool(row["is_retiring"]),
            last_alert_price_gbp=row["last_alert_price_gbp"],
            last_alert_at=row["last_alert_at"],
            created_at=row["created_at"],
        )

    def add_item(
        self,
        name: str,
        rrp_gbp: float,
        *,
        set_number: str | None = None,
        target_price_gbp: float | None = None,
        min_discount_pct: float = 15.0,
        url: str | None = None,
        retailer: str = "other",
        is_retiring: bool = False,
    ) -> TrackedItem:
        now = self._now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO tracked_items (
                name, set_number, rrp_gbp, target_price_gbp,
                min_discount_pct, url, retailer, is_retiring, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                set_number,
                round(rrp_gbp, 2),
                round(target_price_gbp, 2) if target_price_gbp is not None else None,
                round(min_discount_pct, 1),
                url,
                retailer,
                1 if is_retiring else 0,
                now,
            ),
        )
        self._conn.commit()
        item = self.get_item(cur.lastrowid)
        assert item is not None
        return item

    def get_item(self, item_id: int) -> TrackedItem | None:
        row = self._conn.execute(
            "SELECT * FROM tracked_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def list_items(self) -> list[TrackedItem]:
        rows = self._conn.execute(
            "SELECT * FROM tracked_items ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def remove_item(self, item_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM tracked_items WHERE id = ?",
            (item_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def record_price(
        self,
        item_id: int,
        price_gbp: float,
        shipping_gbp: float,
        total_gbp: float,
        retailer: str,
    ) -> PriceRecord:
        now = self._now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO price_history (
                item_id, price_gbp, shipping_gbp, total_gbp, retailer, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, round(price_gbp, 2), round(shipping_gbp, 2), round(total_gbp, 2), retailer, now),
        )
        self._conn.commit()
        return PriceRecord(
            id=cur.lastrowid,
            item_id=item_id,
            price_gbp=round(price_gbp, 2),
            shipping_gbp=round(shipping_gbp, 2),
            total_gbp=round(total_gbp, 2),
            retailer=retailer,
            recorded_at=now,
        )

    def get_price_history(self, item_id: int, limit: int = 30) -> list[PriceRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM price_history
            WHERE item_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (item_id, limit),
        ).fetchall()
        return [
            PriceRecord(
                id=r["id"],
                item_id=r["item_id"],
                price_gbp=r["price_gbp"],
                shipping_gbp=r["shipping_gbp"],
                total_gbp=r["total_gbp"],
                retailer=r["retailer"],
                recorded_at=r["recorded_at"],
            )
            for r in rows
        ]

    def should_alert(
        self,
        item: TrackedItem,
        total_gbp: float,
        cooldown_hours: int,
    ) -> bool:
        """Avoid repeat pings unless cooldown expired or price dropped further."""
        if item.last_alert_at is None or item.last_alert_price_gbp is None:
            return True

        # Always alert if the price dropped further by at least £1
        if total_gbp <= item.last_alert_price_gbp - 1.0:
            return True

        last_dt = datetime.fromisoformat(item.last_alert_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_dt >= timedelta(hours=cooldown_hours)

    def mark_alerted(self, item_id: int, total_gbp: float) -> None:
        self._conn.execute(
            """
            UPDATE tracked_items
            SET last_alert_price_gbp = ?, last_alert_at = ?
            WHERE id = ?
            """,
            (round(total_gbp, 2), self._now_iso(), item_id),
        )
        self._conn.commit()

from __future__ import annotations

import calendar
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class TrackedItem:
    id: int
    name: str
    set_number: str | None
    category: str | None
    rrp_gbp: float
    target_price_gbp: float | None
    min_discount_pct: float
    url: str | None
    retailer: str
    is_retiring: bool
    image_url: str | None
    notify_user_ids: tuple[str, ...]
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
                image_url TEXT,
                notify_user_ids TEXT NOT NULL DEFAULT '[]',
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

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_quota (
                provider TEXT NOT NULL,
                month_key TEXT NOT NULL,
                calls_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, month_key)
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_item_time
            ON price_history (item_id, recorded_at DESC);
            """
        )
        # Migrate older databases safely
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(tracked_items)").fetchall()}
        if "image_url" not in cols:
            self._conn.execute("ALTER TABLE tracked_items ADD COLUMN image_url TEXT")
        if "notify_user_ids" not in cols:
            self._conn.execute("ALTER TABLE tracked_items ADD COLUMN notify_user_ids TEXT NOT NULL DEFAULT '[]'")
        if "category" not in cols:
            self._conn.execute("ALTER TABLE tracked_items ADD COLUMN category TEXT")
        self._conn.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _month_key(now: datetime | None = None) -> str:
        dt = now or datetime.now(timezone.utc)
        return dt.strftime("%Y-%m")

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> TrackedItem:
        keys = row.keys()
        raw_notify = row["notify_user_ids"] if "notify_user_ids" in keys else "[]"
        try:
            parsed_notify = tuple(str(u) for u in json.loads(raw_notify or "[]"))
        except json.JSONDecodeError:
            parsed_notify = ()

        return TrackedItem(
            id=row["id"],
            name=row["name"],
            set_number=row["set_number"],
            category=row["category"] if "category" in keys else None,
            rrp_gbp=row["rrp_gbp"],
            target_price_gbp=row["target_price_gbp"],
            min_discount_pct=row["min_discount_pct"],
            url=row["url"],
            retailer=row["retailer"],
            is_retiring=bool(row["is_retiring"]),
            image_url=row["image_url"] if "image_url" in keys else None,
            notify_user_ids=parsed_notify,
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
        category: str | None = None,
        target_price_gbp: float | None = None,
        min_discount_pct: float = 15.0,
        url: str | None = None,
        retailer: str = "other",
        is_retiring: bool = False,
        image_url: str | None = None,
        notify_user_ids: list[str] | tuple[str, ...] | None = None,
    ) -> TrackedItem:
        now = self._now_iso()
        notify_json = json.dumps([str(u) for u in (notify_user_ids or [])])
        cur = self._conn.execute(
            """
            INSERT INTO tracked_items (
                name, set_number, category, rrp_gbp, target_price_gbp,
                min_discount_pct, url, retailer, is_retiring,
                image_url, notify_user_ids, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                set_number,
                category,
                round(rrp_gbp, 2),
                round(target_price_gbp, 2) if target_price_gbp is not None else None,
                round(min_discount_pct, 1),
                url,
                retailer,
                1 if is_retiring else 0,
                image_url,
                notify_json,
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

    def get_item_by_set_number(self, set_number: str) -> TrackedItem | None:
        clean = set_number.strip().split("-")[0]
        row = self._conn.execute(
            "SELECT * FROM tracked_items WHERE set_number = ? OR set_number = ?",
            (set_number.strip(), clean),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def update_item(
        self,
        item_id: int,
        *,
        name: str | None = None,
        category: str | None = None,
        rrp_gbp: float | None = None,
        target_price_gbp: float | None = None,
        min_discount_pct: float | None = None,
        is_retiring: bool | None = None,
        image_url: str | None = None,
        notify_user_ids: list[str] | tuple[str, ...] | None = None,
    ) -> TrackedItem | None:
        fields: list[str] = []
        values: list[object] = []

        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if category is not None:
            fields.append("category = ?")
            values.append(category)
        if rrp_gbp is not None:
            fields.append("rrp_gbp = ?")
            values.append(round(rrp_gbp, 2))
        if target_price_gbp is not None:
            fields.append("target_price_gbp = ?")
            values.append(round(target_price_gbp, 2) if target_price_gbp > 0 else None)
        if min_discount_pct is not None:
            fields.append("min_discount_pct = ?")
            values.append(round(min_discount_pct, 1))
        if is_retiring is not None:
            fields.append("is_retiring = ?")
            values.append(1 if is_retiring else 0)
        if image_url is not None:
            fields.append("image_url = ?")
            values.append(image_url)
        if notify_user_ids is not None:
            fields.append("notify_user_ids = ?")
            values.append(json.dumps([str(u) for u in notify_user_ids]))

        if not fields:
            return self.get_item(item_id)

        values.append(item_id)
        self._conn.execute(
            f"UPDATE tracked_items SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
        self._conn.commit()
        return self.get_item(item_id)

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

    def get_latest_price(self, item_id: int) -> PriceRecord | None:
        hist = self.get_price_history(item_id, limit=1)
        return hist[0] if hist else None

    def get_historical_low(self, item_id: int) -> float | None:
        row = self._conn.execute(
            "SELECT MIN(total_gbp) AS min_total FROM price_history WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return round(float(row["min_total"]), 2) if row and row["min_total"] is not None else None

    def should_alert(
        self,
        item: TrackedItem,
        total_gbp: float,
        cooldown_hours: int,
    ) -> bool:
        """Price-drop-only alerts: alert on first deal, or if price drops >= £1 further.
        Once a deal ends (reset_alert_state), a future deal will alert again."""
        if item.last_alert_at is None or item.last_alert_price_gbp is None:
            return True

        return total_gbp <= item.last_alert_price_gbp - 1.0

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

    def reset_alert_state(self, item_id: int) -> None:
        self._conn.execute(
            """
            UPDATE tracked_items
            SET last_alert_price_gbp = NULL, last_alert_at = NULL
            WHERE id = ?
            """,
            (item_id,),
        )
        self._conn.commit()

    def purge_implausible_history(self) -> tuple[int, int]:
        """Remove poisoned/stale outlier price_history rows (<40% of RRP or 'other' unverified)
        and reset invalid last_alert_price_gbp states so genuine UK deals can alert properly."""
        deleted_rows = 0
        reset_items = 0
        cur_other = self._conn.execute("DELETE FROM price_history WHERE retailer = 'other'")
        deleted_rows += cur_other.rowcount

        for item in self.list_items():
            if item.rrp_gbp <= 0:
                continue
            min_ok = round(item.rrp_gbp * 0.40, 2)
            max_ok = round(item.rrp_gbp * 1.35, 2)
            cur = self._conn.execute(
                "DELETE FROM price_history WHERE item_id = ? AND (price_gbp < ? OR price_gbp > ?)",
                (item.id, min_ok, max_ok),
            )
            deleted_rows += cur.rowcount
            if item.last_alert_price_gbp is not None and item.last_alert_price_gbp < min_ok:
                self._conn.execute(
                    "UPDATE tracked_items SET last_alert_price_gbp = NULL, last_alert_at = NULL WHERE id = ?",
                    (item.id,),
                )
                reset_items += 1

        self._conn.commit()
        return deleted_rows, reset_items

    def prune_old_history(self, retention_days: int = 180) -> int:
        """Delete price_history rows older than retention_days to keep SQLite compact."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cur = self._conn.execute("DELETE FROM price_history WHERE recorded_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    # --- Persisted Dev Mode ---

    def get_dev_mode(self, default: bool = False) -> bool:
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key = 'dev_mode'"
        ).fetchone()
        if not row:
            return default
        return row["value"].lower() == "true"

    def set_dev_mode(self, enabled: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('dev_mode', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("true" if enabled else "false",),
        )
        self._conn.commit()

    def get_amazon_dp_urls(self) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key = 'amazon_dp_urls'"
        ).fetchone()
        if not row or not row["value"]:
            return {}
        try:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k and v}
        except Exception:
            pass
        return {}

    def set_amazon_dp_url(self, set_number: str, url: str | None) -> None:
        clean_num = set_number.strip().split("-")[0]
        urls = self.get_amazon_dp_urls()
        if url:
            if urls.get(clean_num) == url:
                return
            urls[clean_num] = url
        else:
            if clean_num not in urls:
                return
            urls.pop(clean_num, None)
        self._conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('amazon_dp_urls', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps(urls),),
        )
        self._conn.commit()

    # --- Monthly Token Quota Governor ---

    def get_monthly_usage(self, provider: str, now: datetime | None = None) -> int:
        mkey = self._month_key(now)
        row = self._conn.execute(
            "SELECT calls_used FROM api_quota WHERE provider = ? AND month_key = ?",
            (provider, mkey),
        ).fetchone()
        return int(row["calls_used"]) if row else 0

    def increment_monthly_usage(self, provider: str, count: int = 1, now: datetime | None = None) -> int:
        mkey = self._month_key(now)
        self._conn.execute(
            """
            INSERT INTO api_quota (provider, month_key, calls_used)
            VALUES (?, ?, ?)
            ON CONFLICT(provider, month_key) DO UPDATE SET calls_used = calls_used + excluded.calls_used
            """,
            (provider, mkey, count),
        )
        self._conn.commit()
        return self.get_monthly_usage(provider, now=now)

    def can_spend_token(
        self,
        provider: str,
        monthly_limit: int,
        *,
        manual_force: bool = False,
        now: datetime | None = None,
    ) -> tuple[bool, int]:
        """Pace API usage across the calendar month so tokens NEVER run out early.
        Returns (allowed, max_cumulative_allowed_today)."""
        dt = now or datetime.now(timezone.utc)
        used = self.get_monthly_usage(provider, now=dt)
        safe_ceiling = max(1, monthly_limit - 2)  # Always leave 2 safety margin
        if used >= safe_ceiling:
            return False, safe_ceiling

        if manual_force:
            return True, safe_ceiling

        # Reserve 15% of monthly limit for manual force re-checks
        auto_pool = max(1, int(monthly_limit * 0.85))
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        allowed_by_today = max(1, math.ceil((auto_pool / days_in_month) * dt.day))
        return used < allowed_by_today, allowed_by_today

    def get_items_due_for_check(
        self,
        *,
        min_age_hours: float = 4.0,
        now: datetime | None = None,
    ) -> list[TrackedItem]:
        """Return wishlist items due for a background check, stalest first (retiring prioritized 1.5x)."""
        dt = now or datetime.now(timezone.utc)
        scored: list[tuple[float, TrackedItem]] = []
        for item in self.list_items():
            latest = self.get_latest_price(item.id)
            if latest is None:
                scored.append((1e9, item))
                continue
            try:
                rec_dt = datetime.fromisoformat(latest.recorded_at)
                if rec_dt.tzinfo is None:
                    rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                age_hours = (dt - rec_dt).total_seconds() / 3600.0
            except ValueError:
                age_hours = 999.0

            effective_min = min_age_hours / 1.5 if item.is_retiring else min_age_hours
            if age_hours >= effective_min:
                priority = age_hours * (1.5 if item.is_retiring else 1.0)
                scored.append((priority, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]

    def should_send_weekly_summary(self) -> bool:
        """Return True on Sundays if a weekly summary hasn't been sent in the last 6 days."""
        now = datetime.now(timezone.utc)
        if now.weekday() != 6:  # Sunday
            return False
        row = self._conn.execute(
            "SELECT value FROM bot_state WHERE key = 'last_weekly_summary_at'"
        ).fetchone()
        if not row:
            return True
        last_dt = datetime.fromisoformat(row["value"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return now - last_dt >= timedelta(days=6)

    def mark_weekly_summary_sent(self) -> None:
        self._conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES ('last_weekly_summary_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (self._now_iso(),),
        )
        self._conn.commit()

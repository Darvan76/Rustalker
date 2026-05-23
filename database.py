from __future__ import annotations

import datetime as dt
from typing import Any

import aiosqlite


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self._create_schema()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()

    async def _create_schema(self) -> None:
        assert self.conn is not None

        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                alert_channel_id INTEGER,
                mention_role_id INTEGER,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                clan_spike_window_minutes INTEGER NOT NULL DEFAULT 15,
                clan_spike_threshold INTEGER NOT NULL DEFAULT 3,
                queue_threshold INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tracked_servers (
                guild_id INTEGER NOT NULL,
                battlemetrics_server_id INTEGER NOT NULL,
                name TEXT,
                last_map TEXT,
                last_ip TEXT,
                last_port INTEGER,
                last_player_count INTEGER,
                last_max_players INTEGER,
                last_queue INTEGER,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, battlemetrics_server_id)
            );

            CREATE TABLE IF NOT EXISTS players (
                battlemetrics_player_id INTEGER PRIMARY KEY,
                current_name TEXT NOT NULL,
                steam_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                guild_id INTEGER NOT NULL,
                battlemetrics_player_id INTEGER NOT NULL,
                notify_channel_id INTEGER,
                added_by INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, battlemetrics_player_id),
                FOREIGN KEY (battlemetrics_player_id) REFERENCES players(battlemetrics_player_id)
            );

            CREATE TABLE IF NOT EXISTS clans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS clan_members (
                guild_id INTEGER NOT NULL,
                clan_id INTEGER NOT NULL,
                battlemetrics_player_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, clan_id, battlemetrics_player_id),
                FOREIGN KEY (clan_id) REFERENCES clans(id) ON DELETE CASCADE,
                FOREIGN KEY (battlemetrics_player_id) REFERENCES players(battlemetrics_player_id)
            );

            CREATE TABLE IF NOT EXISTS presence_snapshots (
                guild_id INTEGER NOT NULL,
                battlemetrics_server_id INTEGER NOT NULL,
                battlemetrics_player_id INTEGER NOT NULL,
                is_online INTEGER NOT NULL,
                first_seen_online_at TEXT,
                last_seen_online_at TEXT,
                last_seen_name TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, battlemetrics_server_id, battlemetrics_player_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                battlemetrics_server_id INTEGER NOT NULL,
                battlemetrics_player_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER
            );
            """
        )
        await self.conn.commit()

    async def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        assert self.conn is not None
        cursor = await self.conn.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        assert self.conn is not None
        await self.conn.execute(query, params)
        await self.conn.commit()

    async def ensure_guild_settings(self, guild_id: int) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (guild_id, now, now),
        )

    async def set_alert_channel(self, guild_id: int, channel_id: int) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, alert_channel_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                alert_channel_id = excluded.alert_channel_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, now, now),
        )

    async def set_mention_role(self, guild_id: int, role_id: int | None) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, mention_role_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                mention_role_id = excluded.mention_role_id,
                updated_at = excluded.updated_at
            """,
            (guild_id, role_id, now, now),
        )

    async def get_guild_settings(self, guild_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )

    async def set_queue_threshold(self, guild_id: int, threshold: int) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, queue_threshold, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                queue_threshold = excluded.queue_threshold,
                updated_at = excluded.updated_at
            """,
            (guild_id, threshold, now, now),
        )

    async def set_clan_spike_rules(self, guild_id: int, window_minutes: int, threshold: int) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, clan_spike_window_minutes, clan_spike_threshold, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                clan_spike_window_minutes = excluded.clan_spike_window_minutes,
                clan_spike_threshold = excluded.clan_spike_threshold,
                updated_at = excluded.updated_at
            """,
            (guild_id, window_minutes, threshold, now, now),
        )

    async def upsert_player(self, player_id: int, name: str, steam_id: str | None = None) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO players (battlemetrics_player_id, current_name, steam_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(battlemetrics_player_id) DO UPDATE SET
                current_name = excluded.current_name,
                steam_id = COALESCE(excluded.steam_id, players.steam_id),
                updated_at = excluded.updated_at
            """,
            (player_id, name, steam_id, now, now),
        )

    async def get_player(self, player_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM players WHERE battlemetrics_player_id = ?",
            (player_id,),
        )

    async def add_watch_player(
        self,
        guild_id: int,
        player_id: int,
        notify_channel_id: int | None,
        added_by: int,
        notes: str | None = None,
    ) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO watchlist (guild_id, battlemetrics_player_id, notify_channel_id, added_by, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, battlemetrics_player_id) DO UPDATE SET
                notify_channel_id = excluded.notify_channel_id,
                added_by = excluded.added_by,
                notes = COALESCE(excluded.notes, watchlist.notes)
            """,
            (guild_id, player_id, notify_channel_id, added_by, notes, now),
        )

    async def remove_watch_player(self, guild_id: int, player_id: int) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "DELETE FROM watchlist WHERE guild_id = ? AND battlemetrics_player_id = ?",
            (guild_id, player_id),
        )
        await self.conn.commit()
        count = cursor.rowcount
        await cursor.close()
        return count

    async def list_watch_players(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT
                w.guild_id,
                w.battlemetrics_player_id,
                w.notify_channel_id,
                p.current_name,
                p.steam_id,
                c.id AS clan_id,
                c.name AS clan_name
            FROM watchlist w
            JOIN players p ON p.battlemetrics_player_id = w.battlemetrics_player_id
            LEFT JOIN clan_members cm
                ON cm.guild_id = w.guild_id
               AND cm.battlemetrics_player_id = w.battlemetrics_player_id
            LEFT JOIN clans c
                ON c.id = cm.clan_id
               AND c.guild_id = w.guild_id
            WHERE w.guild_id = ?
            ORDER BY p.current_name COLLATE NOCASE ASC
            """,
            (guild_id,),
        )

    async def create_clan(self, guild_id: int, name: str, created_by: int) -> int:
        now = utc_now_iso()
        assert self.conn is not None
        cursor = await self.conn.execute(
            """
            INSERT INTO clans (guild_id, name, created_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, name, created_by, now),
        )
        await self.conn.commit()
        clan_id = cursor.lastrowid
        await cursor.close()
        return int(clan_id)

    async def get_clan_by_name(self, guild_id: int, name: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM clans WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )

    async def get_clan_by_id(self, guild_id: int, clan_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM clans WHERE guild_id = ? AND id = ?",
            (guild_id, clan_id),
        )

    async def list_clans(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT c.*, COUNT(cm.battlemetrics_player_id) AS members
            FROM clans c
            LEFT JOIN clan_members cm
                ON cm.clan_id = c.id
               AND cm.guild_id = c.guild_id
            WHERE c.guild_id = ?
            GROUP BY c.id
            ORDER BY c.name COLLATE NOCASE ASC
            """,
            (guild_id,),
        )

    async def add_clan_member(self, guild_id: int, clan_id: int, player_id: int) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO clan_members (guild_id, clan_id, battlemetrics_player_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, clan_id, battlemetrics_player_id) DO NOTHING
            """,
            (guild_id, clan_id, player_id, now),
        )

    async def remove_clan_member(self, guild_id: int, clan_id: int, player_id: int) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            """
            DELETE FROM clan_members
            WHERE guild_id = ? AND clan_id = ? AND battlemetrics_player_id = ?
            """,
            (guild_id, clan_id, player_id),
        )
        await self.conn.commit()
        count = cursor.rowcount
        await cursor.close()
        return count

    async def get_player_clans(self, guild_id: int, player_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT c.*
            FROM clans c
            JOIN clan_members cm ON cm.clan_id = c.id
            WHERE cm.guild_id = ? AND cm.battlemetrics_player_id = ?
            """,
            (guild_id, player_id),
        )

    async def list_clan_members(self, guild_id: int, clan_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT p.battlemetrics_player_id, p.current_name, p.steam_id
            FROM clan_members cm
            JOIN players p ON p.battlemetrics_player_id = cm.battlemetrics_player_id
            WHERE cm.guild_id = ? AND cm.clan_id = ?
            ORDER BY p.current_name COLLATE NOCASE ASC
            """,
            (guild_id, clan_id),
        )

    async def add_tracked_server(self, guild_id: int, server_id: int, name: str | None) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO tracked_servers (
                guild_id,
                battlemetrics_server_id,
                name,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, battlemetrics_server_id) DO UPDATE SET
                name = COALESCE(excluded.name, tracked_servers.name),
                updated_at = excluded.updated_at
            """,
            (guild_id, server_id, name, now, now),
        )

    async def remove_tracked_server(self, guild_id: int, server_id: int) -> int:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "DELETE FROM tracked_servers WHERE guild_id = ? AND battlemetrics_server_id = ?",
            (guild_id, server_id),
        )
        await self.conn.commit()
        count = cursor.rowcount
        await cursor.close()
        return count

    async def list_tracked_servers(self, guild_id: int | None = None) -> list[aiosqlite.Row]:
        if guild_id is None:
            return await self.fetchall(
                "SELECT * FROM tracked_servers ORDER BY guild_id, name"
            )
        return await self.fetchall(
            "SELECT * FROM tracked_servers WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )

    async def update_server_state(
        self,
        guild_id: int,
        server_id: int,
        name: str | None,
        map_name: str | None,
        ip: str | None,
        port: int | None,
        player_count: int | None,
        max_players: int | None,
        queue: int | None,
    ) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            UPDATE tracked_servers
            SET name = COALESCE(?, name),
                last_map = ?,
                last_ip = ?,
                last_port = ?,
                last_player_count = ?,
                last_max_players = ?,
                last_queue = ?,
                last_checked_at = ?,
                updated_at = ?
            WHERE guild_id = ? AND battlemetrics_server_id = ?
            """,
            (
                name,
                map_name,
                ip,
                port,
                player_count,
                max_players,
                queue,
                now,
                now,
                guild_id,
                server_id,
            ),
        )

    async def get_presence_snapshot(self, guild_id: int, server_id: int, player_id: int) -> aiosqlite.Row | None:
        return await self.fetchone(
            """
            SELECT *
            FROM presence_snapshots
            WHERE guild_id = ? AND battlemetrics_server_id = ? AND battlemetrics_player_id = ?
            """,
            (guild_id, server_id, player_id),
        )

    async def list_presence_snapshots_for_server(self, guild_id: int, server_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT *
            FROM presence_snapshots
            WHERE guild_id = ? AND battlemetrics_server_id = ?
            """,
            (guild_id, server_id),
        )

    async def upsert_presence_snapshot(
        self,
        guild_id: int,
        server_id: int,
        player_id: int,
        is_online: bool,
        current_name: str | None,
        first_seen_online_at: str | None,
        last_seen_online_at: str | None,
    ) -> None:
        now = utc_now_iso()
        await self.execute(
            """
            INSERT INTO presence_snapshots (
                guild_id,
                battlemetrics_server_id,
                battlemetrics_player_id,
                is_online,
                first_seen_online_at,
                last_seen_online_at,
                last_seen_name,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, battlemetrics_server_id, battlemetrics_player_id) DO UPDATE SET
                is_online = excluded.is_online,
                first_seen_online_at = excluded.first_seen_online_at,
                last_seen_online_at = excluded.last_seen_online_at,
                last_seen_name = COALESCE(excluded.last_seen_name, presence_snapshots.last_seen_name),
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                server_id,
                player_id,
                1 if is_online else 0,
                first_seen_online_at,
                last_seen_online_at,
                current_name,
                now,
            ),
        )

    async def open_session(self, guild_id: int, server_id: int, player_id: int, started_at: str) -> None:
        open_session = await self.fetchone(
            """
            SELECT id
            FROM sessions
            WHERE guild_id = ?
              AND battlemetrics_server_id = ?
              AND battlemetrics_player_id = ?
              AND ended_at IS NULL
            """,
            (guild_id, server_id, player_id),
        )
        if open_session is not None:
            return
        await self.execute(
            """
            INSERT INTO sessions (guild_id, battlemetrics_server_id, battlemetrics_player_id, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, server_id, player_id, started_at),
        )

    async def close_session(
        self,
        guild_id: int,
        server_id: int,
        player_id: int,
        ended_at: str,
    ) -> int | None:
        open_session = await self.fetchone(
            """
            SELECT id, started_at
            FROM sessions
            WHERE guild_id = ?
              AND battlemetrics_server_id = ?
              AND battlemetrics_player_id = ?
              AND ended_at IS NULL
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (guild_id, server_id, player_id),
        )
        if open_session is None:
            return None

        started_at = dt.datetime.fromisoformat(open_session["started_at"])
        ended_dt = dt.datetime.fromisoformat(ended_at)
        duration_seconds = max(0, int((ended_dt - started_at).total_seconds()))

        await self.execute(
            """
            UPDATE sessions
            SET ended_at = ?, duration_seconds = ?
            WHERE id = ?
            """,
            (ended_at, duration_seconds, open_session["id"]),
        )
        return duration_seconds

    async def get_online_players_for_server(self, guild_id: int, server_id: int) -> list[int]:
        rows = await self.fetchall(
            """
            SELECT battlemetrics_player_id
            FROM presence_snapshots
            WHERE guild_id = ?
              AND battlemetrics_server_id = ?
              AND is_online = 1
            """,
            (guild_id, server_id),
        )
        return [int(r["battlemetrics_player_id"]) for r in rows]

    async def get_player_activity_by_hour(self, guild_id: int, player_id: int, days: int = 14) -> list[int]:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        rows = await self.fetchall(
            """
            SELECT started_at, ended_at
            FROM sessions
            WHERE guild_id = ?
              AND battlemetrics_player_id = ?
              AND started_at >= ?
            """,
            (guild_id, player_id, since),
        )

        buckets = [0 for _ in range(24)]
        for row in rows:
            start = dt.datetime.fromisoformat(row["started_at"])
            end_raw = row["ended_at"]
            if end_raw is None:
                end = dt.datetime.now(dt.timezone.utc)
            else:
                end = dt.datetime.fromisoformat(end_raw)
            if end <= start:
                continue

            cursor = start
            while cursor < end:
                next_hour = (cursor + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                if next_hour <= cursor:
                    next_hour = cursor + dt.timedelta(minutes=1)
                segment_end = min(end, next_hour)
                minutes = int((segment_end - cursor).total_seconds() // 60)
                buckets[cursor.hour] += max(1, minutes)
                cursor = segment_end

        return buckets

    async def get_clan_member_ids(self, guild_id: int, clan_id: int) -> set[int]:
        rows = await self.fetchall(
            """
            SELECT battlemetrics_player_id
            FROM clan_members
            WHERE guild_id = ? AND clan_id = ?
            """,
            (guild_id, clan_id),
        )
        return {int(r["battlemetrics_player_id"]) for r in rows}

    async def list_open_sessions(self, guild_id: int, server_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT *
            FROM sessions
            WHERE guild_id = ?
              AND battlemetrics_server_id = ?
              AND ended_at IS NULL
            """,
            (guild_id, server_id),
        )

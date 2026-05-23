from __future__ import annotations

import re
from typing import Any

import aiohttp


class BattleMetricsError(Exception):
    pass


class BattleMetricsClient:
    BASE_URL = "https://api.battlemetrics.com"

    def __init__(self, api_token: str | None = None, timeout_seconds: int = 25) -> None:
        self.api_token = api_token
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        self.session = aiohttp.ClientSession(timeout=self.timeout, headers=headers)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if self.session is None:
            raise BattleMetricsError("BattleMetrics client is not started")

        url = f"{self.BASE_URL}{path}"
        async with self.session.get(url, params=params) as response:
            if response.status >= 400:
                body = await response.text()
                raise BattleMetricsError(f"BattleMetrics error {response.status}: {body}")
            data = await response.json()
            if not isinstance(data, dict):
                raise BattleMetricsError("Unexpected API response format")
            return data

    async def get_player(self, player_id: int) -> dict[str, Any]:
        payload = await self._get(f"/players/{player_id}", params={"include": "identifier"})
        data = payload.get("data") or {}
        attributes = data.get("attributes") or {}
        included = payload.get("included") or []

        name = attributes.get("name") or f"Player {player_id}"
        steam_id: str | None = None
        for item in included:
            if item.get("type") != "identifier":
                continue
            attrs = item.get("attributes") or {}
            identifier_type = attrs.get("type")
            if identifier_type in {"steamID", "steamid", "steam_id"}:
                steam_id = attrs.get("identifier")
                break

        return {
            "id": int(data.get("id", player_id)),
            "name": str(name),
            "steam_id": steam_id,
        }

    async def get_server(self, server_id: int, include_players: bool = True) -> dict[str, Any]:
        params = {"include": "player"} if include_players else None
        payload = await self._get(f"/servers/{server_id}", params=params)

        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        details = attrs.get("details") or {}

        queue = (
            details.get("rust_queued_players")
            or details.get("rust_queued_players_total")
            or details.get("queued_players")
            or 0
        )
        map_name = (
            details.get("rust_map")
            or details.get("map")
            or details.get("mapName")
            or None
        )

        included_players: list[dict[str, Any]] = []
        for item in payload.get("included") or []:
            if item.get("type") != "player":
                continue
            player_attrs = item.get("attributes") or {}
            try:
                pid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            included_players.append(
                {
                    "id": pid,
                    "name": str(player_attrs.get("name") or f"Player {pid}"),
                }
            )

        try:
            parsed_queue = int(queue)
        except (TypeError, ValueError):
            parsed_queue = 0

        return {
            "id": int(data.get("id", server_id)),
            "name": attrs.get("name") or f"Server {server_id}",
            "ip": attrs.get("ip"),
            "port": attrs.get("port"),
            "players": attrs.get("players"),
            "max_players": attrs.get("maxPlayers"),
            "queue": parsed_queue,
            "map": map_name,
            "included_players": included_players,
        }


PLAYER_ID_PATTERNS = [
    re.compile(r"battlemetrics\.com/players/(\d+)", re.IGNORECASE),
    re.compile(r"^(\d+)$"),
]

SERVER_ID_PATTERNS = [
    re.compile(r"battlemetrics\.com/servers/[^/]+/(\d+)", re.IGNORECASE),
    re.compile(r"battlemetrics\.com/servers/(\d+)", re.IGNORECASE),
    re.compile(r"^(\d+)$"),
]


def parse_player_id(raw: str) -> int | None:
    text = raw.strip()
    for pattern in PLAYER_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def parse_server_id(raw: str) -> int | None:
    text = raw.strip()
    for pattern in SERVER_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None

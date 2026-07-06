from __future__ import annotations

import datetime as dt
import logging
import traceback
import zoneinfo
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import tasks, commands

from battlemetrics import BattleMetricsError

if TYPE_CHECKING:
    from main import RustalkerBot

logger = logging.getLogger("rustalker.tracker")


class TrackerCog(commands.Cog):
    def __init__(self, bot: RustalkerBot) -> None:
        self.bot = bot
        # Dictionary to throttle clan spike alerts: {(guild_id, clan_id): last_alert_time}
        self.last_clan_spike_alerts: dict[tuple[int, int], dt.datetime] = {}
        # Dictionary to track last seen queue size to avoid spamming queue alerts
        # {(guild_id, server_id): last_seen_queue}
        self.last_seen_queues: dict[tuple[int, int], int] = {}
        # Dictionary to throttle channel updates: {(guild_id, channel_id): last_update_time}
        self.last_channel_updates: dict[tuple[int, int], dt.datetime] = {}
        # Dictionary to track last daily summary sent date: {guild_id: date}
        self.last_summary_sent: dict[int, dt.date] = {}
        
        self.tracker_loop.start()
        self.daily_summary_loop.start()

    def cog_unload(self) -> None:
        self.tracker_loop.cancel()
        self.daily_summary_loop.cancel()

    @tasks.loop(seconds=60)
    async def tracker_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
            logger.info("Starting background tracker check...")
            
            # Fetch all tracked servers across all guilds
            all_tracked = await self.bot.db.list_tracked_servers()
            if not all_tracked:
                logger.info("No servers tracked yet. Skipping check.")
                return

            # Group tracked servers by BattleMetrics Server ID to query each only once
            # bm_id -> list of tracked_server rows
            servers_by_bm_id: dict[int, list[Any]] = {}
            for row in all_tracked:
                bm_id = int(row["battlemetrics_server_id"])
                servers_by_bm_id.setdefault(bm_id, []).append(row)

            # Query BattleMetrics for each unique server
            for bm_id, guild_rows in servers_by_bm_id.items():
                try:
                    server_data = await self.bot.bm_client.get_server(bm_id, include_players=True)
                except BattleMetricsError as e:
                    logger.warning(f"Failed to fetch BattleMetrics server {bm_id}: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Unexpected error fetching BattleMetrics server {bm_id}: {e}\n{traceback.format_exc()}")
                    continue

                # Process this server data for each guild tracking it
                for server_row in guild_rows:
                    guild_id = int(server_row["guild_id"])
                    
                    # Fetch guild object from Discord
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue

                    await self._process_guild_server_update(guild, server_row, server_data)

            logger.info("Background tracker check complete.")
        except Exception as e:
            logger.error(f"Error in tracker loop: {e}\n{traceback.format_exc()}")

    async def _process_guild_server_update(
        self,
        guild: discord.Guild,
        server_row: Any,
        server_data: dict[str, Any]
    ) -> None:
        guild_id = guild.id
        server_id = int(server_row["battlemetrics_server_id"])
        
        # 1. Fetch settings for this guild
        await self.bot.db.ensure_guild_settings(guild_id)
        settings = await self.bot.db.get_guild_settings(guild_id)
        if not settings:
            return

        alert_channel_id = settings["alert_channel_id"]
        mention_role_id = settings["mention_role_id"]
        queue_threshold = settings["queue_threshold"]
        
        # Get target channel for alerts
        alert_channel = guild.get_channel(alert_channel_id) if alert_channel_id else None

        # 2. Monitor Server Wipe / Map Changes
        last_map = server_row["last_map"]
        current_map = server_data["map"]
        if last_map and current_map and last_map.lower() != current_map.lower():
            await self.bot.db.add_wipe_event(guild_id, server_id, current_map)
            if alert_channel:
                embed = discord.Embed(
                    title="✨ POSIBLE WIPE DETECTADO (CAMBIO DE MAPA)",
                    description=f"El mapa del servidor **{server_data['name']}** ha cambiado.",
                    color=discord.Color.blue(),
                    timestamp=dt.datetime.now(dt.timezone.utc)
                )
                embed.add_field(name="Mapa Anterior", value=f"`{last_map}`", inline=True)
                embed.add_field(name="Mapa Nuevo", value=f"`{current_map}`", inline=True)
                embed.add_field(name="IP:Puerto", value=f"`{server_data['ip']}:{server_data['port']}`", inline=False)
                if mention_role_id:
                    await alert_channel.send(content=f"<@&{mention_role_id}>", embed=embed)
                else:
                    await alert_channel.send(embed=embed)

        # 3. Monitor Server Queue Drops
        current_queue = server_data["queue"]
        last_seen_queue = self.last_seen_queues.get((guild_id, server_id))
        if last_seen_queue is not None and current_queue < last_seen_queue:
            if current_queue <= queue_threshold < last_seen_queue:
                if alert_channel:
                    embed = discord.Embed(
                        title="🎫 COLA BAJA EN SERVIDOR",
                        description=f"La cola en **{server_data['name']}** ha bajado considerablemente.",
                        color=discord.Color.teal(),
                        timestamp=dt.datetime.now(dt.timezone.utc)
                    )
                    embed.add_field(name="Cola Actual", value=f"`{current_queue}` jugadores", inline=True)
                    embed.add_field(name="Límite configurado", value=f"`{queue_threshold}` jugadores", inline=True)
                    embed.set_footer(text="¡Momento ideal para conectarse!")
                    await alert_channel.send(embed=embed)
        self.last_seen_queues[(guild_id, server_id)] = current_queue

        # Update the server state in the database
        await self.bot.db.update_server_state(
            guild_id=guild_id,
            server_id=server_id,
            name=server_data["name"],
            map_name=server_data["map"],
            ip=server_data["ip"],
            port=server_data["port"],
            player_count=server_data["players"],
            max_players=server_data["max_players"],
            queue=server_data["queue"]
        )

        # 3.1 Update dynamic voice channels if configured
        players_channel_id = settings.get("stats_channel_players_id")
        queue_channel_id = settings.get("stats_channel_queue_id")
        map_channel_id = settings.get("stats_channel_map_id")

        if players_channel_id:
            players_count = server_data.get("players", 0)
            max_players = server_data.get("max_players", 0)
            await self._update_stats_channel(guild, players_channel_id, f"👥 Jugadores: {players_count}/{max_players}")

        if queue_channel_id:
            queue_count = server_data.get("queue", 0)
            await self._update_stats_channel(guild, queue_channel_id, f"🎫 Cola: {queue_count}")

        if map_channel_id:
            map_name = server_data.get("map", "Desconocido")
            await self._update_stats_channel(guild, map_channel_id, f"🗺️ Mapa: {map_name}")

        # 4. Track Players Presence
        # Fetch the current watchlist for this guild
        watchlist_rows = await self.bot.db.list_watch_players(guild_id)
        if not watchlist_rows:
            return

        # Map watchlist players by BattleMetrics Player ID
        watchlist_by_id = {int(r["battlemetrics_player_id"]): r for r in watchlist_rows}

        # Build list of player IDs currently online on this server
        current_online_players = server_data["included_players"] # list of dict: {"id": int, "name": str}
        online_by_id = {int(p["id"]): p for p in current_online_players}

        # Fetch last known presence snapshots for this server in this guild
        snapshots = await self.bot.db.list_presence_snapshots_for_server(guild_id, server_id)
        snapshots_by_id = {int(s["battlemetrics_player_id"]): s for s in snapshots}

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

        for player_id, watch_row in watchlist_by_id.items():
            is_currently_online = player_id in online_by_id
            last_snapshot = snapshots_by_id.get(player_id)
            
            was_online = last_snapshot is not None and bool(last_snapshot["is_online"])
            last_seen_name = last_snapshot["last_seen_name"] if last_snapshot else None
            first_seen_online_at = last_snapshot["first_seen_online_at"] if last_snapshot else None

            # Determine player name
            current_api_name = online_by_id[player_id]["name"] if is_currently_online else watch_row["current_name"]

            # Save/Update player name in database
            await self.bot.db.upsert_player(player_id, current_api_name)

            # Determine the correct channel for alerts (watchlist item channel, or fallback to general guild alert channel)
            channel_id = watch_row["notify_channel_id"] or alert_channel_id
            player_alert_channel = guild.get_channel(channel_id) if channel_id else None

            # CASE 1: CONNECTION (Offline -> Online)
            if is_currently_online and not was_online:
                # Update Snapshot and Open Session
                await self.bot.db.upsert_presence_snapshot(
                    guild_id=guild_id,
                    server_id=server_id,
                    player_id=player_id,
                    is_online=True,
                    current_name=current_api_name,
                    first_seen_online_at=now_iso,
                    last_seen_online_at=now_iso
                )
                await self.bot.db.open_session(guild_id, server_id, player_id, now_iso)

                # Send connection alert
                if player_alert_channel:
                    embed = discord.Embed(
                        title="🟢 CONEXIÓN DETECTADA",
                        color=discord.Color.green(),
                        timestamp=dt.datetime.now(dt.timezone.utc)
                    )
                    bm_link = f"[{current_api_name}](https://www.battlemetrics.com/players/{player_id})"
                    steam_id = watch_row["steam_id"]
                    steam_link = f"[{steam_id}](https://steamcommunity.com/profiles/{steam_id})" if steam_id else "`No disponible`"
                    
                    clan_name = watch_row["clan_name"] or "`Ninguno`"
                    notes = watch_row["notes"] or "`Ninguna`"

                    embed.add_field(name="👤 Jugador", value=bm_link, inline=True)
                    embed.add_field(name="🎮 Clan", value=clan_name, inline=True)
                    embed.add_field(name="🆔 Steam ID", value=steam_link, inline=True)
                    embed.add_field(name="🖥️ Servidor", value=f"**{server_data['name']}**", inline=False)
                    embed.add_field(name="📊 Población", value=f"`{server_data['players']}/{server_data['max_players']}` (Cola: `{server_data['queue']}`)", inline=True)
                    embed.add_field(name="📝 Notas", value=notes, inline=True)
                    
                    # Connection protocol
                    connect_uri = f"steam://connect/{server_data['ip']}:{server_data['port']}"
                    embed.add_field(name="🔗 Conexión Rápida", value=f"[Unirse al Servidor]({connect_uri})", inline=False)
                    
                    await player_alert_channel.send(embed=embed)

                # Process Clan Spike Alert
                clan_id = watch_row["clan_id"]
                if clan_id:
                    await self._check_clan_spike(guild, player_id, clan_id, clan_name, server_id, server_data, player_alert_channel, mention_role_id)

            # CASE 2: DISCONNECTION (Online -> Offline)
            elif not is_currently_online and was_online:
                # Close Session and Update Snapshot
                duration_seconds = await self.bot.db.close_session(guild_id, server_id, player_id, now_iso)
                await self.bot.db.upsert_presence_snapshot(
                    guild_id=guild_id,
                    server_id=server_id,
                    player_id=player_id,
                    is_online=False,
                    current_name=current_api_name,
                    first_seen_online_at=first_seen_online_at,
                    last_seen_online_at=now_iso
                )

                # Format Session Duration
                duration_str = "`Desconocida`"
                if duration_seconds is not None:
                    h = duration_seconds // 3600
                    m = (duration_seconds % 3600) // 60
                    s = duration_seconds % 60
                    duration_str = f"**{h}h {m}m {s}s**"

                # Send disconnection alert
                if player_alert_channel:
                    embed = discord.Embed(
                        title="🔴 DESCONEXIÓN DETECTADA",
                        color=discord.Color.red(),
                        timestamp=dt.datetime.now(dt.timezone.utc)
                    )
                    bm_link = f"[{current_api_name}](https://www.battlemetrics.com/players/{player_id})"
                    clan_name = watch_row["clan_name"] or "`Ninguno`"

                    embed.add_field(name="👤 Jugador", value=bm_link, inline=True)
                    embed.add_field(name="🎮 Clan", value=clan_name, inline=True)
                    embed.add_field(name="⏱️ Duración de Sesión", value=duration_str, inline=True)
                    embed.add_field(name="🖥️ Servidor", value=f"**{server_data['name']}**", inline=False)
                    
                    await player_alert_channel.send(embed=embed)

            # CASE 3: NAME CHANGE DETECTION (While Online)
            elif is_currently_online and was_online:
                # Check if name changed from last seen snap
                if last_seen_name and last_seen_name != current_api_name:
                    if player_alert_channel:
                        embed = discord.Embed(
                            title="🔄 DETECTADO CAMBIO DE NOMBRE",
                            description="Un jugador de la watchlist ha cambiado su nombre de juego.",
                            color=discord.Color.gold(),
                            timestamp=dt.datetime.now(dt.timezone.utc)
                        )
                        embed.add_field(name="Nombre Anterior", value=f"`{last_seen_name}`", inline=True)
                        embed.add_field(name="Nombre Nuevo", value=f"**{current_api_name}**", inline=True)
                        embed.add_field(name="ID de BattleMetrics", value=f"`{player_id}`", inline=True)
                        embed.add_field(name="Servidor", value=f"**{server_data['name']}**", inline=False)
                        embed.set_footer(text="BattleMetrics ID rastreable permanentemente")
                        await player_alert_channel.send(embed=embed)

                # Always update snapshot timestamp to keep first/last seen updated
                await self.bot.db.upsert_presence_snapshot(
                    guild_id=guild_id,
                    server_id=server_id,
                    player_id=player_id,
                    is_online=True,
                    current_name=current_api_name,
                    first_seen_online_at=first_seen_online_at,
                    last_seen_online_at=now_iso
                )

    async def _check_clan_spike(
        self,
        guild: discord.Guild,
        trigger_player_id: int,
        clan_id: int,
        clan_name: str,
        server_id: int,
        server_data: dict[str, Any],
        alert_channel: discord.TextChannel | None,
        mention_role_id: int | None
    ) -> None:
        guild_id = guild.id
        
        # Get threshold rules for this guild
        settings = await self.bot.db.get_guild_settings(guild_id)
        if not settings:
            return
        
        window_minutes = settings["clan_spike_window_minutes"]
        threshold = settings["clan_spike_threshold"]

        # Calculate time window
        since_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=window_minutes)).isoformat()

        # Query sessions of clan members started within the spike window
        query = """
            SELECT s.battlemetrics_player_id, p.current_name
            FROM sessions s
            JOIN clan_members cm ON cm.battlemetrics_player_id = s.battlemetrics_player_id AND cm.guild_id = s.guild_id
            JOIN players p ON p.battlemetrics_player_id = s.battlemetrics_player_id
            WHERE s.guild_id = ?
              AND s.battlemetrics_server_id = ?
              AND cm.clan_id = ?
              AND s.started_at >= ?
              AND s.ended_at IS NULL
            GROUP BY s.battlemetrics_player_id
        """
        recent_sessions = await self.bot.db.fetchall(query, (guild_id, server_id, clan_id, since_time))
        recent_count = len(recent_sessions)

        if recent_count >= threshold:
            # Check throttling: if we already sent a spike alert in the last window_minutes
            last_alert_time = self.last_clan_spike_alerts.get((guild_id, clan_id))
            now = dt.datetime.now(dt.timezone.utc)
            
            if last_alert_time is not None and (now - last_alert_time) < dt.timedelta(minutes=window_minutes):
                # Throttle alert to avoid spamming
                logger.info(f"Throttled clan spike alert for Clan '{clan_name}' on Guild {guild_id}")
                return

            self.last_clan_spike_alerts[(guild_id, clan_id)] = now

            if alert_channel:
                embed = discord.Embed(
                    title="⚠️ ALERTA TÁCTICA: ACTIVIDAD DE CLAN (SPIKE)",
                    description=f"Se ha detectado una conexión coordinada de miembros del clan **{clan_name}**.",
                    color=discord.Color.purple(),
                    timestamp=now
                )
                
                member_list = []
                for idx, row in enumerate(recent_sessions, 1):
                    name = row["current_name"]
                    pid = row["battlemetrics_player_id"]
                    is_trigger = " 🌟 (Acaba de entrar)" if pid == trigger_player_id else ""
                    member_list.append(f"{idx}. [{name}](https://www.battlemetrics.com/players/{pid}){is_trigger}")
                
                embed.add_field(name=f"👥 Miembros Conectados Recientemente ({recent_count})", value="\n".join(member_list), inline=False)
                embed.add_field(name="🖥️ Servidor", value=f"**{server_data['name']}**", inline=False)
                embed.set_footer(text=f"Regla: {threshold}+ conexiones en {window_minutes} minutos")

                mention_content = f"<@&{mention_role_id}>" if mention_role_id else ""
                await alert_channel.send(content=mention_content, embed=embed)

    async def _update_stats_channel(self, guild: discord.Guild, channel_id: int | None, new_name: str) -> None:
        if not channel_id:
            return
        guild_id = guild.id
        now = dt.datetime.now(dt.timezone.utc)
        
        # Check throttling
        last_update = self.last_channel_updates.get((guild_id, channel_id))
        if last_update is not None and (now - last_update) < dt.timedelta(minutes=10):
            return
            
        channel = guild.get_channel(channel_id)
        if not channel:
            return
            
        if channel.name == new_name:
            return
            
        try:
            await channel.edit(name=new_name)
            self.last_channel_updates[(guild_id, channel_id)] = now
            logger.info(f"Updated voice channel {channel_id} in guild {guild_id} to '{new_name}'")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to edit voice channel {channel_id} in guild {guild_id}")
        except discord.NotFound:
            logger.warning(f"Voice channel {channel_id} not found in guild {guild_id}")
        except Exception as e:
            logger.error(f"Failed to update voice channel {channel_id} in guild {guild_id}: {e}")

    @tasks.loop(minutes=1)
    async def daily_summary_loop(self) -> None:
        try:
            await self.bot.wait_until_ready()
            rows = await self.bot.db.fetchall("SELECT * FROM guild_settings WHERE summary_channel_id IS NOT NULL")
            for settings in rows:
                guild_id = int(settings["guild_id"])
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                
                # Check timezone and current time
                tz_name = settings["timezone"] or "UTC"
                try:
                    tz = zoneinfo.ZoneInfo(tz_name)
                except Exception:
                    tz = dt.timezone.utc
                
                now_local = dt.datetime.now(tz)
                current_time_str = now_local.strftime("%H:%M")
                current_date = now_local.date()
                
                # Check summary_time matches
                if current_time_str != settings["summary_time"]:
                    continue
                    
                # Already sent today?
                if self.last_summary_sent.get(guild_id) == current_date:
                    continue
                
                # Compile and send summary
                await self._send_daily_summary(guild, settings, current_date)
        except Exception as e:
            logger.error(f"Error in daily summary loop: {e}\n{traceback.format_exc()}")

    async def _send_daily_summary(self, guild: discord.Guild, settings: Any, current_date: dt.date) -> None:
        guild_id = guild.id
        summary_channel_id = settings["summary_channel_id"]
        
        # 1. Get channel
        channel = guild.get_channel(summary_channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(summary_channel_id)
            except Exception:
                logger.warning(f"Summary channel {summary_channel_id} not found/accessible in guild {guild_id}")
                return
                
        # 2. Gather data for the last 24 hours
        since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        since_iso = since_dt.isoformat()
        
        # - Active players
        active_players = await self.bot.db.get_active_players_summary(guild_id, since_iso)
        active_players_lines = []
        for idx, p in enumerate(active_players[:10], 1):
            duration_min = int(p["total_duration"] // 60)
            if duration_min >= 60:
                h = duration_min // 60
                m = duration_min % 60
                duration_str = f"{h}h {m}m"
            else:
                duration_str = f"{duration_min}m"
            active_players_lines.append(
                f"{idx}. [{p['current_name']}](https://www.battlemetrics.com/players/{p['battlemetrics_player_id']}) - **{duration_str}**"
            )
        active_players_text = "\n".join(active_players_lines) if active_players_lines else "No se registró actividad de jugadores en las últimas 24 horas."
        
        # - Name changes
        name_changes = await self.bot.db.get_name_changes_summary(guild_id, since_iso)
        changed_names_lines = []
        for row in name_changes:
            old_name = row["last_seen_name"]
            new_name = row["current_name"]
            pid = row["battlemetrics_player_id"]
            if old_name and old_name != new_name:
                changed_names_lines.append(
                    f"• `{old_name}` ➡️ **{new_name}** ([BM](https://www.battlemetrics.com/players/{pid}))"
                )
        name_changes_text = "\n".join(changed_names_lines[:10]) if changed_names_lines else "No se detectaron cambios de nombre en las últimas 24 horas."
        
        # - Tracked servers
        tracked_servers = await self.bot.db.list_tracked_servers(guild_id)
        server_status_lines = []
        for s in tracked_servers:
            status = f"**{s['name']}**\n"
            status += f"• 🗺️ Mapa: `{s['last_map'] or 'Desconocido'}`\n"
            status += f"• 👥 Jugadores: `{s['last_player_count'] or 0}/{s['last_max_players'] or 0}`"
            if s["last_queue"]:
                status += f" (Cola: `{s['last_queue']}`)"
            status += f"\n• 🔗 IP: `{s['last_ip'] or 'Desconocido'}:{s['last_port'] or 0}`"
            server_status_lines.append(status)
        server_status_text = "\n\n".join(server_status_lines) if server_status_lines else "No hay servidores configurados para monitoreo."
        
        # - Wipes estimation
        last_wipe_text = "Sin historial de wipes"
        next_wipe_prediction_text = "No hay datos suficientes para estimar"
        
        if tracked_servers:
            primary_server = tracked_servers[0]
            server_id = primary_server["battlemetrics_server_id"]
            
            wipes = await self.bot.db.get_wipe_history(guild_id, server_id, limit=20)
            if wipes:
                last_wipe = wipes[0]
                try:
                    last_wipe_dt = dt.datetime.fromisoformat(last_wipe["wiped_at"])
                    last_wipe_text = f"`{last_wipe_dt.strftime('%Y-%m-%d %H:%M UTC')}`"
                except Exception:
                    last_wipe_text = f"`{last_wipe['wiped_at']}`"
                    last_wipe_dt = None
                    
                if len(wipes) >= 2:
                    intervals = []
                    for i in range(len(wipes) - 1):
                        try:
                            dt1 = dt.datetime.fromisoformat(wipes[i]["wiped_at"])
                            dt2 = dt.datetime.fromisoformat(wipes[i+1]["wiped_at"])
                            diff = (dt1 - dt2).total_seconds() / 86400.0
                            intervals.append(diff)
                        except Exception:
                            continue
                    if intervals:
                        avg_days = sum(intervals) / len(intervals)
                        if last_wipe_dt:
                            next_wipe_dt = last_wipe_dt + dt.timedelta(days=avg_days)
                            next_wipe_prediction_text = f"`{next_wipe_dt.strftime('%Y-%m-%d %H:%M UTC')}` (promedio de `{avg_days:.1f}` días)"
                        else:
                            next_wipe_prediction_text = f"Promedio estimado: `{avg_days:.1f}` días desde el último wipe"
                    else:
                        next_wipe_prediction_text = "No se pudieron calcular los intervalos"
                else:
                    next_wipe_prediction_text = "Se requieren al menos 2 wipes registrados en el historial para estimar."
                    
        # 3. Create Embed
        embed = discord.Embed(
            title="📊 RESUMEN DIARIO DE ACTIVIDAD",
            description="Informe de monitoreo de las últimas 24 horas.",
            color=discord.Color.orange(),
            timestamp=dt.datetime.now(dt.timezone.utc)
        )
        embed.add_field(name="🖥️ Estado de Servidor(es)", value=server_status_text, inline=False)
        embed.add_field(name="👥 Jugadores más Activos (Últimas 24h)", value=active_players_text, inline=False)
        embed.add_field(name="🔄 Cambios de Nombre (Últimas 24h)", value=name_changes_text, inline=False)
        embed.add_field(name="✨ Último Wipe", value=last_wipe_text, inline=True)
        embed.add_field(name="🔮 Siguiente Wipe Estimado", value=next_wipe_prediction_text, inline=True)
        embed.set_footer(text="Rustalker Bot • Resumen automático")
        
        # Send
        try:
            await channel.send(embed=embed)
            self.last_summary_sent[guild_id] = current_date
            logger.info(f"Daily summary sent successfully to guild {guild_id}")
        except Exception as e:
            logger.error(f"Failed to send daily summary to guild {guild_id}: {e}")


async def setup(bot: RustalkerBot) -> None:
    await bot.add_cog(TrackerCog(bot))

from __future__ import annotations

import datetime as dt
import json
import asyncio
import io
import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
import traceback
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands
import matplotlib.pyplot as plt

from battlemetrics import parse_player_id, parse_server_id, BattleMetricsError

if TYPE_CHECKING:
    from main import RustalkerBot

logger = logging.getLogger("rustalker.commands")


@dataclass(slots=True)
class PlayerMatch:
    id: int
    name: str
    score: float = 0.0


class PlayerSelect(discord.ui.Select):
    def __init__(self, matches: list[PlayerMatch], on_pick: Callable[[discord.Interaction, PlayerMatch], Awaitable[None]]) -> None:
        options = [
            discord.SelectOption(
                label=match.name[:100],
                value=str(match.id),
                description=f"BM ID {match.id} | similitud {round(match.score * 100)}%",
            )
            for match in matches[:25]
        ]
        super().__init__(placeholder="Selecciona el jugador correcto", min_values=1, max_values=1, options=options)
        self.matches = {str(match.id): match for match in matches[:25]}
        self._on_pick = on_pick

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.matches.get(self.values[0])
        if selected is None:
            await interaction.response.send_message("❌ No se pudo resolver la selección.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._on_pick(interaction, selected)


class PlayerSelectView(discord.ui.View):
    def __init__(self, matches: list[PlayerMatch], on_pick: Callable[[discord.Interaction, PlayerMatch], Awaitable[None]], author_id: int) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.add_item(PlayerSelect(matches, on_pick))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo quien ejecutó el comando puede usar este selector.", ephemeral=True)
            return False
        return True


def _generate_chart(buckets: list[int], player_name: str) -> io.BytesIO:
    # Set dark background style
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5))
    
    hours = list(range(24))
    
    # Render bar chart
    ax.bar(hours, buckets, color='#2ecc71', edgecolor='#27ae60', alpha=0.8, width=0.7)
    
    ax.set_title(f"Perfil de Actividad - {player_name} (Últimos 14 días)", fontsize=13, pad=15, color='#f1c40f', weight='bold')
    ax.set_xlabel("Hora del Día (UTC)", fontsize=11, labelpad=10)
    ax.set_ylabel("Minutos Activos Totales", fontsize=11, labelpad=10)
    ax.set_xticks(hours)
    ax.set_xticklabels([f"{h:02d}:00" for h in hours], rotation=45, fontsize=9)
    
    ax.grid(True, linestyle='--', alpha=0.2, color='#7f8c8d')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#7f8c8d')
    ax.spines['bottom'].set_color('#7f8c8d')
    
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120)
    buf.seek(0)
    plt.close(fig)
    return buf


async def generate_chart_async(buckets: list[int], player_name: str) -> io.BytesIO:
    return await asyncio.to_thread(_generate_chart, buckets, player_name)


class CommandsCog(commands.Cog):
    def __init__(self, bot: RustalkerBot) -> None:
        self.bot = bot

    def _format_duration(self, seconds: int | None) -> str:
        if seconds is None:
            return "`Desconocido`"
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"**{hours}h {minutes}m {secs}s**"

    def _format_dt(self, value: str | None) -> str:
        if not value:
            return "`Desconocido`"
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return f"`{value}`"
        return f"`{parsed.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`"

    async def _search_player_matches(self, query: str) -> list[PlayerMatch]:
        try:
            results = await self.bot.bm_client.search_players(query, limit=10)
        except BattleMetricsError:
            results = []

        matches: list[PlayerMatch] = []
        query_norm = query.casefold().strip()
        seen_ids: set[int] = set()

        for item in results:
            try:
                pid = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            name = str(item.get("name") or f"Player {pid}")
            score = SequenceMatcher(None, query_norm, name.casefold()).ratio()
            if name.casefold() == query_norm:
                score = 1.0
            matches.append(PlayerMatch(id=pid, name=name, score=score))

        matches.sort(key=lambda m: (m.score, m.name.casefold()), reverse=True)

        if matches:
            return matches

        query_norm = query.casefold().strip()
        local_rows = await self.bot.db.fetchall(
            """
            SELECT battlemetrics_player_id, current_name
            FROM players
            WHERE LOWER(current_name) = ?
               OR LOWER(current_name) LIKE ?
            ORDER BY current_name COLLATE NOCASE ASC
            LIMIT 10
            """,
            (query_norm, f"%{query_norm}%"),
        )

        for row in local_rows:
            pid = int(row["battlemetrics_player_id"])
            name = str(row["current_name"])
            score = SequenceMatcher(None, query_norm, name.casefold()).ratio()
            if name.casefold() == query_norm:
                score = 1.0
            matches.append(PlayerMatch(id=pid, name=name, score=score))

        matches.sort(key=lambda m: (m.score, m.name.casefold()), reverse=True)
        return matches

    async def _fetch_player_profile(self, player_id: int) -> dict[str, Any] | None:
        try:
            return await self.bot.bm_client.get_player(player_id)
        except BattleMetricsError:
            return None

    async def _resolve_player_target(
        self,
        interaction: discord.Interaction,
        raw_target: str,
        on_resolved: Callable[[discord.Interaction, dict[str, Any]], Awaitable[None]],
        *,
        prompt_title: str,
    ) -> None:
        player_id = parse_player_id(raw_target)
        if player_id:
            profile = await self._fetch_player_profile(player_id)
            if profile is None:
                await interaction.followup.send("❌ No se pudo encontrar ese jugador en BattleMetrics.")
                return
            await on_resolved(interaction, profile)
            return

        matches = await self._search_player_matches(raw_target)
        exact_matches = [match for match in matches if match.name.casefold() == raw_target.casefold().strip()]

        if len(exact_matches) == 1:
            profile = await self._fetch_player_profile(exact_matches[0].id)
            if profile is None:
                await interaction.followup.send("❌ No se pudo encontrar ese jugador en BattleMetrics.")
                return
            await on_resolved(interaction, profile)
            return

        if not matches:
            await interaction.followup.send(f"❌ No encontré jugadores que coincidan con **{raw_target}**.")
            return

        if len(exact_matches) > 1:
            matches = exact_matches + [m for m in matches if m.id not in {e.id for e in exact_matches}]

        if len(matches) == 1:
            profile = await self._fetch_player_profile(matches[0].id)
            if profile is None:
                await interaction.followup.send("❌ No se pudo encontrar ese jugador en BattleMetrics.")
                return
            await on_resolved(interaction, profile)
            return

        async def _apply_selection(select_interaction: discord.Interaction, match: PlayerMatch) -> None:
            profile = await self._fetch_player_profile(match.id)
            if profile is None:
                await select_interaction.followup.send("❌ No se pudo cargar el perfil seleccionado.", ephemeral=True)
                return
            await on_resolved(select_interaction, profile)

        view = PlayerSelectView(matches[:5], _apply_selection, interaction.user.id)
        lines = [f"• **{match.name}** (`{match.id}`)" for match in matches[:5]]
        await interaction.followup.send(
            content=f"{prompt_title}\nCoincidencias encontradas:\n" + "\n".join(lines),
            view=view,
            ephemeral=True,
        )

    def _player_identity_fields(self, player: dict[str, Any]) -> tuple[str, str, str]:
        player_id = int(player["id"])
        name = str(player.get("name") or f"Player {player_id}")
        steam_id = player.get("steam_id")
        steam_value = f"[{steam_id}](https://steamcommunity.com/profiles/{steam_id})" if steam_id else "`No disponible`"
        bm_link = f"[{name}](https://www.battlemetrics.com/players/{player_id})"
        return bm_link, str(player_id), steam_value

    async def _send_player_profile(self, interaction: discord.Interaction, player: dict[str, Any]) -> None:
        guild_id = interaction.guild_id
        player_id = int(player["id"])
        name = str(player.get("name") or f"Player {player_id}")

        session_summary = await self.bot.db.get_player_session_summary(guild_id, player_id) if guild_id else None
        recent_sessions = await self.bot.db.get_recent_player_sessions(guild_id, player_id, limit=5) if guild_id else []
        latest_presence = await self.bot.db.get_latest_presence_snapshot(guild_id, player_id) if guild_id else None
        player_clans = await self.bot.db.get_player_clans(guild_id, player_id) if guild_id else []
        watch_entry = await self.bot.db.get_watch_player(guild_id, player_id) if guild_id else None

        bm_link, player_id_text, steam_value = self._player_identity_fields(player)
        attributes = player.get("attributes") or {}

        embed = discord.Embed(
            title="👤 PERFIL DE JUGADOR",
            description=f"Perfil de BattleMetrics para **{name}**.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="BattleMetrics", value=bm_link, inline=False)
        embed.add_field(name="ID", value=f"`{player_id_text}`", inline=True)
        embed.add_field(name="Steam ID", value=steam_value, inline=True)

        status_text = "`Desconocido`"
        last_online_text = "`Desconocido`"
        last_offline_text = "`Desconocido`"
        server_text = "`Desconocido`"
        if latest_presence:
            status_text = "🟢 Online" if int(latest_presence["is_online"]) == 1 else "🔴 Offline"
            server_text = f"**{latest_presence['server_name']}**"
            if latest_presence["last_seen_online_at"]:
                last_online_text = self._format_dt(latest_presence["last_seen_online_at"])

        if recent_sessions:
            latest_session = recent_sessions[0]
            if latest_session["started_at"]:
                last_online_text = self._format_dt(latest_session["started_at"])
            if latest_session["ended_at"]:
                last_offline_text = self._format_dt(latest_session["ended_at"])

        embed.add_field(name="Estado", value=status_text, inline=True)
        embed.add_field(name="Servidor", value=server_text, inline=True)
        embed.add_field(name="Última conexión", value=last_online_text, inline=True)
        embed.add_field(name="Última desconexión", value=last_offline_text, inline=True)

        if session_summary:
            total_seconds = int(session_summary["total_seconds"] or 0)
            session_count = int(session_summary["session_count"] or 0)
            embed.add_field(name="Sesiones", value=f"`{session_count}`", inline=True)
            embed.add_field(name="Tiempo jugado", value=self._format_duration(total_seconds), inline=True)
            embed.add_field(name="Primera sesión", value=self._format_dt(session_summary["first_started_at"]), inline=True)
            embed.add_field(name="Última sesión", value=self._format_dt(session_summary["last_started_at"]), inline=True)

        if watch_entry:
            watch_text = f"Sí, en watchlist\nNotas: {watch_entry['notes'] or 'Ninguna'}"
        else:
            watch_text = "No"
        embed.add_field(name="Watchlist", value=watch_text, inline=False)

        if player_clans:
            clan_names = "\n".join(f"• {row['name']}" for row in player_clans[:5])
        else:
            clan_names = "Ninguno"
        embed.add_field(name="Clanes", value=clan_names, inline=False)

        if recent_sessions:
            recent_lines = []
            for row in recent_sessions[:5]:
                duration = self._format_duration(row["duration_seconds"])
                end_value = self._format_dt(row["ended_at"])
                recent_lines.append(f"• {row['server_name']} | inicio {self._format_dt(row['started_at'])} | fin {end_value} | {duration}")
            embed.add_field(name="Sesiones recientes", value="\n".join(recent_lines), inline=False)

        raw_lines = []
        for key, value in attributes.items():
            if key in {"name"}:
                continue
            if isinstance(value, (str, int, float, bool)) and value not in {"", None}:
                raw_lines.append(f"• **{key}**: `{value}`")
        if raw_lines:
            embed.add_field(name="Datos BattleMetrics", value="\n".join(raw_lines[:10]), inline=False)

        raw_payload = player.get("raw") or {"attributes": attributes, "included": player.get("included") or []}
        raw_buffer = io.BytesIO(json.dumps(raw_payload, indent=2, ensure_ascii=False).encode("utf-8"))
        raw_file = discord.File(raw_buffer, filename=f"battlemetrics_player_{player_id}.json")

        await interaction.followup.send(embed=embed, file=raw_file)

    @app_commands.command(name="tuto", description="Muestra una guía rápida de uso de los comandos")
    async def tuto(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📘 GUÍA RÁPIDA DE RUSTALKER",
            description="Estos son los comandos principales y cómo usarlos.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )

        embed.add_field(
            name="🔧 Configuración inicial",
            value=(
                "`/setup_alerts channel [role_mention]`\n"
                "Define el canal donde llegarán las alertas.\n\n"
                "`/setup_rules spike_window spike_threshold queue_threshold`\n"
                "Ajusta los umbrales de alertas tácticas."
            ),
            inline=False
        )

        embed.add_field(
            name="🎯 Watchlist y servidores",
            value=(
                "`/watch target [notes] [custom_channel]`\n"
                "Añade un jugador a vigilancia. Usa `target` como nombre, ID o URL de BattleMetrics.\n\n"
                "`/unwatch player_id`\n"
                "Quita un jugador de la watchlist.\n\n"
                "`/watchlist`\n"
                "Muestra todos los jugadores vigilados.\n\n"
                "`/server_track target`\n"
                "Empieza a monitorear un servidor de BattleMetrics.\n\n"
                "`/server_untrack server_id`\n"
                "Detiene el monitoreo de un servidor."
            ),
            inline=False
        )

        embed.add_field(
            name="🏴 Clanes",
            value=(
                "`/clan create name`\n"
                "Crea un clan en este servidor.\n\n"
                "`/clan list`\n"
                "Lista los clanes guardados.\n\n"
                "`/clan add_member clan_name player_target`\n"
                "Añade un jugador a un clan.\n\n"
                "`/clan remove_member clan_name player_target`\n"
                "Elimina un miembro del clan."
            ),
            inline=False
        )

        embed.add_field(
            name="📈 Análisis",
            value=(
                "`/check_player target`\n"
                "Muestra el perfil completo del jugador y su historial.\n\n"
                "`/stats target [dias]`\n"
                "Genera un gráfico con la actividad de un jugador.\n\n"
                "`/raid_predictor [target_player] [clan_name]`\n"
                "Calcula la mejor ventana de inactividad para un jugador o un clan."
            ),
            inline=False
        )

        embed.add_field(
            name="💡 Tips",
            value=(
                "- Usa nombres, IDs o URLs de BattleMetrics cuando el comando lo pida.\n"
                "- `custom_channel` es opcional, pero útil si quieres alertas separadas.\n"
                "- La mayoría de comandos solo funcionan dentro de servidores."
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed)

    # --- CLAN SUBCOMMAND GROUP ---
    clan_group = app_commands.Group(name="clan", description="Gestión de clanes enemigos y alianzas")

    @clan_group.command(name="create", description="Crea un perfil de clan en este servidor de Discord")
    @app_commands.describe(name="Nombre único del clan")
    async def clan_create(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            existing = await self.bot.db.get_clan_by_name(guild_id, name)
            if existing:
                await interaction.followup.send(f"❌ Ya existe un clan con el nombre **{name}** en este servidor.")
                return

            await self.bot.db.create_clan(guild_id, name, interaction.user.id)
            await interaction.followup.send(f"✅ El clan **{name}** ha sido creado exitosamente.")
        except Exception as e:
            logger.error(f"Error in clan_create: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al crear el clan.")

    @clan_group.command(name="list", description="Lista todos los clanes enemigos registrados en este servidor")
    async def clan_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            clans = await self.bot.db.list_clans(guild_id)
            if not clans:
                await interaction.followup.send("ℹ️ No hay clanes registrados en este servidor de Discord. Crea uno con `/clan create`.")
                return

            embed = discord.Embed(
                title="🏴 CLANES ENEMIGOS REGISTRADOS",
                color=discord.Color.dark_grey(),
                timestamp=discord.utils.utcnow()
            )

            for c in clans:
                members_count = c["members"]
                embed.add_field(
                    name=f"Clan: {c['name']}",
                    value=f"• Miembros vigilados: `{members_count}`\n• Creado el: `{c['created_at'][:10]}`",
                    inline=False
                )

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in clan_list: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al listar los clanes.")

    @clan_group.command(name="add_member", description="Añade un jugador vigilado a un clan")
    @app_commands.describe(clan_name="Nombre del clan", player_target="Nombre, ID o URL del jugador de BattleMetrics")
    async def clan_add_member(self, interaction: discord.Interaction, clan_name: str, player_target: str) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            clan = await self.bot.db.get_clan_by_name(guild_id, clan_name)
            if not clan:
                await interaction.followup.send(f"❌ No se encontró el clan **{clan_name}**.")
                return

            async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
                player_id = int(player["id"])
                player_name = str(player.get("name") or f"Player {player_id}")

                await self.bot.db.upsert_player(player_id, player_name, player.get("steam_id"))
                await self.bot.db.add_clan_member(guild_id, clan["id"], player_id)
                await selected_interaction.followup.send(
                    f"✅ El jugador **{player_name}** (`{player_id}`) fue añadido al clan **{clan_name}**.",
                    ephemeral=True,
                )

            await self._resolve_player_target(
                interaction,
                player_target,
                _apply,
                prompt_title=f"Selecciona el jugador para añadir al clan **{clan_name}**.",
            )
        except Exception as e:
            logger.error(f"Error in clan_add_member: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al añadir al miembro al clan.")

    @clan_group.command(name="remove_member", description="Remueve un jugador de un clan")
    @app_commands.describe(clan_name="Nombre del clan", player_target="Nombre, ID o URL del jugador de BattleMetrics")
    async def clan_remove_member(self, interaction: discord.Interaction, clan_name: str, player_target: str) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            clan = await self.bot.db.get_clan_by_name(guild_id, clan_name)
            if not clan:
                await interaction.followup.send(f"❌ No se encontró el clan **{clan_name}**.")
                return

            async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
                player_id = int(player["id"])
                player_name = str(player.get("name") or f"Player {player_id}")

                removed = await self.bot.db.remove_clan_member(guild_id, clan["id"], player_id)
                if removed == 0:
                    await selected_interaction.followup.send(
                        f"❌ El jugador **{player_name}** no pertenece al clan **{clan_name}**.",
                        ephemeral=True,
                    )
                else:
                    await selected_interaction.followup.send(
                        f"✅ El jugador **{player_name}** (`{player_id}`) fue removido del clan **{clan_name}**.",
                        ephemeral=True,
                    )

            await self._resolve_player_target(
                interaction,
                player_target,
                _apply,
                prompt_title=f"Selecciona el jugador que quieres remover del clan **{clan_name}**.",
            )
        except Exception as e:
            logger.error(f"Error in clan_remove_member: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al remover al miembro.")

    # --- SETUP COMMANDS ---

    @app_commands.command(name="setup_alerts", description="Configura el canal para las alertas del rastreador")
    @app_commands.describe(
        channel="Canal de texto donde se publicarán las alertas",
        role_mention="Rol de Discord a mencionar en alertas de actividad de clanes (opcional)"
    )
    async def setup_alerts(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role_mention: discord.Role | None = None
    ) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            await self.bot.db.ensure_guild_settings(guild_id)
            await self.bot.db.set_alert_channel(guild_id, channel.id)
            await self.bot.db.set_mention_role(guild_id, role_mention.id if role_mention else None)
            
            msg = f"✅ Canal de alertas configurado en {channel.mention}."
            if role_mention:
                msg += f" Se mencionará al rol {role_mention.mention} en alertas críticas."
            await interaction.followup.send(msg)
        except Exception as e:
            logger.error(f"Error in setup_alerts: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al configurar las alertas.")

    @app_commands.command(name="setup_rules", description="Configura los umbrales de alertas tácticas")
    @app_commands.describe(
        spike_window="Ventana de tiempo en minutos para agrupamiento de conexiones (por defecto 15)",
        spike_threshold="Cantidad mínima de conexiones para disparar la alerta (por defecto 3)",
        queue_threshold="Avisar cuando la cola de espera de un servidor baje de este número (por defecto 5)"
    )
    async def setup_rules(
        self,
        interaction: discord.Interaction,
        spike_window: int = 15,
        spike_threshold: int = 3,
        queue_threshold: int = 5
    ) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            await self.bot.db.ensure_guild_settings(guild_id)
            await self.bot.db.set_clan_spike_rules(guild_id, spike_window, spike_threshold)
            await self.bot.db.set_queue_threshold(guild_id, queue_threshold)
            
            embed = discord.Embed(
                title="⚙️ CONFIGURACIÓN TÁCTICA ACTUALIZADA",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Minutos de Ventana de Clan", value=f"`{spike_window} min`", inline=True)
            embed.add_field(name="Umbral de Conexión de Clan", value=f"`{spike_threshold} miembros`", inline=True)
            embed.add_field(name="Umbral de Cola de Espera", value=f"`{queue_threshold} jugadores`", inline=True)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in setup_rules: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al configurar las reglas tácticas.")

    # --- WATCHLIST / SERVER TRACK COMMANDS ---

    @app_commands.command(name="server_track", description="Añade un servidor de BattleMetrics para monitorear")
    @app_commands.describe(target="ID o URL de BattleMetrics del servidor")
    async def server_track(self, interaction: discord.Interaction, target: str) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        server_id = parse_server_id(target)
        if not server_id:
            await interaction.followup.send("❌ ID o URL de servidor inválida.")
            return

        try:
            # Query server to verify it exists and get its name
            server_data = await self.bot.bm_client.get_server(server_id, include_players=False)
            server_name = server_data["name"]

            await self.bot.db.add_tracked_server(guild_id, server_id, server_name)
            await interaction.followup.send(f"✅ Servidor añadido al monitoreo: **{server_name}** (`{server_id}`)")
        except BattleMetricsError as e:
            await interaction.followup.send(f"❌ Error al consultar BattleMetrics: {e}")
        except Exception as e:
            logger.error(f"Error in server_track: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al añadir el servidor.")

    @app_commands.command(name="server_untrack", description="Detiene el monitoreo de un servidor")
    @app_commands.describe(server_id="ID numérico de BattleMetrics del servidor")
    async def server_untrack(self, interaction: discord.Interaction, server_id: int) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            count = await self.bot.db.remove_tracked_server(guild_id, server_id)
            if count == 0:
                await interaction.followup.send(f"❌ Este servidor de Discord no está rastreando el servidor `{server_id}`.")
            else:
                await interaction.followup.send(f"✅ Se ha detenido el monitoreo del servidor `{server_id}`.")
        except Exception as e:
            logger.error(f"Error in server_untrack: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al eliminar el servidor.")

    @app_commands.command(name="watch", description="Añade un jugador a la lista de vigilancia (Watchlist)")
    @app_commands.describe(
        target="Nombre, ID o URL de BattleMetrics del jugador",
        notes="Notas o comentarios sobre este objetivo (ej: 'Líder del clan Red')",
        custom_channel="Canal exclusivo para este jugador (por defecto el canal general)"
    )
    async def watch(
        self,
        interaction: discord.Interaction,
        target: str,
        notes: str | None = None,
        custom_channel: discord.TextChannel | None = None
    ) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
                player_id = int(player["id"])
                player_name = str(player.get("name") or f"Player {player_id}")

                settings = await self.bot.db.get_guild_settings(guild_id)
                if not settings or not settings["alert_channel_id"]:
                    if not custom_channel:
                        await selected_interaction.followup.send(
                            "⚠️ No has configurado un canal de alertas global. Usa `/setup_alerts` primero o especifica un `custom_channel`.",
                            ephemeral=True,
                        )
                        return

                await self.bot.db.upsert_player(player_id, player_name, player.get("steam_id"))
                await self.bot.db.add_watch_player(
                    guild_id=guild_id,
                    player_id=player_id,
                    notify_channel_id=custom_channel.id if custom_channel else None,
                    added_by=interaction.user.id,
                    notes=notes,
                )

                embed = discord.Embed(
                    title="🎯 NUEVO OBJETIVO AÑADIDO",
                    color=discord.Color.dark_green(),
                    timestamp=discord.utils.utcnow(),
                )
                bm_link, player_id_text, steam_val = self._player_identity_fields(player)
                embed.add_field(name="Jugador", value=bm_link, inline=True)
                embed.add_field(name="ID de BattleMetrics", value=f"`{player_id_text}`", inline=True)
                embed.add_field(name="Steam Link", value=steam_val, inline=True)

                if notes:
                    embed.add_field(name="Notas", value=f"*{notes}*", inline=False)
                embed.add_field(
                    name="Canal de Alertas",
                    value=custom_channel.mention if custom_channel else "`Canal General de Alertas`",
                    inline=False,
                )

                await selected_interaction.followup.send(embed=embed, ephemeral=True)

            await self._resolve_player_target(
                interaction,
                target,
                _apply,
                prompt_title="Selecciona el jugador que quieres añadir a watchlist.",
            )
        except BattleMetricsError as e:
            await interaction.followup.send(f"❌ Error al consultar BattleMetrics: {e}")
        except Exception as e:
            logger.error(f"Error in watch command: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al añadir al jugador.")

    @app_commands.command(name="unwatch", description="Elimina un jugador de la watchlist")
    @app_commands.describe(player_id="ID numérico de BattleMetrics del jugador")
    async def unwatch(self, interaction: discord.Interaction, player_id: int) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            count = await self.bot.db.remove_watch_player(guild_id, player_id)
            if count == 0:
                await interaction.followup.send(f"❌ El jugador `{player_id}` no está en la watchlist de este servidor.")
            else:
                await interaction.followup.send(f"✅ Se ha detenido la vigilancia del jugador `{player_id}`.")
        except Exception as e:
            logger.error(f"Error in unwatch: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al eliminar al jugador.")

    @app_commands.command(name="watchlist", description="Muestra la lista de jugadores vigilados y su estado actual")
    async def watchlist(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        try:
            watched = await self.bot.db.list_watch_players(guild_id)
            if not watched:
                await interaction.followup.send("ℹ️ No hay jugadores en la watchlist de este servidor. Usa `/watch` para añadir uno.")
                return

            embed = discord.Embed(
                title="🎯 LISTA DE OBJETIVOS VIGILADOS",
                color=discord.Color.dark_teal(),
                timestamp=discord.utils.utcnow()
            )

            for idx, r in enumerate(watched, 1):
                p_id = r["battlemetrics_player_id"]
                name = r["current_name"]
                clan = f" | Clan: **{r['clan_name']}**" if r["clan_name"] else ""
                
                # Check snapshot state
                snapshots = await self.bot.db.fetchall(
                    "SELECT is_online FROM presence_snapshots WHERE guild_id = ? AND battlemetrics_player_id = ?",
                    (guild_id, p_id)
                )
                
                online_text = "🔴 Offline"
                for snap in snapshots:
                    if snap["is_online"]:
                        online_text = "🟢 Online"
                        break

                embed.add_field(
                    name=f"{idx}. {name} ({online_text})",
                    value=f"• BM ID: `[{p_id}](https://www.battlemetrics.com/players/{p_id})`{clan}",
                    inline=False
                )

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in watchlist: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al obtener la watchlist.")

    @app_commands.command(name="check_player", description="Muestra el perfil completo de un jugador de BattleMetrics")
    @app_commands.describe(target="Nombre, ID o URL de BattleMetrics del jugador")
    async def check_player(self, interaction: discord.Interaction, target: str) -> None:
        await interaction.response.defer(ephemeral=True)

        async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
            await self._send_player_profile(selected_interaction, player)

        await self._resolve_player_target(
            interaction,
            target,
            _apply,
            prompt_title="Selecciona el jugador que quieres consultar.",
        )

    # --- TACTICAL INTELLIGENCE COMMANDS ---

    @app_commands.command(name="stats", description="Genera un gráfico con el perfil de actividad horaria de un jugador")
    @app_commands.describe(target="Nombre, ID o URL de BattleMetrics del jugador", dias="Días de historial a analizar (por defecto 14)")
    async def stats(self, interaction: discord.Interaction, target: str, dias: int = 14) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
            player_id = int(player["id"])
            player_name = str(player.get("name") or f"Jugador {player_id}")

            try:
                buckets = await self.bot.db.get_player_activity_by_hour(guild_id, player_id, days=dias)
                if sum(buckets) == 0:
                    await selected_interaction.followup.send(
                        f"ℹ️ Aún no hay sesiones registradas para **{player_name}** en los últimos {dias} días para poder graficar.",
                        ephemeral=True,
                    )
                    return

                chart_buffer = await generate_chart_async(buckets, player_name)
                discord_file = discord.File(chart_buffer, filename=f"stats_{player_id}.png")

                embed = discord.Embed(
                    title=f"📈 ANÁLISIS DE ACTIVIDAD: {player_name.upper()}",
                    description=f"Patrones de conexión recolectados en los últimos `{dias}` días.",
                    color=discord.Color.green(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_image(url=f"attachment://stats_{player_id}.png")
                embed.add_field(name="Tiempo de Juego Registrado", value=f"`{sum(buckets)} minutos` (~{round(sum(buckets)/60, 1)} horas)", inline=True)
                embed.add_field(name="ID Jugador", value=f"`{player_id}`", inline=True)
                embed.set_footer(text="Horario en formato internacional UTC")

                await selected_interaction.followup.send(file=discord_file, embed=embed)
            except Exception as e:
                logger.error(f"Error in stats: {e}\n{traceback.format_exc()}")
                await selected_interaction.followup.send("❌ Ocurrió un error al generar el gráfico de actividad.", ephemeral=True)

        await self._resolve_player_target(
            interaction,
            target,
            _apply,
            prompt_title="Selecciona el jugador para analizar su actividad.",
        )

    @app_commands.command(name="raid_predictor", description="Calcula la ventana óptima de inactividad para un jugador o clan entero")
    @app_commands.describe(
        target_player="Nombre, ID o URL del jugador (Opcional si eliges clan)",
        clan_name="Nombre del clan registrado en este servidor (Opcional si eliges jugador)"
    )
    async def raid_predictor(
        self,
        interaction: discord.Interaction,
        target_player: str | None = None,
        clan_name: str | None = None
    ) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.")
            return

        if not target_player and not clan_name:
            await interaction.followup.send("❌ Debes especificar un `target_player` (jugador) o un `clan_name` (clan) para analizar.")
            return

        try:
            async def _apply(selected_interaction: discord.Interaction, player: dict[str, Any]) -> None:
                player_id = int(player["id"])
                display_title = str(player.get("name") or f"Jugador {player_id}")

                player_ids = [player_id]
                aggregate_buckets = [0 for _ in range(24)]
                total_active_mins = 0

                for pid in player_ids:
                    buckets = await self.bot.db.get_player_activity_by_hour(guild_id, pid, days=14)
                    for h in range(24):
                        aggregate_buckets[h] += buckets[h]
                    total_active_mins += sum(buckets)

                if total_active_mins == 0:
                    await selected_interaction.followup.send(
                        f"ℹ️ No hay registros de conexiones suficientes para **{display_title}** en los últimos 14 días para realizar un análisis de sueño.",
                        ephemeral=True,
                    )
                    return

                best_window_start = 0
                min_window_sum = float('inf')
                double_buckets = aggregate_buckets + aggregate_buckets

                for h in range(24):
                    current_window_sum = sum(double_buckets[h : h + 6])
                    if current_window_sum < min_window_sum:
                        min_window_sum = current_window_sum
                        best_window_start = h

                best_window_hours = [(h % 24) for h in range(best_window_start, best_window_start + 6)]
                start_hour = best_window_hours[0]
                end_hour = (best_window_hours[-1] + 1) % 24
                window_str = f"**{start_hour:02d}:00 a {end_hour:02d}:00 UTC**"

                window_activity_mins = sum(aggregate_buckets[h] for h in best_window_hours)
                safety_ratio = 1.0 - (window_activity_mins / total_active_mins) if total_active_mins > 0 else 1.0
                safety_percentage = round(safety_ratio * 100, 1)

                if safety_percentage >= 95:
                    rating_str = "🟢 EXCELENTE (Casi 0% actividad registrada)"
                elif safety_percentage >= 80:
                    rating_str = "🟡 ALTA (Muy poca actividad)"
                elif safety_percentage >= 50:
                    rating_str = "🟠 MODERADA (Se registra algo de actividad esporádica)"
                else:
                    rating_str = "🔴 CRÍTICA/BAJA (Múltiples conexiones esporádicas en esta ventana)"

                embed = discord.Embed(
                    title="🕵️‍♂️ INFORME TÁCTICO: PREDICTOR DE RAID",
                    description=f"Ventana óptima para realizar un **Offline Raid** a **{display_title}**.",
                    color=discord.Color.dark_magenta(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="🎯 Objetivo Analizado", value=display_title, inline=True)
                embed.add_field(name="⏳ Período de Rastreo", value="`Últimos 14 días`", inline=True)
                embed.add_field(name="💤 Ventana de Inactividad Óptima", value=window_str, inline=False)
                embed.add_field(name="🛡️ Calificación de Seguridad", value=rating_str, inline=False)
                embed.add_field(name="📊 Minutos Activos en esta Ventana", value=f"`{window_activity_mins} min` de un total de `{total_active_mins} min` de juego", inline=True)
                embed.set_footer(text="Horarios calculados en base a sesiones recolectadas en formato UTC")

                await selected_interaction.followup.send(embed=embed)

            if target_player:
                await self._resolve_player_target(
                    interaction,
                    target_player,
                    _apply,
                    prompt_title="Selecciona el jugador que quieres analizar para raid predictor.",
                )
                return

            assert clan_name is not None
            clan = await self.bot.db.get_clan_by_name(guild_id, clan_name)
            if not clan:
                await interaction.followup.send(f"❌ No se encontró el clan **{clan_name}**.")
                return

            clan_m_ids = await self.bot.db.get_clan_member_ids(guild_id, clan["id"])
            if not clan_m_ids:
                await interaction.followup.send(f"❌ El clan **{clan_name}** no tiene miembros agregados.")
                return

            player_ids = list(clan_m_ids)
            aggregate_buckets = [0 for _ in range(24)]
            total_active_mins = 0

            for pid in player_ids:
                buckets = await self.bot.db.get_player_activity_by_hour(guild_id, pid, days=14)
                for h in range(24):
                    aggregate_buckets[h] += buckets[h]
                total_active_mins += sum(buckets)

            if total_active_mins == 0:
                await interaction.followup.send(f"ℹ️ No hay registros de conexiones suficientes para **Clan '{clan_name}'** en los últimos 14 días para realizar un análisis de sueño.")
                return

            best_window_start = 0
            min_window_sum = float('inf')
            double_buckets = aggregate_buckets + aggregate_buckets

            for h in range(24):
                current_window_sum = sum(double_buckets[h : h + 6])
                if current_window_sum < min_window_sum:
                    min_window_sum = current_window_sum
                    best_window_start = h

            best_window_hours = [(h % 24) for h in range(best_window_start, best_window_start + 6)]
            start_hour = best_window_hours[0]
            end_hour = (best_window_hours[-1] + 1) % 24
            window_str = f"**{start_hour:02d}:00 a {end_hour:02d}:00 UTC**"

            window_activity_mins = sum(aggregate_buckets[h] for h in best_window_hours)
            safety_ratio = 1.0 - (window_activity_mins / total_active_mins) if total_active_mins > 0 else 1.0
            safety_percentage = round(safety_ratio * 100, 1)

            if safety_percentage >= 95:
                rating_str = "🟢 EXCELENTE (Casi 0% actividad registrada)"
            elif safety_percentage >= 80:
                rating_str = "🟡 ALTA (Muy poca actividad)"
            elif safety_percentage >= 50:
                rating_str = "🟠 MODERADA (Se registra algo de actividad esporádica)"
            else:
                rating_str = "🔴 CRÍTICA/BAJA (Múltiples conexiones esporádicas en esta ventana)"

            embed = discord.Embed(
                title="🕵️‍♂️ INFORME TÁCTICO: PREDICTOR DE RAID",
                description=f"Ventana óptima para realizar un **Offline Raid** al clan **{clan_name}**.",
                color=discord.Color.dark_magenta(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="🎯 Objetivo Analizado", value=f"Clan '{clan_name}'", inline=True)
            embed.add_field(name="⏳ Período de Rastreo", value="`Últimos 14 días`", inline=True)
            embed.add_field(name="💤 Ventana de Inactividad Óptima", value=window_str, inline=False)
            embed.add_field(name="🛡️ Calificación de Seguridad", value=rating_str, inline=False)
            embed.add_field(name="📊 Minutos Activos en esta Ventana", value=f"`{window_activity_mins} min` de un total de `{total_active_mins} min` de juego", inline=True)
            embed.set_footer(text="Horarios calculados en base a sesiones recolectadas en formato UTC")

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in raid_predictor: {e}\n{traceback.format_exc()}")
            await interaction.followup.send("❌ Ocurrió un error al predecir la ventana de raid.")


async def setup(bot: RustalkerBot) -> None:
    await bot.add_cog(CommandsCog(bot))

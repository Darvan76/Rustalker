from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

# Importación opcional de rustplus — si no está instalado el cog se carga igualmente
# pero todos los comandos responderán con un aviso de instalación.
try:
    from rustplus import RustSocket, ServerDetails, EntityEvent, ChatEvent
    from rustplus import EntityEventPayload, ChatEventPayload
    RUSTPLUS_AVAILABLE = True
except ImportError:
    RUSTPLUS_AVAILABLE = False

if TYPE_CHECKING:
    from main import RustalkerBot

logger = logging.getLogger("rustalker.rustplus")

# ---------------------------------------------------------------------------
# Colores de embed estandarizados
# ---------------------------------------------------------------------------
COLOR_OK      = discord.Color.green()       # Conexión / éxito
COLOR_ERROR   = discord.Color.red()         # Error / alarma activa
COLOR_WARNING = discord.Color.orange()      # Advertencia / alarma inactiva
COLOR_INFO    = discord.Color.blue()        # Información general
COLOR_GOLD    = discord.Color.gold()        # Chat relay / team info



def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _not_available_embed() -> discord.Embed:
    """Embed de error cuando rustplus no está instalado."""
    return discord.Embed(
        title="❌ rustplus no está instalado",
        description=(
            "La librería `rustplus` no está disponible en este entorno.\n"
            "Instálala con:\n```\npip install rustplus\n```"
        ),
        color=COLOR_ERROR,
    )


# ---------------------------------------------------------------------------
# RustPlusManager — gestiona los sockets activos
# ---------------------------------------------------------------------------

class RustPlusManager:
    """
    Mantiene un dict de RustSocket activos indexados por pairing_id.
    Gestiona el ciclo de vida completo: conectar, suscribir eventos, desconectar.
    """

    def __init__(self, bot: RustalkerBot) -> None:
        self._sockets: dict[int, RustSocket] = {}
        self._bot = bot

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------

    async def connect_pairing(
        self,
        pairing_row: Any,
        cog: RustPlusCog,
    ) -> bool:
        """
        Conecta el socket para el pairing dado.
        Suscribe los handlers de alarma y chat usando los decoradores de rustplus.

        Returns True si la conexión fue exitosa, False en caso de error.
        """
        if not RUSTPLUS_AVAILABLE:
            return False

        pairing_id = int(pairing_row["id"])

        # Si ya existe un socket activo, no reconectamos
        if pairing_id in self._sockets:
            logger.debug("Pairing %d ya tiene socket activo, omitiendo.", pairing_id)
            return True

        ip           = pairing_row["ip"]
        port         = int(pairing_row["port"])
        steam_id     = pairing_row["steam_id"]
        player_token = int(pairing_row["player_token"])

        try:
            details = ServerDetails(ip, port, steam_id, player_token)
            socket  = RustSocket(details)
            await socket.connect()
            logger.info("RustSocket conectado para pairing %d (%s:%d)", pairing_id, ip, port)
        except Exception as exc:
            logger.error(
                "Error al conectar RustSocket para pairing %d: %s\n%s",
                pairing_id, exc, traceback.format_exc(),
            )
            # Marcar pairing como inactivo en la DB para no reintentar en cada arranque
            try:
                await self._bot.db.execute(
                    "UPDATE rustplus_pairings SET is_active = 0, updated_at = ? WHERE id = ?",
                    (_utc_now(), pairing_id),
                )
            except Exception:
                pass
            return False

        self._sockets[pairing_id] = socket

        # Suscribir ChatEvent
        try:
            @ChatEvent(details)
            async def on_chat(event: ChatEventPayload) -> None:  # type: ignore[name-defined]
                try:
                    row = await self._bot.db.fetchone(
                        "SELECT * FROM rustplus_pairings WHERE id = ?", (pairing_id,)
                    )
                    if row:
                        await cog.on_rustplus_chat(
                            pairing_id=pairing_id,
                            message_name=event.name,
                            message_text=event.message,
                            pairing_row=row,
                        )
                except Exception as e:
                    logger.error("Error en ChatEvent handler (pairing %d): %s", pairing_id, e)
        except Exception as e:
            logger.warning("No se pudo registrar ChatEvent para pairing %d: %s", pairing_id, e)

        # Suscribir EntityEvent por cada alarma registrada
        await self._subscribe_alarms(pairing_id, details, cog)

        return True

    async def _subscribe_alarms(
        self,
        pairing_id: int,
        details: ServerDetails,
        cog: RustPlusCog,
    ) -> None:
        """Registra un handler de EntityEvent por cada alarma del pairing."""
        try:
            alarms = await self._bot.db.fetchall(
                "SELECT * FROM rustplus_alarms WHERE pairing_id = ?", (pairing_id,)
            )
        except Exception as e:
            logger.error("Error al obtener alarmas para pairing %d: %s", pairing_id, e)
            return

        for alarm_row in alarms:
            entity_id = int(alarm_row["entity_id"])
            try:
                @EntityEvent(details, entity_id)
                async def on_entity(event: EntityEventPayload, _pid=pairing_id, _aid=int(alarm_row["id"])) -> None:  # type: ignore[name-defined]
                    try:
                        p_row = await self._bot.db.fetchone(
                            "SELECT * FROM rustplus_pairings WHERE id = ?", (_pid,)
                        )
                        a_row = await self._bot.db.fetchone(
                            "SELECT * FROM rustplus_alarms WHERE id = ?", (_aid,)
                        )
                        if p_row and a_row:
                            await cog.on_rustplus_alarm(
                                pairing_id=_pid,
                                entity_id=entity_id,
                                is_active=event.value,
                                pairing_row=p_row,
                                alarm_row=a_row,
                            )
                    except Exception as e:
                        logger.error("Error en EntityEvent handler (pairing %d, entity %d): %s", _pid, entity_id, e)
            except Exception as e:
                logger.warning(
                    "No se pudo registrar EntityEvent para pairing %d, entity %d: %s",
                    pairing_id, entity_id, e,
                )

    # ------------------------------------------------------------------
    # Desconexión
    # ------------------------------------------------------------------

    async def disconnect_pairing(self, pairing_id: int) -> None:
        """Desconecta y elimina el socket del pairing indicado."""
        socket = self._sockets.pop(pairing_id, None)
        if socket is None:
            return
        try:
            await socket.disconnect()
            logger.info("RustSocket desconectado para pairing %d", pairing_id)
        except Exception as e:
            logger.warning("Error al desconectar pairing %d: %s", pairing_id, e)

    async def disconnect_all(self) -> None:
        """Desconecta todos los sockets activos (llamado en cog_unload)."""
        ids = list(self._sockets.keys())
        for pid in ids:
            await self.disconnect_pairing(pid)

    # ------------------------------------------------------------------
    # Consultas de estado
    # ------------------------------------------------------------------

    def is_connected(self, pairing_id: int) -> bool:
        return pairing_id in self._sockets

    def get_socket(self, pairing_id: int) -> RustSocket | None:
        return self._sockets.get(pairing_id)


# ---------------------------------------------------------------------------
# RustPlusCog — cog principal
# ---------------------------------------------------------------------------

class RustPlusCog(commands.Cog):
    """
    Cog de Discord para integración con Rust+ (rustplus.py).
    Gestiona conexiones a servidores Rust, alarmas inteligentes,
    smart switches y relay de chat de equipo.
    """

    def __init__(self, bot: RustalkerBot) -> None:
        self.bot     = bot
        self.manager = RustPlusManager(bot)

    # ------------------------------------------------------------------
    # Ciclo de vida del cog
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        """
        Al cargar el cog:
        Conecta todos los pairings activos de todos los guilds.
        El esquema de tablas ya está garantizado por database.py._create_schema().
        """
        logger.info("Cargando RustPlusCog...")

        if not RUSTPLUS_AVAILABLE:
            logger.warning(
                "rustplus no está instalado. El cog se carga pero los sockets no se conectarán."
            )
            return

        # Conectar todos los pairings activos
        all_pairings = await self.bot.db.fetchall(
            "SELECT * FROM rustplus_pairings WHERE is_active = 1"
        )
        logger.info("Conectando %d pairings activos de Rust+...", len(all_pairings))

        for pairing_row in all_pairings:
            # Corremos cada conexión de forma concurrente para no bloquear el arranque
            asyncio.create_task(
                self._safe_connect(pairing_row),
                name=f"rustplus_connect_{pairing_row['id']}",
            )

    async def _safe_connect(self, pairing_row: Any) -> None:
        """Envuelve connect_pairing con manejo de errores para uso con create_task."""
        try:
            await self.manager.connect_pairing(pairing_row, cog=self)
        except Exception as e:
            logger.error(
                "Error inesperado al conectar pairing %d: %s\n%s",
                pairing_row["id"], e, traceback.format_exc(),
            )

    def cog_unload(self) -> None:
        """Al descargar el cog, desconecta todos los sockets limpiamente."""
        logger.info("Descargando RustPlusCog, desconectando sockets...")
        asyncio.create_task(self.manager.disconnect_all(), name="rustplus_disconnect_all")

    # ------------------------------------------------------------------
    # Handlers internos de eventos Rust+
    # ------------------------------------------------------------------

    async def on_rustplus_alarm(
        self,
        pairing_id: int,
        entity_id: int,
        is_active: bool,
        pairing_row: Any,
        alarm_row: Any,
    ) -> None:
        """
        Llamado cuando una alarma inteligente cambia de estado.
        Envía un embed al canal configurado para alarmas.
        """
        # Determinar el canal de destino: primero el canal específico de la alarma,
        # luego el canal de alarmas del pairing.
        channel_id: int | None = alarm_row["channel_id"] or pairing_row["alarm_channel_id"]
        if not channel_id:
            logger.debug(
                "Alarma %s disparada en pairing %d pero no hay canal configurado.",
                alarm_row["label"], pairing_id,
            )
            return

        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Canal de alarma %d no encontrado o no es TextChannel.", channel_id)
            return

        label  = alarm_row["label"]
        server = pairing_row["server_name"]
        color  = COLOR_ERROR if is_active else COLOR_WARNING
        estado = "🔴 ACTIVADA" if is_active else "🟢 Desactivada"
        icon   = "🚨" if is_active else "✅"

        embed = discord.Embed(
            title=f"{icon} Alarma: {label}",
            description=(
                f"**Estado:** {estado}\n"
                f"**Servidor:** {server}\n"
                f"**Entity ID:** `{entity_id}`"
            ),
            color=color,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text=f"Rust+ • Pairing #{pairing_id}")

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.error("Error al enviar embed de alarma al canal %d: %s", channel_id, e)

    async def on_rustplus_chat(
        self,
        pairing_id: int,
        message_name: str,
        message_text: str,
        pairing_row: Any,
    ) -> None:
        """
        Llamado cuando llega un mensaje de chat de equipo desde Rust+.
        Hace relay al canal de chat configurado.
        """
        channel_id: int | None = pairing_row["chat_channel_id"]
        if not channel_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return

        server = pairing_row["server_name"]
        embed  = discord.Embed(
            description=(
                f"**{discord.utils.escape_markdown(message_name)}:** "
                f"{discord.utils.escape_markdown(message_text)}"
            ),
            color=COLOR_GOLD,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_author(name=f"💬 Team Chat — {server}")
        embed.set_footer(text=f"Rust+ • Pairing #{pairing_id}")

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.error("Error al enviar embed de chat al canal %d: %s", channel_id, e)

    # ------------------------------------------------------------------
    # Grupo de comandos slash /rustplus
    # ------------------------------------------------------------------

    rustplus = app_commands.Group(
        name="rustplus",
        description="Comandos de integración con Rust+",
    )

    # ---- /rustplus pair -----------------------------------------------

    @rustplus.command(name="pair", description="Vincula un servidor de Rust con este Discord via Rust+")
    @app_commands.describe(
        ip="IP del servidor de Rust",
        port="Puerto del servidor (por defecto 28082)",
        steam_id="Tu Steam ID de 64 bits",
        player_token="Token de jugador de Rust+ (obtenido desde la app oficial)",
        server_name="Nombre descriptivo para este servidor",
        alarm_channel="Canal de Discord para notificaciones de alarmas",
        chat_channel="Canal de Discord para relay del chat de equipo",
    )
    @app_commands.guild_only()
    async def cmd_pair(
        self,
        interaction: discord.Interaction,
        ip: str,
        port: int,
        steam_id: str,
        player_token: str,
        server_name: str = "Rust Server",
        alarm_channel: discord.TextChannel | None = None,
        chat_channel: discord.TextChannel | None = None,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        now        = _utc_now()
        alarm_ch_id = alarm_channel.id if alarm_channel else None
        chat_ch_id  = chat_channel.id  if chat_channel  else None

        try:
            assert self.bot.db.conn is not None
            cursor = await self.bot.db.conn.execute(
                """
                INSERT INTO rustplus_pairings
                    (guild_id, server_name, ip, port, steam_id, player_token,
                     alarm_channel_id, chat_channel_id, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    interaction.guild_id, server_name, ip, port,
                    steam_id, player_token,
                    alarm_ch_id, chat_ch_id,
                    now, now,
                ),
            )
            await self.bot.db.conn.commit()
            pairing_id = cursor.lastrowid
            await cursor.close()
        except Exception as e:
            logger.error("Error al guardar pairing en DB: %s\n%s", e, traceback.format_exc())
            embed = discord.Embed(
                title="❌ Error al guardar el pairing",
                description=f"```\n{e}\n```",
                color=COLOR_ERROR,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Obtener el row recién insertado y conectar el socket
        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ?", (pairing_id,)
        )
        connected = False
        if pairing_row:
            connected = await self.manager.connect_pairing(pairing_row, cog=self)

        status_icon = "🟢 Conectado" if connected else "🔴 Sin conexión"
        embed = discord.Embed(
            title="✅ Pairing registrado",
            color=COLOR_OK if connected else COLOR_WARNING,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="🆔 Pairing ID", value=f"`{pairing_id}`", inline=True)
        embed.add_field(name="🖥️ Servidor",   value=server_name,       inline=True)
        embed.add_field(name="🌐 Dirección",  value=f"`{ip}:{port}`",  inline=True)
        embed.add_field(name="📡 Estado",     value=status_icon,       inline=True)
        if alarm_channel:
            embed.add_field(name="🔔 Canal alarmas", value=alarm_channel.mention, inline=True)
        if chat_channel:
            embed.add_field(name="💬 Canal chat",    value=chat_channel.mention,  inline=True)
        embed.set_footer(text="Usa /rustplus list para ver todos los pairings")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus unpair --------------------------------------------

    @rustplus.command(name="unpair", description="Elimina un pairing de Rust+ y desconecta el socket")
    @app_commands.describe(pairing_id="ID del pairing a eliminar (ver /rustplus list)")
    @app_commands.guild_only()
    async def cmd_unpair(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ? AND guild_id = ?",
            (pairing_id, interaction.guild_id),
        )
        if not pairing_row:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Pairing no encontrado",
                    description=f"No existe ningún pairing con ID `{pairing_id}` en este servidor.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Desconectar el socket si estaba activo
        await self.manager.disconnect_pairing(pairing_id)

        # Eliminar de la DB (cascade borra alarmas y switches asociados)
        await self.bot.db.execute(
            "DELETE FROM rustplus_pairings WHERE id = ?", (pairing_id,)
        )

        embed = discord.Embed(
            title="🗑️ Pairing eliminado",
            description=(
                f"El pairing **{pairing_row['server_name']}** "
                f"(`{pairing_row['ip']}:{pairing_row['port']}`) "
                f"con ID `{pairing_id}` ha sido eliminado y el socket desconectado."
            ),
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus list ----------------------------------------------

    @rustplus.command(name="list", description="Lista todos los pairings de Rust+ de este servidor")
    @app_commands.guild_only()
    async def cmd_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pairings = await self.bot.db.fetchall(
            "SELECT * FROM rustplus_pairings WHERE guild_id = ? ORDER BY id",
            (interaction.guild_id,),
        )

        if not pairings:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="📋 Sin pairings registrados",
                    description="Usa `/rustplus pair` para vincular un servidor de Rust.",
                    color=COLOR_INFO,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 Pairings de Rust+",
            color=COLOR_INFO,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

        for row in pairings:
            pid        = int(row["id"])
            connected  = self.manager.is_connected(pid)
            status_dot = "🟢" if connected else "🔴"
            active_txt = "Activo" if row["is_active"] else "Inactivo en DB"
            embed.add_field(
                name=f"{status_dot} [{pid}] {row['server_name']}",
                value=(
                    f"`{row['ip']}:{row['port']}`\n"
                    f"Estado: **{active_txt}** | Socket: **{'Conectado' if connected else 'Desconectado'}**"
                ),
                inline=False,
            )

        embed.set_footer(text=f"{len(pairings)} pairing(s) en total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus alarm_add -----------------------------------------

    @rustplus.command(name="alarm_add", description="Registra una alarma inteligente de Rust+")
    @app_commands.describe(
        pairing_id="ID del pairing al que pertenece esta alarma",
        entity_id="ID de la entidad (Smart Alarm) en el servidor de Rust",
        label="Nombre descriptivo para la alarma",
        channel="Canal donde se enviarán las notificaciones (opcional)",
    )
    @app_commands.guild_only()
    async def cmd_alarm_add(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
        entity_id: int,
        label: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ? AND guild_id = ?",
            (pairing_id, interaction.guild_id),
        )
        if not pairing_row:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Pairing no encontrado", color=COLOR_ERROR),
                ephemeral=True,
            )
            return

        now   = _utc_now()
        ch_id = channel.id if channel else None

        try:
            assert self.bot.db.conn is not None
            cursor = await self.bot.db.conn.execute(
                """
                INSERT INTO rustplus_alarms (pairing_id, guild_id, entity_id, label, channel_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pairing_id, interaction.guild_id, entity_id, label, ch_id, now),
            )
            await self.bot.db.conn.commit()
            alarm_id = cursor.lastrowid
            await cursor.close()
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al registrar alarma",
                    description=f"```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Intentar consultar la entity en el socket activo
        socket    = self.manager.get_socket(pairing_id)
        info_text = ""
        if RUSTPLUS_AVAILABLE and socket:
            try:
                info = await socket.get_entity_info(entity_id)
                info_text = f"\n📡 Entity info: `{info}`"
            except Exception as e:
                info_text = f"\n⚠️ No se pudo consultar la entity: `{e}`"
        elif not self.manager.is_connected(pairing_id):
            info_text = "\n⚠️ El socket no está conectado. La alarma se activará cuando se reconecte."

        embed = discord.Embed(
            title="🔔 Alarma registrada",
            description=(
                f"**Label:** {label}\n"
                f"**Entity ID:** `{entity_id}`\n"
                f"**Alarm ID:** `{alarm_id}`\n"
                f"**Canal:** {channel.mention if channel else 'Canal del pairing'}"
                + info_text
            ),
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text=f"Pairing #{pairing_id} — {pairing_row['server_name']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus alarm_remove --------------------------------------

    @rustplus.command(name="alarm_remove", description="Elimina una alarma registrada")
    @app_commands.describe(alarm_id="ID de la alarma a eliminar (ver /rustplus alarm_list)")
    @app_commands.guild_only()
    async def cmd_alarm_remove(
        self,
        interaction: discord.Interaction,
        alarm_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_alarms WHERE id = ? AND guild_id = ?",
            (alarm_id, interaction.guild_id),
        )
        if not row:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Alarma no encontrada",
                    description=f"No existe ninguna alarma con ID `{alarm_id}` en este servidor.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute("DELETE FROM rustplus_alarms WHERE id = ?", (alarm_id,))

        embed = discord.Embed(
            title="🗑️ Alarma eliminada",
            description=f"La alarma **{row['label']}** (Entity ID `{row['entity_id']}`) ha sido eliminada.",
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus alarm_list ----------------------------------------

    @rustplus.command(name="alarm_list", description="Lista las alarmas registradas para un pairing")
    @app_commands.describe(pairing_id="ID del pairing")
    @app_commands.guild_only()
    async def cmd_alarm_list(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ? AND guild_id = ?",
            (pairing_id, interaction.guild_id),
        )
        if not pairing_row:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Pairing no encontrado", color=COLOR_ERROR),
                ephemeral=True,
            )
            return

        alarms = await self.bot.db.fetchall(
            "SELECT * FROM rustplus_alarms WHERE pairing_id = ? ORDER BY id",
            (pairing_id,),
        )

        embed = discord.Embed(
            title=f"🔔 Alarmas — {pairing_row['server_name']}",
            color=COLOR_INFO,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

        if not alarms:
            embed.description = "No hay alarmas registradas. Usa `/rustplus alarm_add` para añadir una."
        else:
            for a in alarms:
                ch_mention = f"<#{a['channel_id']}>" if a["channel_id"] else "Canal del pairing"
                embed.add_field(
                    name=f"[{a['id']}] {a['label']}",
                    value=f"Entity ID: `{a['entity_id']}` | Canal: {ch_mention}",
                    inline=False,
                )

        embed.set_footer(text=f"Pairing #{pairing_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus switch_add ----------------------------------------

    @rustplus.command(name="switch_add", description="Registra un Smart Switch de Rust+")
    @app_commands.describe(
        pairing_id="ID del pairing",
        entity_id="ID de la entidad (Smart Switch) en el servidor",
        label="Nombre descriptivo para el switch",
    )
    @app_commands.guild_only()
    async def cmd_switch_add(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
        entity_id: int,
        label: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ? AND guild_id = ?",
            (pairing_id, interaction.guild_id),
        )
        if not pairing_row:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Pairing no encontrado", color=COLOR_ERROR),
                ephemeral=True,
            )
            return

        now = _utc_now()
        try:
            assert self.bot.db.conn is not None
            cursor = await self.bot.db.conn.execute(
                """
                INSERT INTO rustplus_switches (pairing_id, guild_id, entity_id, label, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pairing_id, interaction.guild_id, entity_id, label, now),
            )
            await self.bot.db.conn.commit()
            switch_id = cursor.lastrowid
            await cursor.close()
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al registrar switch",
                    description=f"```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔌 Switch registrado",
            description=(
                f"**Label:** {label}\n"
                f"**Entity ID:** `{entity_id}`\n"
                f"**Switch ID:** `{switch_id}`"
            ),
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text=f"Pairing #{pairing_id} — {pairing_row['server_name']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus switch_remove -------------------------------------

    @rustplus.command(name="switch_remove", description="Elimina un Smart Switch registrado")
    @app_commands.describe(switch_id="ID del switch a eliminar")
    @app_commands.guild_only()
    async def cmd_switch_remove(
        self,
        interaction: discord.Interaction,
        switch_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_switches WHERE id = ? AND guild_id = ?",
            (switch_id, interaction.guild_id),
        )
        if not row:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Switch no encontrado",
                    description=f"No existe ningún switch con ID `{switch_id}` en este servidor.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute("DELETE FROM rustplus_switches WHERE id = ?", (switch_id,))

        embed = discord.Embed(
            title="🗑️ Switch eliminado",
            description=f"El switch **{row['label']}** (Entity ID `{row['entity_id']}`) ha sido eliminado.",
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus switch_on -----------------------------------------

    @rustplus.command(name="switch_on", description="Enciende un Smart Switch en el servidor de Rust")
    @app_commands.describe(
        pairing_id="ID del pairing",
        entity_id="ID de la entidad (Smart Switch)",
    )
    @app_commands.guild_only()
    async def cmd_switch_on(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
        entity_id: int,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        socket = self.manager.get_socket(pairing_id)
        if socket is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Socket no conectado",
                    description=f"El pairing `{pairing_id}` no tiene socket activo. Usa `/rustplus reconnect`.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        try:
            await socket.turn_on_smart_switch(entity_id)
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al encender switch",
                    description=f"Entity ID `{entity_id}`\n```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔌 Switch encendido",
            description=f"La entidad `{entity_id}` del pairing `{pairing_id}` ha sido **encendida** ✅",
            color=COLOR_OK,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus switch_off ----------------------------------------

    @rustplus.command(name="switch_off", description="Apaga un Smart Switch en el servidor de Rust")
    @app_commands.describe(
        pairing_id="ID del pairing",
        entity_id="ID de la entidad (Smart Switch)",
    )
    @app_commands.guild_only()
    async def cmd_switch_off(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
        entity_id: int,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        socket = self.manager.get_socket(pairing_id)
        if socket is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Socket no conectado",
                    description=f"El pairing `{pairing_id}` no tiene socket activo. Usa `/rustplus reconnect`.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        try:
            await socket.turn_off_smart_switch(entity_id)
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al apagar switch",
                    description=f"Entity ID `{entity_id}`\n```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔌 Switch apagado",
            description=f"La entidad `{entity_id}` del pairing `{pairing_id}` ha sido **apagada** ⭕",
            color=COLOR_WARNING,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus server_info ---------------------------------------

    @rustplus.command(name="server_info", description="Muestra información del servidor de Rust")
    @app_commands.describe(pairing_id="ID del pairing")
    @app_commands.guild_only()
    async def cmd_server_info(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        socket = self.manager.get_socket(pairing_id)
        if socket is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Socket no conectado",
                    description=f"El pairing `{pairing_id}` no tiene socket activo.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        try:
            info = await socket.get_info()
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al obtener información del servidor",
                    description=f"```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        def _safe(obj: Any, attr: str, default: str = "N/A") -> str:
            """Extrae un atributo del objeto info de forma segura."""
            try:
                val = getattr(obj, attr, None)
                return str(val) if val is not None else default
            except Exception:
                return default

        embed = discord.Embed(
            title=f"🖥️ {_safe(info, 'name', 'Servidor de Rust')}",
            color=COLOR_INFO,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="👥 Jugadores",    value=f"{_safe(info, 'players')} / {_safe(info, 'maxPlayers')}", inline=True)
        embed.add_field(name="🗺️ Mapa",        value=_safe(info, "map"),         inline=True)
        embed.add_field(name="🌱 Seed",         value=_safe(info, "seed"),        inline=True)
        embed.add_field(name="📐 Tamaño mapa",  value=_safe(info, "mapSize"),     inline=True)
        embed.add_field(name="⏱️ Hora en juego",value=_safe(info, "gameTime"),    inline=True)
        embed.add_field(name="🏃 En cola",      value=_safe(info, "queued", "0"), inline=True)
        embed.set_footer(text=f"Rust+ • Pairing #{pairing_id}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus team_info -----------------------------------------

    @rustplus.command(name="team_info", description="Muestra los miembros del equipo en el servidor")
    @app_commands.describe(pairing_id="ID del pairing")
    @app_commands.guild_only()
    async def cmd_team_info(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        socket = self.manager.get_socket(pairing_id)
        if socket is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Socket no conectado",
                    description=f"El pairing `{pairing_id}` no tiene socket activo.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        try:
            team = await socket.get_team_info()
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al obtener info del equipo",
                    description=f"```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        pairing_row = await self.bot.db.fetchone(
            "SELECT server_name FROM rustplus_pairings WHERE id = ?", (pairing_id,)
        )
        server_name = pairing_row["server_name"] if pairing_row else f"Pairing #{pairing_id}"

        embed = discord.Embed(
            title=f"👥 Team Info — {server_name}",
            color=COLOR_GOLD,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

        # team.members es una lista de objetos TeamMember en rustplus
        members = getattr(team, "members", []) or []
        if not members:
            embed.description = "No se encontraron miembros en el equipo."
        else:
            lines = []
            for m in members:
                name      = getattr(m, "name",    "Desconocido")
                is_online = getattr(m, "isOnline", False)
                is_alive  = getattr(m, "isAlive",  True)
                status    = "🟢 Online" if is_online else "⚫ Offline"
                alive_txt = "" if is_alive else " 💀"
                lines.append(f"{status} **{discord.utils.escape_markdown(name)}**{alive_txt}")
            embed.description = "\n".join(lines)

        embed.set_footer(text=f"Rust+ • Pairing #{pairing_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus send_message --------------------------------------

    @rustplus.command(name="send_message", description="Envía un mensaje al chat de equipo en Rust")
    @app_commands.describe(
        pairing_id="ID del pairing",
        message="Mensaje a enviar al chat de equipo del juego",
    )
    @app_commands.guild_only()
    async def cmd_send_message(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
        message: str,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        socket = self.manager.get_socket(pairing_id)
        if socket is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Socket no conectado",
                    description=f"El pairing `{pairing_id}` no tiene socket activo.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        try:
            await socket.send_team_message(message)
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Error al enviar mensaje",
                    description=f"```\n{e}\n```",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="💬 Mensaje enviado al equipo",
            description=f"**Mensaje:** {discord.utils.escape_markdown(message)}",
            color=COLOR_GOLD,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text=f"Enviado por {interaction.user} • Pairing #{pairing_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /rustplus reconnect -----------------------------------------

    @rustplus.command(name="reconnect", description="Desconecta y reconecta el socket de un pairing")
    @app_commands.describe(pairing_id="ID del pairing a reconectar")
    @app_commands.guild_only()
    async def cmd_reconnect(
        self,
        interaction: discord.Interaction,
        pairing_id: int,
    ) -> None:
        if not RUSTPLUS_AVAILABLE:
            await interaction.response.send_message(embed=_not_available_embed(), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        pairing_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ? AND guild_id = ?",
            (pairing_id, interaction.guild_id),
        )
        if not pairing_row:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Pairing no encontrado",
                    description=f"No existe ningún pairing con ID `{pairing_id}` en este servidor.",
                    color=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Desconectar si ya estaba conectado
        await self.manager.disconnect_pairing(pairing_id)

        # Asegurarse de que el pairing está marcado como activo antes de reconectar
        await self.bot.db.execute(
            "UPDATE rustplus_pairings SET is_active = 1, updated_at = ? WHERE id = ?",
            (_utc_now(), pairing_id),
        )

        # Recargar el row con los datos actualizados
        fresh_row = await self.bot.db.fetchone(
            "SELECT * FROM rustplus_pairings WHERE id = ?", (pairing_id,)
        )
        connected = False
        if fresh_row:
            connected = await self.manager.connect_pairing(fresh_row, cog=self)

        status_icon = "🟢 Conectado" if connected else "🔴 Falló la conexión"
        embed = discord.Embed(
            title="🔄 Reconexión",
            description=(
                f"**Pairing:** {pairing_row['server_name']} "
                f"(`{pairing_row['ip']}:{pairing_row['port']}`)\n"
                f"**Resultado:** {status_icon}"
            ),
            color=COLOR_OK if connected else COLOR_ERROR,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text=f"Pairing #{pairing_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Setup del cog
# ---------------------------------------------------------------------------

async def setup(bot: RustalkerBot) -> None:
    await bot.add_cog(RustPlusCog(bot))

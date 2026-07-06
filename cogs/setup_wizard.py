from __future__ import annotations

import logging
import re
import traceback
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from battlemetrics import parse_server_id, BattleMetricsError

if TYPE_CHECKING:
    from main import RustalkerBot

logger = logging.getLogger("rustalker.setup_wizard")


async def is_admin_or_has_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    db = getattr(interaction.client, "db", None)
    if db:
        settings = await db.get_guild_settings(interaction.guild_id)
        if settings and settings["admin_role_id"]:
            role = interaction.guild.get_role(settings["admin_role_id"])
            if role and role in interaction.user.roles:
                return True
    return False


class Step1ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Selecciona el canal de alertas (Texto)...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        channel = self.values[0]
        guild_channel = interaction.guild.get_channel(channel.id)
        if isinstance(guild_channel, discord.TextChannel):
            view.alert_channel = guild_channel
        else:
            view.alert_channel = interaction.guild.get_channel(channel.id)  # type: ignore
        
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class Step1RoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Selecciona el rol de mención (Opcional)...",
            min_values=0,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        if self.values:
            view.mention_role = self.values[0]  # type: ignore
        else:
            view.mention_role = None
        
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class OpenStep2ModalButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="⚙️ Configurar Umbrales",
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        modal = Step2Modal(view)
        await interaction.response.send_modal(modal)


class Step2Modal(discord.ui.Modal):
    def __init__(self, wizard_view: SetupWizardView) -> None:
        super().__init__(title="Configurar Umbrales Tácticos")
        self.wizard_view = wizard_view
        
        self.window = discord.ui.TextInput(
            label="Ventana de picos de clan (minutos)",
            placeholder="Ej: 15",
            default=str(wizard_view.clan_spike_window),
            min_length=1,
            max_length=4
        )
        self.threshold = discord.ui.TextInput(
            label="Límite de picos de clan (jugadores)",
            placeholder="Ej: 3 (mínimo de miembros online para alerta)",
            default=str(wizard_view.clan_spike_threshold),
            min_length=1,
            max_length=3
        )
        self.queue = discord.ui.TextInput(
            label="Límite de cola (alerta de cola)",
            placeholder="Ej: 5 (avisar si la cola baja de este número)",
            default=str(wizard_view.queue_threshold),
            min_length=1,
            max_length=3
        )
        
        self.add_item(self.window)
        self.add_item(self.threshold)
        self.add_item(self.queue)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            window_val = int(self.window.value)
            threshold_val = int(self.threshold.value)
            queue_val = int(self.queue.value)
            
            if window_val <= 0 or threshold_val <= 0 or queue_val < 0:
                raise ValueError("Values must be positive integers")
        except ValueError:
            await interaction.response.send_message(
                "❌ Valores inválidos. Debes ingresar números enteros positivos.",
                ephemeral=True
            )
            return
            
        self.wizard_view.clan_spike_window = window_val
        self.wizard_view.clan_spike_threshold = threshold_val
        self.wizard_view.queue_threshold = queue_val
        
        try:
            await interaction.response.edit_message(embed=self.wizard_view.make_embed(), view=self.wizard_view)
        except Exception:
            await self.wizard_view.update_message()
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)


class Step3RoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Selecciona el rol Administrador del Bot...",
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        view.admin_role = self.values[0]  # type: ignore
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class Step4PlayersChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Canal para Jugadores Online (Voz)...",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        channel = self.values[0]
        guild_channel = interaction.guild.get_channel(channel.id)
        if isinstance(guild_channel, discord.VoiceChannel):
            view.stats_players_channel = guild_channel
        else:
            view.stats_players_channel = guild_channel  # type: ignore
            
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class Step4QueueChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Canal para Cola de Espera (Voz)...",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        channel = self.values[0]
        guild_channel = interaction.guild.get_channel(channel.id)
        if isinstance(guild_channel, discord.VoiceChannel):
            view.stats_queue_channel = guild_channel
        else:
            view.stats_queue_channel = guild_channel  # type: ignore
            
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class Step4MapChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Canal para Nombre del Mapa (Voz)...",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=2
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        channel = self.values[0]
        guild_channel = interaction.guild.get_channel(channel.id)
        if isinstance(guild_channel, discord.VoiceChannel):
            view.stats_map_channel = guild_channel
        else:
            view.stats_map_channel = guild_channel  # type: ignore
            
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class Step5ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Selecciona el canal para resúmenes (Texto)...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        channel = self.values[0]
        guild_channel = interaction.guild.get_channel(channel.id)
        if isinstance(guild_channel, discord.TextChannel):
            view.summary_channel = guild_channel
        else:
            view.summary_channel = guild_channel  # type: ignore
            
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class OpenStep5ModalButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="⏰ Configurar Hora",
            row=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        modal = Step5Modal(view)
        await interaction.response.send_modal(modal)


class Step5Modal(discord.ui.Modal):
    def __init__(self, wizard_view: SetupWizardView) -> None:
        super().__init__(title="Hora del Resumen Diario")
        self.wizard_view = wizard_view
        
        self.time_input = discord.ui.TextInput(
            label="Hora del resumen (formato HH:MM)",
            placeholder="Ej: 08:00 o 21:30",
            default=wizard_view.summary_time,
            min_length=5,
            max_length=5
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        time_val = self.time_input.value.strip()
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time_val):
            await interaction.response.send_message(
                "❌ Formato de hora inválido. Debe ser HH:MM (de 00:00 a 23:59).",
                ephemeral=True
            )
            return
            
        self.wizard_view.summary_time = time_val
        try:
            await interaction.response.edit_message(embed=self.wizard_view.make_embed(), view=self.wizard_view)
        except Exception:
            await self.wizard_view.update_message()
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)


class OpenStep6ModalButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="🖥️ Agregar Servidor",
            row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SetupWizardView = self.view  # type: ignore
        modal = Step6Modal(view)
        await interaction.response.send_modal(modal)


class Step6Modal(discord.ui.Modal):
    def __init__(self, wizard_view: SetupWizardView) -> None:
        super().__init__(title="Agregar Servidor BattleMetrics")
        self.wizard_view = wizard_view
        
        self.server_input = discord.ui.TextInput(
            label="URL o ID de BattleMetrics del servidor",
            placeholder="Ej: 1234567 o url completa",
            min_length=1,
            max_length=200
        )
        self.add_item(self.server_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        raw_val = self.server_input.value.strip()
        server_id = parse_server_id(raw_val)
        if not server_id:
            await interaction.followup.send("❌ URL o ID de servidor inválida.", ephemeral=True)
            return
            
        try:
            server_data = await self.wizard_view.bot.bm_client.get_server(server_id, include_players=False)
            server_name = server_data.get("name", f"Servidor {server_id}")
            
            self.wizard_view.bm_server_id = server_id
            self.wizard_view.bm_server_name = server_name
            
            await interaction.followup.send(f"✅ Servidor verificado: **{server_name}**", ephemeral=True)
            await self.wizard_view.update_message()
            
        except BattleMetricsError as e:
            await interaction.followup.send(f"❌ Error al consultar BattleMetrics: {e}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send("❌ Ocurrió un error inesperado al verificar el servidor.", ephemeral=True)


class SetupWizardView(discord.ui.View):
    def __init__(self, bot: RustalkerBot, guild_id: int, author_id: int) -> None:
        super().__init__(timeout=300.0)
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id
        self.message: discord.Message | None = None
        
        # Step track (1 to 6)
        self.current_step = 1
        
        # Step 1
        self.alert_channel: discord.TextChannel | None = None
        self.mention_role: discord.Role | None = None
        
        # Step 2
        self.clan_spike_window: int = 15
        self.clan_spike_threshold: int = 3
        self.queue_threshold: int = 5
        
        # Step 3
        self.admin_role: discord.Role | None = None
        
        # Step 4
        self.stats_players_channel: discord.VoiceChannel | None = None
        self.stats_queue_channel: discord.VoiceChannel | None = None
        self.stats_map_channel: discord.VoiceChannel | None = None
        
        # Step 5
        self.summary_channel: discord.TextChannel | None = None
        self.summary_time: str = "08:00"
        
        # Step 6
        self.bm_server_id: int | None = None
        self.bm_server_name: str | None = None

    async def load_existing_settings(self) -> None:
        try:
            settings = await self.bot.db.get_guild_settings(self.guild_id)
            if settings:
                guild = self.bot.get_guild(self.guild_id)
                if guild:
                    # Step 1
                    if settings.get("alert_channel_id"):
                        self.alert_channel = guild.get_channel(settings["alert_channel_id"])  # type: ignore
                    if settings.get("mention_role_id"):
                        self.mention_role = guild.get_role(settings["mention_role_id"])
                    
                    # Step 2
                    self.clan_spike_window = settings.get("clan_spike_window_minutes", 15)
                    self.clan_spike_threshold = settings.get("clan_spike_threshold", 3)
                    self.queue_threshold = settings.get("queue_threshold", 5)
                    
                    # Step 3
                    if settings.get("admin_role_id"):
                        self.admin_role = guild.get_role(settings["admin_role_id"])
                    
                    # Step 4
                    if settings.get("stats_channel_players_id"):
                        self.stats_players_channel = guild.get_channel(settings["stats_channel_players_id"])  # type: ignore
                    if settings.get("stats_channel_queue_id"):
                        self.stats_queue_channel = guild.get_channel(settings["stats_channel_queue_id"])  # type: ignore
                    if settings.get("stats_channel_map_id"):
                        self.stats_map_channel = guild.get_channel(settings["stats_channel_map_id"])  # type: ignore
                    
                    # Step 5
                    if settings.get("summary_channel_id"):
                        self.summary_channel = guild.get_channel(settings["summary_channel_id"])  # type: ignore
                    self.summary_time = settings.get("summary_time", "08:00")
                    
                servers = await self.bot.db.list_tracked_servers(self.guild_id)
                if servers:
                    self.bm_server_id = servers[0]["battlemetrics_server_id"]
                    self.bm_server_name = servers[0]["name"]
        except Exception as e:
            logger.error(f"Error loading existing settings: {e}")

    def update_components(self) -> None:
        self.clear_items()
        
        # Add step-specific components
        if self.current_step == 1:
            self.add_item(Step1ChannelSelect())
            self.add_item(Step1RoleSelect())
        elif self.current_step == 2:
            self.add_item(OpenStep2ModalButton())
        elif self.current_step == 3:
            self.add_item(Step3RoleSelect())
        elif self.current_step == 4:
            self.add_item(Step4PlayersChannelSelect())
            self.add_item(Step4QueueChannelSelect())
            self.add_item(Step4MapChannelSelect())
        elif self.current_step == 5:
            self.add_item(Step5ChannelSelect())
            self.add_item(OpenStep5ModalButton())
        elif self.current_step == 6:
            self.add_item(OpenStep6ModalButton())
            
        nav_row = 3 if self.current_step == 4 else 2
        
        # 1. Back
        back_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="◀ Atrás",
            disabled=(self.current_step == 1),
            row=nav_row
        )
        back_btn.callback = self.on_back
        self.add_item(back_btn)
        
        # 2. Next
        if self.current_step < 6:
            next_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Siguiente ▶",
                row=nav_row
            )
            next_btn.callback = self.on_next
            self.add_item(next_btn)
            
        # 3. Skip
        skip_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Omitir ⏭",
            row=nav_row
        )
        skip_btn.callback = self.on_skip
        self.add_item(skip_btn)
        
        # 4. Finish
        finish_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Terminar 🏁",
            row=nav_row
        )
        finish_btn.callback = self.on_finish
        self.add_item(finish_btn)

    def get_progress_bar(self) -> str:
        filled = "🟩" * self.current_step
        empty = "⬜" * (6 - self.current_step)
        percentage = int((self.current_step / 6) * 100)
        return f"`[{filled}{empty}]` **{percentage}%**"

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔧 ASISTENTE DE CONFIGURACIÓN — PASO {self.current_step}/6",
            color=0xce422b,  # Rust Red
            timestamp=discord.utils.utcnow()
        )
        
        embed.description = f"Progreso: {self.get_progress_bar()}\n\n"
        
        if self.current_step == 1:
            embed.description += (
                "**Paso 1: Alertas y Menciones**\n"
                "Configura el canal principal donde se enviarán las alertas de conexión/desconexión y el rol que será mencionado en alertas importantes."
            )
            embed.add_field(
                name="Canal de Alertas",
                value=self.alert_channel.mention if self.alert_channel else "❌ *No seleccionado (Obligatorio)*",
                inline=True
            )
            embed.add_field(
                name="Rol de Mención",
                value=self.mention_role.mention if self.mention_role else "⚪ *Ninguno (Opcional)*",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Selecciona un canal de texto y un rol de mención opcional en los menús inferiores, luego presiona **Siguiente ▶**.",
                inline=False
            )
            
        elif self.current_step == 2:
            embed.description += (
                "**Paso 2: Umbrales Tácticos**\n"
                "Ajusta los parámetros y límites para la detección de picos de conexiones de clanes y las alertas de cola de espera."
            )
            embed.add_field(
                name="Ventana de picos de clan",
                value=f"`{self.clan_spike_window} minutos`",
                inline=True
            )
            embed.add_field(
                name="Límite de picos de clan",
                value=f"`{self.clan_spike_threshold} jugadores`",
                inline=True
            )
            embed.add_field(
                name="Límite de cola (Alertas)",
                value=f"`{self.queue_threshold} jugadores`",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Presiona el botón **⚙️ Configurar Umbrales** para modificar estos valores en un formulario emergente, luego presiona **Siguiente ▶**.",
                inline=False
            )
            
        elif self.current_step == 3:
            embed.description += (
                "**Paso 3: Administrador del Bot**\n"
                "Define qué rol tendrá permisos de administración sobre este bot (añadir servidores, vigilar jugadores, crear clanes) adicional a los administradores del servidor."
            )
            embed.add_field(
                name="Rol Administrador del Bot",
                value=self.admin_role.mention if self.admin_role else "❌ *No seleccionado (Obligatorio)*",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Selecciona el rol correspondiente en el menú inferior, luego presiona **Siguiente ▶**.",
                inline=False
            )
            
        elif self.current_step == 4:
            embed.description += (
                "**Paso 4: Canales de Estadísticas Dinámicos**\n"
                "Configura canales de voz que se renombrarán automáticamente para mostrar estadísticas del servidor de Rust en tiempo real."
            )
            embed.add_field(
                name="Canal: Jugadores Online",
                value=self.stats_players_channel.mention if self.stats_players_channel else "⚪ *Desactivado*",
                inline=True
            )
            embed.add_field(
                name="Canal: Cola de Espera",
                value=self.stats_queue_channel.mention if self.stats_queue_channel else "⚪ *Desactivado*",
                inline=True
            )
            embed.add_field(
                name="Canal: Mapa Actual",
                value=self.stats_map_channel.mention if self.stats_map_channel else "⚪ *Desactivado*",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Selecciona los canales de voz en los menús inferiores (puedes dejarlos en blanco para desactivar estadísticas específicas) y presiona **Siguiente ▶**.",
                inline=False
            )
            
        elif self.current_step == 5:
            embed.description += (
                "**Paso 5: Canal y Hora de Resúmenes Diarios**\n"
                "Configura a qué hora y en qué canal de texto el bot publicará un resumen completo de la actividad de las últimas 24 horas."
            )
            embed.add_field(
                name="Canal de Resúmenes",
                value=self.summary_channel.mention if self.summary_channel else "❌ *No seleccionado (Obligatorio)*",
                inline=True
            )
            embed.add_field(
                name="Hora del Resumen",
                value=f"`{self.summary_time} UTC`",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Selecciona el canal de texto y presiona el botón **⏰ Configurar Hora** para cambiar la hora de envío, luego presiona **Siguiente ▶**.",
                inline=False
            )
            
        elif self.current_step == 6:
            embed.description += (
                "**Paso 6: Agregar primer servidor de BattleMetrics**\n"
                "Añade un servidor de Rust para iniciar el monitoreo. Necesitas la URL del servidor o su ID numérico."
            )
            embed.add_field(
                name="Servidor BattleMetrics",
                value=f"**{self.bm_server_name}** (`{self.bm_server_id}`)" if self.bm_server_id else "❌ *Ninguno*",
                inline=True
            )
            embed.add_field(
                name="👉 Instrucciones",
                value="Presiona el botón **🖥️ Agregar Servidor** e ingresa la ID/URL del servidor en el formulario. Finalmente, pulsa **Terminar 🏁**.",
                inline=False
            )
            
        embed.set_footer(text=f"Asistente iniciado por {self.bot.user.name if self.bot.user else 'Bot'}")
        return embed

    async def update_message(self) -> None:
        if self.message:
            try:
                await self.message.edit(embed=self.make_embed(), view=self)
            except Exception as e:
                logger.error(f"Failed to edit message: {e}")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Solo el administrador que inició `/setup` puede interactuar con este asistente.",
                ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()
        embed = discord.Embed(
            title="⏰ ASISTENTE EXPIRADO",
            description="El asistente de configuración ha expirado por inactividad (límite de 300 segundos).",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        if self.message:
            try:
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass

    async def on_back(self, interaction: discord.Interaction) -> None:
        if self.current_step > 1:
            self.current_step -= 1
            self.update_components()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await interaction.response.defer()

    async def on_skip(self, interaction: discord.Interaction) -> None:
        if self.current_step < 6:
            self.current_step += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await self.finish_wizard(interaction)

    async def on_next(self, interaction: discord.Interaction) -> None:
        success = await self.save_current_step(interaction)
        if not success:
            return
            
        if self.current_step < 6:
            self.current_step += 1
            self.update_components()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        else:
            await self.finish_wizard(interaction)

    async def on_finish(self, interaction: discord.Interaction) -> None:
        await self.save_current_step(interaction, ignore_validation=True)
        await self.finish_wizard(interaction)

    async def save_current_step(self, interaction: discord.Interaction, ignore_validation: bool = False) -> bool:
        guild_id = self.guild_id
        
        try:
            if self.current_step == 1:
                if self.alert_channel:
                    await self.bot.db.ensure_guild_settings(guild_id)
                    await self.bot.db.set_alert_channel(guild_id, self.alert_channel.id)
                    await self.bot.db.set_mention_role(guild_id, self.mention_role.id if self.mention_role else None)
                else:
                    if not ignore_validation:
                        await interaction.response.send_message(
                            "❌ Debes seleccionar un canal de alertas de texto para continuar, o presionar 'Omitir'.",
                            ephemeral=True
                        )
                        return False
                        
            elif self.current_step == 2:
                await self.bot.db.ensure_guild_settings(guild_id)
                await self.bot.db.set_clan_spike_rules(guild_id, self.clan_spike_window, self.clan_spike_threshold)
                await self.bot.db.set_queue_threshold(guild_id, self.queue_threshold)
                
            elif self.current_step == 3:
                if self.admin_role:
                    await self.bot.db.ensure_guild_settings(guild_id)
                    await self.bot.db.set_admin_role(guild_id, self.admin_role.id)
                else:
                    if not ignore_validation:
                        await interaction.response.send_message(
                            "❌ Debes seleccionar un rol de administrador para continuar, o presionar 'Omitir'.",
                            ephemeral=True
                        )
                        return False
                        
            elif self.current_step == 4:
                await self.bot.db.ensure_guild_settings(guild_id)
                await self.bot.db.set_stats_channels(
                    guild_id,
                    self.stats_players_channel.id if self.stats_players_channel else None,
                    self.stats_queue_channel.id if self.stats_queue_channel else None,
                    self.stats_map_channel.id if self.stats_map_channel else None
                )
                
            elif self.current_step == 5:
                if self.summary_channel:
                    await self.bot.db.ensure_guild_settings(guild_id)
                    await self.bot.db.set_summary_settings(guild_id, self.summary_channel.id, self.summary_time)
                else:
                    if not ignore_validation:
                        await interaction.response.send_message(
                            "❌ Debes seleccionar un canal de resúmenes para continuar, o presionar 'Omitir'.",
                            ephemeral=True
                        )
                        return False
                        
            elif self.current_step == 6:
                if self.bm_server_id:
                    await self.bot.db.add_tracked_server(guild_id, self.bm_server_id, self.bm_server_name)
                else:
                    if not ignore_validation:
                        await interaction.response.send_message(
                            "❌ Debes agregar un servidor para continuar, o presionar 'Omitir' / 'Terminar'.",
                            ephemeral=True
                        )
                        return False
            return True
            
        except Exception as e:
            logger.error(f"Error saving settings for step {self.current_step}: {e}\n{traceback.format_exc()}")
            await interaction.response.send_message(
                "❌ Ocurrió un error al guardar la configuración en la base de datos.",
                ephemeral=True
            )
            return False

    async def finish_wizard(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        
        embed = discord.Embed(
            title="🏁 CONFIGURACIÓN COMPLETADA",
            description="¡El asistente de configuración ha finalizado! Se han guardado todos los parámetros configurados con éxito.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        
        alert_chan_str = self.alert_channel.mention if self.alert_channel else "`No configurado`"
        mention_role_str = self.mention_role.mention if self.mention_role else "`Ninguno`"
        admin_role_str = self.admin_role.mention if self.admin_role else "`No configurado`"
        
        stats_str = (
            f"• Jugadores: {self.stats_players_channel.mention if self.stats_players_channel else '`No configurado`'}\n"
            f"• Cola: {self.stats_queue_channel.mention if self.stats_queue_channel else '`No configurado`'}\n"
            f"• Mapa: {self.stats_map_channel.mention if self.stats_map_channel else '`No configurado`'}"
        )
        
        summary_str = (
            f"• Canal: {self.summary_channel.mention if self.summary_channel else '`No configurado`'}\n"
            f"• Hora: `{self.summary_time}`"
        )
        
        server_str = f"**{self.bm_server_name}** (`{self.bm_server_id}`)" if self.bm_server_id else "`Ninguno`"
        
        embed.add_field(name="📢 Alertas de Actividad", value=f"Canal: {alert_chan_str}\nMención: {mention_role_str}", inline=False)
        embed.add_field(
            name="⚙️ Umbrales Tácticos", 
            value=f"• Ventana de Picos: `{self.clan_spike_window} min`\n• Mín. Jugadores Clan: `{self.clan_spike_threshold}`\n• Mín. Jugadores Cola: `{self.queue_threshold}`", 
            inline=False
        )
        embed.add_field(name="🛡️ Admin del Bot", value=admin_role_str, inline=False)
        embed.add_field(name="📈 Canales de Estadísticas", value=stats_str, inline=False)
        embed.add_field(name="📅 Resúmenes Diarios", value=summary_str, inline=False)
        embed.add_field(name="🖥️ Primer Servidor Monitoreado", value=server_str, inline=False)
        
        if interaction.response.is_done():
            if self.message:
                try:
                    await self.message.edit(embed=embed, view=self)
                except Exception:
                    pass
        else:
            await interaction.response.edit_message(embed=embed, view=self)
            
        self.stop()


class SetupWizardCog(commands.Cog):
    def __init__(self, bot: RustalkerBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Inicia el asistente interactivo de configuración del bot")
    @app_commands.check(is_admin_or_has_role)
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("❌ Este comando solo se puede usar en servidores.", ephemeral=True)
            return
            
        view = SetupWizardView(self.bot, guild_id, interaction.user.id)
        await view.load_existing_settings()
        view.update_components()
        
        embed = view.make_embed()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

    @setup_cmd.error
    async def setup_cmd_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ No tienes permisos para usar este comando. Se requiere ser Administrador de Discord o tener el rol de administrador configurado.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ No tienes permisos para usar este comando. Se requiere ser Administrador de Discord o tener el rol de administrador configurado.",
                    ephemeral=True
                )
        else:
            logger.error(f"Error in setup command: {error}\n{traceback.format_exc()}")
            if interaction.response.is_done():
                await interaction.followup.send("❌ Ocurrió un error al iniciar el asistente de configuración.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ocurrió un error al iniciar el asistente de configuración.", ephemeral=True)


async def setup(bot: RustalkerBot) -> None:
    await bot.add_cog(SetupWizardCog(bot))

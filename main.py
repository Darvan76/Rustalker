from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from battlemetrics import BattleMetricsClient
from database import Database

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("rustalker")


class RustalkerBot(commands.Bot):
    def __init__(self, db_path: str, bm_token: str | None = None) -> None:
        # We only need standard guilds intent and some default intents
        # since everything operates via Slash Commands and background tasks.
        intents = discord.Intents.default()
        intents.guilds = True
        
        super().__init__(
            command_prefix="!",  # Fallback prefix (unused because of Slash Commands)
            intents=intents,
            help_command=None
        )
        
        self.db = Database(db_path)
        self.bm_client = BattleMetricsClient(api_token=bm_token)

    async def setup_hook(self) -> None:
        # Connect to Database and start BattleMetrics Client
        logger.info("Initializing database...")
        await self.db.connect()
        
        logger.info("Initializing BattleMetrics client...")
        await self.bm_client.start()

        # Load extension cogs
        logger.info("Loading cogs...")
        await self.load_extension("cogs.tracker")
        await self.load_extension("cogs.commands")

    async def close(self) -> None:
        logger.info("Shutting down cleanly...")
        await self.bm_client.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info(f"Bot connected successfully as {self.user} (ID: {self.user.id})")
        
        # Synchronize slash commands globally across all guilds
        logger.info("Synchronizing Slash Commands...")
        try:
            synced = await self.tree.sync()
            logger.info(f"Successfully synced {len(synced)} global Slash Commands.")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")


def main() -> None:
    # Load env variables from a .env file next to this script.
    env_path = Path(__file__).resolve().with_name(".env")
    load_dotenv(dotenv_path=env_path)

    token = os.getenv("DISCORD_BOT_TOKEN")
    bm_token = os.getenv("BATTLEMETRICS_TOKEN")
    db_path = os.getenv("DATABASE_PATH", "rustalker.db")

    missing_vars = [name for name, value in (
        ("DISCORD_BOT_TOKEN", token),
    ) if not value]

    if missing_vars:
        if not env_path.exists():
            logger.critical(
                "Error: no se encontró el archivo .env en %s. Copia .env.example a .env y completa los valores requeridos.",
                env_path,
            )
        logger.critical(
            "Faltan variables de entorno obligatorias: %s",
            ", ".join(missing_vars),
        )
        if bm_token is None:
            logger.info("BATTLEMETRICS_TOKEN no está configurado. El bot arrancará con funcionalidad limitada.")
        sys.exit(1)

    bot = RustalkerBot(db_path=db_path, bm_token=bm_token)

    try:
        # Run the Discord Bot
        bot.run(token)
    except KeyboardInterrupt:
        logger.info("Bot execution interrupted by user.")
    except Exception as e:
        logger.critical(f"Bot crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging
import os
import sys

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
    # Load env variables from .env if present
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.critical("Error: DISCORD_BOT_TOKEN is missing in the environment. Please configure it in .env file.")
        sys.exit(1)

    bm_token = os.getenv("BATTLEMETRICS_TOKEN")
    db_path = os.getenv("DATABASE_PATH", "rustalker.db")

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

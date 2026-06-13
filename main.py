import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import shutil

import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_REMOVED, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from dotenv import load_dotenv

# インポートを整理
from utils import JST, AUDIO_DIR

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
STORAGE_CHANNEL_ID = os.getenv('STORAGE_CHANNEL_ID') # データを保存するチャンネルID
GUILD_ID = os.getenv('GUILD_ID') # ストレージチャンネルを作成するサーバーID

# インテントの設定
intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

class AlarmBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        
        # 実行ファイルのディレクトリを取得してパスを動的に設定
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.scheduler = AsyncIOScheduler(timezone=JST)
        self.db_file = os.path.join(base_dir, "jobs.sqlite")
        self.base_dir = base_dir

    async def setup_hook(self):
        # データの復元とエンジンの読み込み
        await self.load_extension('cogs.storage_cog')
        if self.storage:
            await self.storage.ensure_storage_channel(GUILD_ID, STORAGE_CHANNEL_ID)
            await self.storage.download_data_from_channel()

        self.tree.on_error = self.on_app_command_error
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{self.db_file}')
        }
        self.scheduler.configure(jobstores=jobstores)
        self.scheduler.start()

        # 各種機能の読み込み
        for ext in ['voice_cog', 'alarm_cog', 'pomodoro_cog', 'utility_cog']:
            await self.load_extension(f'cogs.{ext}')

        await self.tree.sync()
        logger.info("アラームちゃん 準備完了")

    @property
    def storage(self):
        return self.get_cog('StorageCog')

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """コマンド実行中にエラーが発生した際の共通処理"""
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ クールタイム中です。{error.retry_after:.1f}秒後に再度試してください。"
        else:
            logger.error(f"Unhandled command error: {error}")
            msg = "⚠️ コマンドの実行中に予期せぬエラーが発生しました。"
        
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True, silent=True)

    async def close(self):
        logger.info("Bot is shutting down. Finalizing state...")
        if self.storage:
            await self.storage.upload_data_to_channel()
        self.scheduler.shutdown()
        await super().close()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name}")

bot = AlarmBot()
if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing. Please set it in GitHub Secrets or .env file.")
    else:
        bot.run(TOKEN)

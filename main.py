import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import shutil
import random # randomはalarm_cogで使うので残す
import json # JSONファイル読み書き用

import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from dotenv import load_dotenv

# インポートを整理
from utils import JST, AUDIO_DIR

# ログの設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# インテントの設定
intents = discord.Intents.default()
intents.voice_states = True

class AlarmBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        
        # 実行ファイルのディレクトリを取得してパスを動的に設定
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # スケジューラーの設定（SQLiteにジョブを保存するように変更）
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{os.path.join(base_dir, "jobs.sqlite")}')
        }
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)
        self.history_file = os.path.join(base_dir, "history.json") # history.jsonのパス
        self.history = self.load_history() # 履歴を読み込む

    async def setup_hook(self):
        # ボット起動時にスケジューラーを開始
        self.scheduler.start()
        # Cogの読み込み (ファイル名から拡張子を除いたもの)
        await self.load_extension('alarm_cog')
        await self.load_extension('pomodoro_cog')
        # スラッシュコマンドをDiscord側に同期
        await self.tree.sync()
        logger.info("Scheduler started.")
        logger.info("Slash commands synced.")

    def load_history(self):
        """履歴をJSONファイルから読み込む"""
        if os.path.exists(self.history_file):
            with open(self.history_file, 'r', encoding='utf-8') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError: # ファイルが空や不正な形式の場合
                    return []
        return []

    def save_history(self):
        """履歴をJSONファイルに保存する"""
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, ensure_ascii=False, indent=4)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        
        # JSON履歴ファイルの初期化は不要（存在しない場合は空リストで開始）
        
        # 音声フォルダーの作成
        if not os.path.exists(AUDIO_DIR):
            os.makedirs(AUDIO_DIR)
            logger.info(f"Created directory: {AUDIO_DIR}")

        # ffmpegの存在確認
        if not shutil.which("ffmpeg"):
            logger.error("FFmpeg was not found in the system PATH. Audio playback will fail.")

        await self.change_presence(activity=discord.Game(name="/alarm でセット"))

bot = AlarmBot()
if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing in .env file.")
    else:
        bot.run(TOKEN)

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
        # スケジューラーの設定（SQLiteにジョブを保存するように変更）
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{os.path.join(base_dir, "jobs.sqlite")}')
        }
        # タイムゾーンをJSTに指定して、時間のズレを防止
        self.scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=JST)
        self.db_file = os.path.join(base_dir, "jobs.sqlite") # データベースのパス
        self.storage_channel = None

    async def setup_hook(self):
        # ストレージチャンネルの確保
        await self.ensure_storage_channel()

        # 最新データのダウンロード
        await self.download_data_from_channel()

        # ボット起動時にスケジューラーを開始
        self.scheduler.start()

        # 名前空間を cogs. に固定して PicklingError を防ぐ
        for ext in ['alarm_cog', 'pomodoro_cog']:
            try:
                await self.load_extension(f'cogs.{ext}')
                logger.info(f"Loaded extension: cogs.{ext}")
            except Exception as e:
                logger.error(f"Failed to load extension cogs.{ext}: {e}")

        # スラッシュコマンドをDiscord側に同期
        await self.tree.sync()
        logger.info("Scheduler started.")
        logger.info("Slash commands synced.")

    async def ensure_storage_channel(self):
        """ストレージ用チャンネルを確認・作成し、ボットのオーナーに権限を付与する"""
        guild = None
        if GUILD_ID and GUILD_ID.isdigit():
            try:
                guild = self.get_guild(int(GUILD_ID)) or await self.fetch_guild(int(GUILD_ID))
            except:
                pass
        
        if not guild:
            # GUILD_IDが未設定の場合、ボットが参加している最初のサーバーを自動的に使用する
            async for g in self.fetch_guilds(limit=1):
                guild = await self.fetch_guild(g.id)
                break
        
        if not guild:
            logger.warning("No guild found. Cannot create storage channel.")
            return

        channel_name = "storage"
        
        # 1. すでに設定済みのIDがあれば、そのチャンネルが実在するか確認する
        if STORAGE_CHANNEL_ID and STORAGE_CHANNEL_ID.isdigit():
            try:
                channel = self.get_channel(int(STORAGE_CHANNEL_ID)) or await self.fetch_channel(int(STORAGE_CHANNEL_ID))
                if channel:
                    self.storage_channel = channel
                    return
            except:
                pass

        # 2. 名前で探す（キャッシュだけでなく、APIから最新のリストを取得して重複作成を防ぐ）
        all_channels = await guild.fetch_channels()
        channel = discord.utils.get(all_channels, name=channel_name)

        if not channel:
            # ボットのオーナーを取得
            app_info = await self.application_info()
            owner = app_info.owner

            # 権限設定: 全員不可視、ボットとオーナーのみ可視
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                self.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True),
                owner: discord.PermissionOverwrite(view_channel=True, read_messages=True, read_message_history=True)
            }
            channel = await guild.create_text_channel(channel_name, overwrites=overwrites, topic="Bot Data Storage (Private)")
            logger.info(f"Created private storage channel: {channel_name} for {owner.name}")
        
        self.storage_channel = channel

    async def upload_data_to_channel(self):
        """jobs.sqlite と history.json を指定のチャンネルにアップロードして保存する"""
        target_channel = self.storage_channel
        storage_id = os.getenv('STORAGE_CHANNEL_ID')
        if not target_channel and storage_id and storage_id.isdigit():
            try:
                target_channel = self.get_channel(int(storage_id)) or await self.fetch_channel(int(storage_id))
            except:
                pass

        if not target_channel:
            return
        try:
            files = []
            if os.path.exists(self.db_file):
                files.append(discord.File(self.db_file))
            
            if files:
                # シンプルでシステム的な表示
                embed = discord.Embed(
                    description=f"💾 **System Data Synced** | `{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}`",
                    color=discord.Color.dark_grey()
                )
                new_msg = await target_channel.send(embed=embed, files=files)
                logger.info("Data uploaded to storage channel.")
                
                # 掃除処理（一括削除）は時間がかかるため、awaitせずバックグラウンドで実行
                async def cleanup():
                    try:
                        await target_channel.purge(
                            limit=10, # 100件も遡る必要はない（頻繁に更新されるため）
                            check=lambda m: m.author == self.user and m.id != new_msg.id,
                            before=new_msg
                        )
                    except: pass
                self.loop.create_task(cleanup())
        except Exception as e:
            logger.error(f"Failed to upload data: {e}")

    async def download_data_from_channel(self):
        """指定のチャンネルから最新のバックアップをダウンロードする"""
        target_channel = self.storage_channel
        storage_id = os.getenv('STORAGE_CHANNEL_ID')
        if not target_channel and storage_id and storage_id.isdigit():
            try:
                target_channel = self.get_channel(int(storage_id)) or await self.fetch_channel(int(storage_id))
            except:
                pass

        if not target_channel:
            return
        try:
            async for message in target_channel.history(limit=100):
                if message.author == self.user and message.attachments:
                    for attachment in message.attachments:
                        if attachment.filename == "jobs.sqlite":
                            await attachment.save(self.db_file)
                            logger.info(f"Downloaded {attachment.filename} from Discord.")
                    break
        except Exception as e:
            logger.error(f"Failed to download data: {e}")

    async def close(self):
        """シャットダウン前にデータをアップロードする"""
        await self.upload_data_to_channel()
        await super().close()

    async def on_message(self, message):
        """メッセージ受信時のイベント処理"""
        # storage チャンネル内でのボット以外の発言を削除
        if self.storage_channel and message.channel.id == self.storage_channel.id:
            if message.author != self.user:
                try:
                    await message.delete()
                    return # ストレージ保護のため、コマンド処理を含めここで中断
                except discord.Forbidden:
                    logger.warning("Could not delete message in storage channel: Missing permissions.")
                except Exception as e:
                    logger.error(f"Error in on_message delete: {e}")
        
        await self.process_commands(message)

    async def on_ready(self):
        # 起動時に「過去の遺物」を掃除する
        cleaned_count = 0
        now = datetime.now(timezone.utc)
        for job in self.scheduler.get_jobs():
            # 次の実行予定がない、または実行予定が過去の「1回限り」のジョブを削除
            if job.next_run_time is None or job.next_run_time < now:
                # 繰り返し（cron）ではないジョブ（snoozeやpomoの一時ジョブ）が対象
                if not hasattr(job.trigger, 'cron'): 
                    self.scheduler.remove_job(job.id)
                    cleaned_count += 1
        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} stale jobs from database.")

        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        
        # JSON履歴ファイルの初期化は不要（存在しない場合は空リストで開始）
        # TODO: daveyの存在チェックをここに入れる
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
        logger.error("DISCORD_TOKEN is missing. Please set it in GitHub Secrets or .env file.")
    else:
        bot.run(TOKEN)

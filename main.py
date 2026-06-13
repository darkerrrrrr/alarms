import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import shutil
import json # JSONファイル読み書き用

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
        # スケジューラーは後で初期化するため、ここではインスタンスのみ作成
        self.scheduler = AsyncIOScheduler(timezone=JST)
        self.history_file = os.path.join(base_dir, "history.json") # history.jsonのパス
        self.db_file = os.path.join(base_dir, "jobs.sqlite") # データベースのパス
        self.base_dir = base_dir
        self.history = self.load_history() # 履歴を読み込む
        self.storage_channel = None
        self._sync_wait_task = None # 同期待機用のタスク保持

    async def setup_hook(self):
        # ストレージチャンネルの確保
        await self.ensure_storage_channel()

        # 【重要】DB接続前に、まず最新データをDiscordからダウンロードする
        await self.download_data_from_channel()

        # ダウンロード完了後にDBストアを接続（これで上書きエラーを防ぐ）
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{self.db_file}')
        }
        self.scheduler.configure(jobstores=jobstores)

        # ジョブに変更があったら同期を依頼する
        def on_job_change(event):
            self.request_sync()

        # ジョブの追加、削除、実行完了をすべて監視して、自動でバックアップをとる
        self.scheduler.add_listener(on_job_change, EVENT_JOB_REMOVED | EVENT_JOB_EXECUTED | EVENT_JOB_ADDED | EVENT_JOB_MODIFIED)

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
        logger.info("Bot components initialized.")

    def request_sync(self):
        """同期（バックアップ）を依頼する。連続した依頼は5秒待ってから1回にまとめる。"""
        if not self.loop or not self.loop.is_running():
            return

        async def delayed_sync():
            await asyncio.sleep(5)
            await self.upload_data_to_channel()

        # すでに待機中の同期タスクがあればキャンセルして新しく作り直す
        if self._sync_wait_task and not self._sync_wait_task.done():
            self._sync_wait_task.cancel()
        
        self._sync_wait_task = self.loop.create_task(delayed_sync())

    def load_history(self):
        """履歴をJSONファイルから読み込む"""
        if os.path.exists(self.history_file):
            with open(self.history_file, 'r', encoding='utf-8') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError: # ファイルが空や不正な形式の場合
                    return []
        return []

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
        if not self.storage_channel:
            logger.warning("Storage channel not available. Skipping upload.")
            return
        try:
            # アップロード直前に現在の履歴をディスクに強制保存（同期漏れ防止）
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=4)

            files = []
            if os.path.exists(self.db_file):
                files.append(discord.File(self.db_file))
            if os.path.exists(self.history_file):
                files.append(discord.File(self.history_file))
            
            if files:
                # シンプルでシステム的な表示
                embed = discord.Embed(
                    description=(
                        f"💾 **System State Synced**\n"
                        f"実効予約数: "
                        f"`{len([j for j in self.scheduler.get_jobs() if not j.id.startswith(('pre_', 'snooze_'))])}` "
                        f" | 更新時刻: `{datetime.now(JST).strftime('%H:%M:%S')}`"
                    ),
                    color=discord.Color.dark_grey()
                )
                new_msg = await self.storage_channel.send(embed=embed, files=files)
                logger.info(f"Data synced to storage channel (Jobs: {len(self.scheduler.get_jobs())})")
                
                async def cleanup():
                    try:
                        # 最新のメッセージ以外を掃除（ストレージを清潔に保つ）
                        await self.storage_channel.purge(
                            limit=20,
                            check=lambda m: m.author == self.user and m.id != new_msg.id,
                            before=new_msg
                        )
                    except Exception as e:
                        logger.warning(f"Storage cleanup had a minor issue: {e}")
                
                # 掃除はバックグラウンドで実行
                self.loop.create_task(cleanup()) 
        except Exception as e:
            logger.error(f"Failed to upload data: {e}")

    async def download_data_from_channel(self):
        """指定のチャンネルから最新のバックアップをダウンロードする"""
        if not self.storage_channel:
            logger.warning("Storage channel not available. Skipping download.")
            return
        try:
            found_db = False
            found_history = False
            
            async for message in self.storage_channel.history(limit=100):
                if message.author == self.user and message.attachments:
                    for attachment in message.attachments:
                        if attachment.filename == "jobs.sqlite" and not found_db:
                            await attachment.save(self.db_file)
                            found_db = True
                            logger.info("Downloaded jobs.sqlite")
                        elif attachment.filename == "history.json" and not found_history:
                            await attachment.save(self.history_file)
                            found_history = True
                            logger.info("Downloaded history.json")
                    
                if found_db and found_history:
                    break
            
            # ダウンロード完了後にメモリに展開
            if found_history:
                self.history = self.load_history()
                logger.info(f"History synchronized: {len(self.history)} entries loaded.")

        except Exception as e:
            logger.error(f"Failed to download data: {e}")

    def save_history(self):
        """履歴をJSONファイルに保存する"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=4)
            # 履歴の変更もまとめ役に依頼
            self.request_sync()
        except Exception as e:
            logger.error(f"Error in save_history: {e}")

    async def close(self):
        """シャットダウン前にデータをアップロードする"""
        logger.info("Bot is shutting down. Finalizing state...")
        if self._sync_wait_task and not self._sync_wait_task.done():
            self._sync_wait_task.cancel() # 待機中の5秒タイマーをキャンセル
        
        await self.upload_data_to_channel() # 待たずに即座に最終保存
        self.scheduler.shutdown()
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
        now = datetime.now(JST) # UTCではなくJSTで統一
        for job in self.scheduler.get_jobs():
            try:
                # cronトリガー（繰り返し）は掃除の対象外
                if hasattr(job.trigger, 'fields'):
                    continue
                # 次の実行予定がない、または過去である「一度きり」のジョブを削除
                if job.next_run_time is None or job.next_run_time < now:
                    self.scheduler.remove_job(job.id)
                    cleaned_count += 1
            except:
                continue

        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} stale jobs from database.")
            # 掃除した結果をストレージに同期
            await self.upload_data_to_channel()

        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        
        # daveyの存在チェック
        try:
            import davey
            logger.info("Voice library 'davey' is correctly installed.")
        except ImportError:
            logger.error("CRITICAL: 'davey' library not found. Voice playback will fail!")

        # 音声フォルダーの作成
        if not os.path.exists(AUDIO_DIR):
            os.makedirs(AUDIO_DIR)
            logger.info(f"Created directory: {AUDIO_DIR}")

        # ffmpegの存在確認
        if not shutil.which("ffmpeg"):
            logger.error("FFmpeg was not found in the system PATH. Audio playback will fail.")

bot = AlarmBot()
if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing. Please set it in GitHub Secrets or .env file.")
    else:
        bot.run(TOKEN)

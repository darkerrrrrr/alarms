import os
import asyncio
import logging
from datetime import datetime, timedelta
import sqlite3
import discord
from discord.ext import commands
from apscheduler.events import EVENT_JOB_REMOVED, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED
from utils import JST

logger = logging.getLogger(__name__)

# --- 設定定数 ---
HISTORY_RETENTION_DAYS = 3  # 履歴を保持する日数（3日経ったら自動削除）
SYNC_DELAY_SECONDS = 60     # 同期の待機時間（秒）。頻繁なアップロードを抑える。

class StorageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.storage_channel = None
        self._sync_wait_task = None
        self.init_db()

    def cog_unload(self):
        """Cogがアンロードされる際にスケジューラーのリスナーを解除する"""
        self.bot.scheduler.remove_listener(self.on_job_change)

    def init_db(self):
        """SQLiteに履歴テーブルを作成する"""
        try:
            with sqlite3.connect(self.bot.db_file) as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS history 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                                 user_id TEXT, time TEXT, days TEXT, 
                                 set_at TEXT, category TEXT)''')
                conn.commit()
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")

    def get_history(self, user_id=None):
        """DBから履歴を取得する"""
        try:
            with sqlite3.connect(self.bot.db_file) as conn:
                conn.row_factory = sqlite3.Row
                if user_id:
                    cur = conn.execute("SELECT * FROM history WHERE user_id = ? ORDER BY set_at DESC LIMIT 100", (str(user_id),))
                else:
                    cur = conn.execute("SELECT * FROM history ORDER BY set_at DESC LIMIT 5")
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get history: {e}")
            return []

    def add_history(self, user_id, time, days, category):
        """履歴をDBに直接挿入し、古いデータをクリーンアップする"""
        now = datetime.now(JST)
        set_at = now.isoformat()
        threshold = (now - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()

        try:
            with sqlite3.connect(self.bot.db_file) as conn:
                conn.execute("INSERT INTO history (user_id, time, days, set_at, category) VALUES (?, ?, ?, ?, ?)",
                             (str(user_id), time, days, set_at, category))
                # 1週間以上前のデータを削除
                conn.execute("DELETE FROM history WHERE set_at < ?", (threshold,))
                conn.commit()
            self.request_sync()
        except Exception as e:
            logger.error(f"Error in add_history: {e}")

    def on_job_change(self, event):
        """ジョブに変更があった際に同期を依頼する"""
        self.request_sync()

    def request_sync(self):
        """同期（バックアップ）を依頼する。連続した依頼は指定秒数待ってから1回にまとめる。"""
        async def delayed_sync():
            await asyncio.sleep(SYNC_DELAY_SECONDS)
            await self.upload_data_to_channel()

        if self._sync_wait_task and not self._sync_wait_task.done():
            self._sync_wait_task.cancel()
        self._sync_wait_task = self.bot.loop.create_task(delayed_sync())

    async def ensure_storage_channel(self, guild_id, storage_channel_id):
        """ストレージ用チャンネルを確認・作成する"""
        guild = self.bot.get_guild(int(guild_id)) if guild_id else None
        if not guild:
            async for g in self.bot.fetch_guilds(limit=1):
                guild = await self.bot.fetch_guild(g.id)
                break
        if not guild: return

        try:
            if storage_channel_id and str(storage_channel_id).isdigit():
                cid = int(storage_channel_id)
                self.storage_channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                if self.storage_channel: return
        except (discord.NotFound, discord.Forbidden, ValueError):
            logger.warning("Specified storage channel not found or inaccessible. Searching by name...")

        all_channels = await guild.fetch_channels()
        channel = discord.utils.get(all_channels, name="storage")
        if not channel:
            app_info = await self.bot.application_info()
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                self.bot.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True),
                app_info.owner: discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=False)
            }
            channel = await guild.create_text_channel("storage", overwrites=overwrites, topic="アラームちゃんの活動記録")
        self.storage_channel = channel

    async def upload_data_to_channel(self):
        """バックアップをアップロードする"""
        if not self.storage_channel: return
        try:
            if not os.path.exists(self.bot.db_file): return
            files = [discord.File(self.bot.db_file)]

            embed = discord.Embed(title="🍦 アラームちゃんのバックアップ", color=discord.Color.from_rgb(255, 240, 245))
            recent = self.get_history()
            if recent:
                log = "".join([f"{'🍅' if h.get('category')=='pomodoro' else '⏰'} `{h.get('time')}`\n" for h in reversed(recent)])
                embed.add_field(name="直近の活動記録", value=log, inline=False)
            
            job_count = len([j for j in self.bot.scheduler.get_jobs() if not j.id.startswith(('pre_', 'snooze_'))])
            embed.add_field(name="待機中の予約", value=f"`{job_count}` 件", inline=True)
            embed.set_footer(text=f"同期完了: {datetime.now(JST).strftime('%m/%d %H:%M:%S')}")

            new_msg = await self.storage_channel.send(embed=embed, files=files, silent=True)
            
            # 5世代残して古いメッセージを削除
            count = 0
            async for m in self.storage_channel.history(limit=50):
                if m.author == self.bot.user:
                    count += 1
                    if count > 5: await m.delete()
        except Exception as e:
            logger.error(f"Upload failed: {e}")

    async def download_data_from_channel(self):
        """最新のバックアップをダウンロードする"""
        if not self.storage_channel: return
        try:
            found_db = False
            async for message in self.storage_channel.history(limit=100):
                if message.author == self.bot.user and message.attachments:
                    for attach in message.attachments:
                        if attach.filename == "jobs.sqlite" and not found_db:
                            await attach.save(self.bot.db_file); found_db = True
                if found_db: break
        except Exception as e:
            logger.error(f"Download failed: {e}")

    async def grant_storage_access(self, member):
        """ユーザーに閲覧権限を付与"""
        if not self.storage_channel or not isinstance(member, discord.Member): return
        try:
            await self.storage_channel.set_permissions(member, view_channel=True, read_messages=True, send_messages=False)
        except discord.NotFound:
            self.storage_channel = None # チャンネルが消えていたら参照を消す
        except Exception as e:
            logger.error(f"Failed to grant storage access: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if self.storage_channel and channel.id == self.storage_channel.id:
            await self.ensure_storage_channel(channel.guild.id, None)
            await self.upload_data_to_channel()

    @commands.Cog.listener()
    async def on_message(self, message):
        if self.storage_channel and message.channel.id == self.storage_channel.id:
            if message.author != self.bot.user and message.type == discord.MessageType.default:
                await message.delete()

async def setup(bot):
    storage_cog = StorageCog(bot)
    await bot.add_cog(storage_cog)
    # スケジューラーの監視設定（メソッドを直接登録）
    bot.scheduler.add_listener(storage_cog.on_job_change, EVENT_JOB_REMOVED | EVENT_JOB_EXECUTED | EVENT_JOB_ADDED | EVENT_JOB_MODIFIED)
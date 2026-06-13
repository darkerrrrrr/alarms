import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from apscheduler.events import EVENT_JOB_REMOVED, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED
from utils import JST

logger = logging.getLogger(__name__)

class StorageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.storage_channel = None
        self._sync_wait_task = None
        self.history = self.load_history()

    def load_history(self):
        """履歴をJSONファイルから読み込む"""
        if os.path.exists(self.bot.history_file):
            with open(self.bot.history_file, 'r', encoding='utf-8') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return []
        return []

    def save_history(self):
        """履歴を整理してJSONファイルに保存し、同期を依頼する"""
        now = datetime.now(JST)
        threshold = now - timedelta(days=7) # 1週間分保持

        self.history = [
            h for h in self.history 
            if datetime.fromisoformat(h.get("set_at")).replace(tzinfo=JST if datetime.fromisoformat(h.get("set_at")).tzinfo is None else None) > threshold
        ][:1000] # 最大1000件

        try:
            with open(self.bot.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=4)
            self.request_sync()
        except Exception as e:
            logger.error(f"Error in save_history: {e}")

    def request_sync(self):
        """同期（バックアップ）を依頼する。連続した依頼は5秒待ってから1回にまとめる。"""
        async def delayed_sync():
            await asyncio.sleep(5)
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

        if storage_channel_id:
            self.storage_channel = self.bot.get_channel(int(storage_channel_id))
            if self.storage_channel: return

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
            files = [discord.File(p) for p in [self.bot.db_file, self.bot.history_file] if os.path.exists(p)]
            if not files: return

            embed = discord.Embed(title="🍦 アラームちゃんのバックアップ", color=discord.Color.from_rgb(255, 240, 245))
            recent = self.history[-5:]
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
            found_db = found_hist = False
            async for message in self.storage_channel.history(limit=100):
                if message.author == self.bot.user and message.attachments:
                    for attach in message.attachments:
                        if attach.filename == "jobs.sqlite" and not found_db:
                            await attach.save(self.bot.db_file); found_db = True
                        elif attach.filename == "history.json" and not found_hist:
                            await attach.save(self.bot.history_file); found_hist = True
                if found_db and found_hist: break
            if found_hist: self.history = self.load_history()
        except Exception as e:
            logger.error(f"Download failed: {e}")

    async def grant_storage_access(self, member):
        """ユーザーに閲覧権限を付与"""
        if self.storage_channel and isinstance(member, discord.Member):
            await self.storage_channel.set_permissions(member, view_channel=True, read_messages=True, send_messages=False)

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
    
    # スケジューラーの監視設定
    def on_job_change(event):
        storage_cog.request_sync()
    bot.scheduler.add_listener(on_job_change, EVENT_JOB_REMOVED | EVENT_JOB_EXECUTED | EVENT_JOB_ADDED | EVENT_JOB_MODIFIED)
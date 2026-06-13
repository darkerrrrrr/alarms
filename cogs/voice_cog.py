import os
import asyncio
import logging
import random
import shutil
from datetime import datetime
import discord
from discord.ext import commands
from utils import JST, AUDIO_DIR
from views import AlarmView, PomodoroView

logger = logging.getLogger(__name__)
_bot = None

async def task_execute_alarm(*args, **kwargs):
    cog = _bot.get_cog('VoiceCog')
    if cog: await cog.execute_alarm(*args, **kwargs)

async def task_pre_notify(*args, **kwargs):
    cog = _bot.get_cog('VoiceCog')
    if cog: await cog.pre_notify(*args, **kwargs)

async def task_execute_pomodoro(*args, **kwargs):
    cog = _bot.get_cog('VoiceCog')
    if cog: await cog.execute_pomodoro(*args, **kwargs)

class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_playbacks = {}

    @commands.Cog.listener()
    async def on_ready(self):
        """ボット起動時に音声環境のセルフチェックを行う"""
        if not os.path.exists(AUDIO_DIR):
            os.makedirs(AUDIO_DIR)
            logger.info(f"Created directory: {AUDIO_DIR}")
        
        if not shutil.which("ffmpeg"):
            logger.error("FFmpeg was not found. Audio playback will fail!")

    async def pre_notify(self, text_channel_id, job_id, time_str, memo=None):
        channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)
        if channel:
            embed = discord.Embed(title=f"⏳ もうすぐ「{memo or '時間'}」です", description=f"**{time_str}** にアラームが鳴ります。", color=discord.Color.blue())
            await channel.send(embed=embed, silent=True)

    async def execute_alarm(self, guild_id, text_channel_id, user_id, job_id, volume, time_str, memo=None, repeat_info="一度きり"):
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if not member or not member.voice: return

        try:
            vc = await member.voice.channel.connect()
            stop_event = asyncio.Event()
            self.active_playbacks[job_id] = {'vc': vc, 'stop_event': stop_event}
            
            files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(('.mp3', '.wav', '.ogg'))]
            audio_path = os.path.join(AUDIO_DIR, random.choice(files))

            def play_loop(error):
                if job_id in self.active_playbacks and not stop_event.is_set() and vc.is_connected():
                    vc.play(discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(audio_path), volume=volume), after=play_loop)
                else:
                    asyncio.run_coroutine_threadsafe(vc.disconnect(), self.bot.loop)
                    self.active_playbacks.pop(job_id, None)

            vc.play(discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(audio_path), volume=volume), after=play_loop)

            if job_id.startswith(('alarm_', 'once_')) and self.bot.storage:
                self.bot.storage.history.append({"user_id": user_id, "time": f"{time_str} ({memo})" if memo else time_str, "days": repeat_info, "set_at": datetime.now(JST).isoformat(), "category": "alarm"})
                self.bot.storage.save_history()

            if any(job_id.startswith(p) for p in ["alarm_", "once_", "snooze_"]):
                channel = self.bot.get_channel(text_channel_id)
                view = AlarmView(self.bot, guild_id, user_id, text_channel_id, volume, time_str, job_id, memo)
                await channel.send(embed=discord.Embed(title=f"🔔 {time_str} です！", description=f"📝 {memo or 'なし'}", color=discord.Color.gold()), view=view, silent=True)
        except:
            if guild.voice_client: await guild.voice_client.disconnect()

    async def execute_pomodoro(self, guild_id, text_channel_id, user_id, job_id, volume, work_mins, rest_mins, was_work, cycle_count, memo=None):
        await self.execute_alarm(guild_id, text_channel_id, user_id, job_id, volume, "ポモドーロ終了", memo, "ポモドーロ")
        
        if was_work:
            cycle_count += 1
            if self.bot.storage:
                self.bot.storage.history.append({"user_id": user_id, "time": f"{memo} {cycle_count}回目完了" if memo else f"{cycle_count}回目完了", "days": "ポモドーロ作業", "set_at": datetime.now(JST).isoformat(), "category": "pomodoro"})
                self.bot.storage.save_history()

        channel = self.bot.get_channel(text_channel_id)
        if channel:
            status = "作業" if was_work else "休憩"
            view = PomodoroView(self.bot, guild_id, user_id, text_channel_id, volume, work_mins, rest_mins, was_work, cycle_count, memo, job_id)
            await channel.send(embed=discord.Embed(title=f"✨ {status}セッション完了！", description=f"次は **{'休憩' if was_work else '作業'}** です。", color=discord.Color.blue()), view=view, silent=True)

    async def stop_playback(self, job_id):
        if job_id in self.active_playbacks:
            info = self.active_playbacks[job_id]
            info['stop_event'].set()
            if info['vc'].is_playing(): info['vc'].stop()
            elif info['vc'].is_connected(): await info['vc'].disconnect()

async def setup(bot):
    global _bot
    _bot = bot
    await bot.add_cog(VoiceCog(bot))
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
            # スケジュールされたジョブから次の実行時刻を取得して動的タイムスタンプを生成
            target_ts = ""
            job = self.bot.scheduler.get_job(job_id)
            if job and job.next_run_time:
                ts = int(job.next_run_time.timestamp())
                target_ts = f"\n予定時刻: <t:{ts}:t> (**<t:{ts}:R>**)"

            display_memo = memo.replace("@time", time_str) if memo else "時間"
            embed = discord.Embed(title=f"⏳ もうすぐ「{display_memo}」です", description=f"**{time_str}** にアラームが鳴ります。{target_ts}", color=discord.Color.blue())
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

            display_memo = memo.replace("@time", time_str) if memo else memo

            if job_id.startswith(('alarm_', 'once_')) and self.bot.storage:
                self.bot.storage.add_history(
                    user_id=user_id,
                    time=f"{time_str} ({display_memo})" if display_memo else time_str,
                    days=repeat_info,
                    category="alarm"
                )

            if any(job_id.startswith(p) for p in ["alarm_", "once_", "snooze_"]):
                channel = self.bot.get_channel(text_channel_id)
                view = AlarmView(self.bot, guild_id, user_id, text_channel_id, volume, time_str, job_id, display_memo)
                await channel.send(embed=discord.Embed(title=f"🔔 {time_str} です！", description=f"📝 {display_memo or 'なし'}", color=discord.Color.gold()), view=view, silent=True)
        except:
            if guild.voice_client: await guild.voice_client.disconnect()

    async def execute_pomodoro(self, guild_id, text_channel_id, user_id, job_id, volume, work_mins, rest_mins, was_work, cycle_count, memo=None):
        # 「ポモドーロ終了」という文字の代わりに、実際の現在時刻を渡して @time 置換を機能させる
        current_time_str = datetime.now(JST).strftime('%H:%M')
        await self.execute_alarm(guild_id, text_channel_id, user_id, job_id, volume, current_time_str, memo, "ポモドーロ")
        
        if was_work:
            cycle_count += 1
            if self.bot.storage:
                self.bot.storage.add_history(
                    user_id=user_id,
                    time=f"{memo} {cycle_count}回目完了" if memo else f"{cycle_count}回目完了",
                    days="ポモドーロ作業",
                    category="pomodoro"
                )

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
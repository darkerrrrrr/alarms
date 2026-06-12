import os
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands

from utils import JST
from views import PomodoroView

logger = logging.getLogger(__name__)

_bot = None

async def task_execute_pomodoro(*args, **kwargs):
    """ポモドーロ実行用のグローバルハンドラー"""
    cog = _bot.get_cog('PomodoroCog')
    if cog:
        await cog.execute_pomodoro(*args, **kwargs)

class PomodoroCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def execute_pomodoro(self, guild_id: int, text_channel_id: int, voice_channel_id: int, job_id: str, volume: float, work_mins: int, rest_mins: int, was_work: bool, cycle_count: int):
        """ポモドーロセッションの終了処理と次フェーズの予約"""
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            # 音声を鳴らす（AlarmCogのexecute_alarmロジックを流用）
            await alarm_cog.execute_alarm(guild_id, text_channel_id, voice_channel_id, job_id, volume, "ポモドーロ")

        if was_work:
            cycle_count += 1
            # 進捗を履歴(JSON)に保存
            now = datetime.now(JST)
            user_id = int(job_id.split('_')[2])
            self.bot.history.append({
                "user_id": user_id,
                "time": f"{cycle_count}回目完了",
                "days": "ポモドーロ作業",
                "set_at": now.isoformat(),
                "category": "pomodoro"
            })
            self.bot.save_history()

        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)
        if not text_channel:
            return

        # 次のフェーズの案内を表示
        status_msg = "作業" if was_work else "休憩"
        next_phase = "休憩" if was_work else "作業"

        # 作業終了時と休憩終了時でViewを表示
        embed = discord.Embed(title=f"⏰ {status_msg}終了", description=f"{status_msg}セッションが終了しました。次は**{next_phase}**です。\n開始しますか？", color=discord.Color.gold())
        view = PomodoroView(self.bot, guild_id, text_channel_id, voice_channel_id, volume, work_mins, rest_mins, was_work, cycle_count)
        await text_channel.send(embed=embed, view=view)

    @app_commands.command(name="pomodoro", description="作業と休憩のサイクル（ポモドーロ・タイマー）を開始します")
    @app_commands.describe(
        work_mins="作業する時間（分）",
        rest_mins="休憩する時間（分）",
        volume="音量 (0.1〜1.0)"
    )
    async def pomodoro(self, interaction: discord.Interaction, work_mins: int = 25, rest_mins: int = 5, volume: float = 0.5):
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ ボイスチャンネルに入った状態で実行してください。", ephemeral=True)

        if work_mins <= 0 or rest_mins <= 0:
            return await interaction.response.send_message("⚠️ 時間は1分以上で指定してください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            work_end = now + timedelta(minutes=work_mins)
            
            # ジョブIDの生成 (pomo_user_time)
            job_id = f"pomo_work_{interaction.user.id}_{work_end.strftime('%H%M%S')}"

            # 既存のポモドーロがあれば削除（1人1つまで）
            jobs = self.bot.scheduler.get_jobs()
            for job in jobs:
                if job.id.startswith(f"pomo_") and str(interaction.user.id) in job.id:
                    self.bot.scheduler.remove_job(job.id)

            # 作業終了時のタスクを登録
            self.bot.scheduler.add_job(
                task_execute_pomodoro, 'date', run_date=work_end,
                args=[interaction.guild.id, interaction.channel.id, interaction.user.voice.channel.id, job_id, volume, work_mins, rest_mins, True, 0],
                id=job_id
            )

            embed = discord.Embed(
                title="🍅 ポモドーロ・タイマー開始",
                description="作業セッションを開始しました。集中しましょう！",
                color=discord.Color.red()
            )
            embed.add_field(name="✍️ 作業時間", value=f"{work_mins}分", inline=True)
            embed.add_field(name="☕ 休憩時間", value=f"{rest_mins}分", inline=True)
            embed.add_field(name="⏰ 作業終了予定", value=f"`{work_end.strftime('%H:%M:%S')}`", inline=False)
            embed.set_footer(text="途中で止める場合は /cancel を使用してください")

            await interaction.response.send_message(embed=embed)
        except Exception as e:
            logger.exception(f"Pomodoro start failed: {e}")
            await interaction.response.send_message("⚠️ タイマーの開始に失敗しました。", ephemeral=True)

    @app_commands.command(name="status", description="ポモドーロ・タイマーの残り時間を表示します")
    async def status(self, interaction: discord.Interaction):
        """現在実行中のポモドーロセッションの残り時間を表示"""
        jobs = self.bot.scheduler.get_jobs()
        user_id_str = str(interaction.user.id)
        pomo_job = None

        for job in jobs:
            if job.id.startswith("pomo_") and user_id_str in job.id:
                pomo_job = job
                break

        if not pomo_job:
            return await interaction.response.send_message("❌ 現在実行中のポモドーロ・タイマーはありません。", ephemeral=True)

        now = datetime.now(JST)
        remaining = pomo_job.next_run_time.astimezone(JST) - now
        seconds = max(int(remaining.total_seconds()), 0)
        mins, secs = divmod(seconds, 60)

        is_work = "work" in pomo_job.id
        status_type = "✍️ 作業中" if is_work else "☕ 休憩中"
        color = discord.Color.red() if is_work else discord.Color.blue()

        embed = discord.Embed(
            title=f"🍅 ポモドーロ・ステータス ({status_type})",
            description=f"残り時間: **{mins}分{secs}秒**",
            color=color,
            timestamp=now
        )
        embed.add_field(name="⏰ 終了予定", value=f"`{pomo_job.next_run_time.astimezone(JST).strftime('%H:%M:%S')}`")
        embed.set_footer(text=f"ID: {pomo_job.id}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pomo_stats", description="今日のポモドーロ達成状況を確認します")
    async def pomo_stats(self, interaction: discord.Interaction):
        """今日の作業完了回数を集計して表示"""
        user_id = interaction.user.id
        today_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        
        count = 0
        for h in self.bot.history:
            if h.get("user_id") == user_id and h.get("category") == "pomodoro":
                set_at_dt = datetime.fromisoformat(h["set_at"])
                if set_at_dt >= today_start:
                    count += 1

        embed = discord.Embed(
            title="📊 今日の作業レポート",
            description=f"{interaction.user.display_name} さん、お疲れ様です！",
            color=discord.Color.green()
        )
        embed.add_field(name="🍅 完了したポモドーロ", value=f"**{count} 回**", inline=True)
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    global _bot
    _bot = bot
    await bot.add_cog(PomodoroCog(bot))
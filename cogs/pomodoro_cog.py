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

    async def execute_pomodoro(self, guild_id: int, text_channel_id: int, user_id: int, job_id: str, volume: float, work_mins: int, rest_mins: int, was_work: bool, cycle_count: int, memo: str = None):
        """ポモドーロセッションの終了処理と次フェーズの予約"""
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            # 音声を鳴らす（AlarmCogのexecute_alarmロジックを流用）
            display_memo = memo if memo else "ポモドーロ"
            await alarm_cog.execute_alarm(guild_id, text_channel_id, user_id, job_id, volume, f"{display_memo}終了", display_memo)

        now = datetime.now(JST)
        if was_work:
            cycle_count += 1
            # 進捗を履歴(JSON)に保存
            self.bot.history.append({
                "user_id": user_id,
                "time": f"{memo} {cycle_count}回目完了" if memo else f"{cycle_count}回目完了",
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
        view = PomodoroView(self.bot, guild_id, user_id, text_channel_id, volume, work_mins, rest_mins, was_work, cycle_count, memo)
        await text_channel.send(embed=embed, view=view)

    @app_commands.command(name="pomodoro", description="作業と休憩のサイクル（ポモドーロ・タイマー）を開始します")
    @app_commands.describe(
        work_mins="作業する時間（分）",
        rest_mins="休憩する時間（分）",
        volume="音量 (0.1〜1.0)",
        memo="タイマーの内容（例: 勉強、仕事など）"
    )
    async def pomodoro(self, interaction: discord.Interaction, work_mins: int = 25, rest_mins: int = 5, volume: float = 0.5, memo: str = None):
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
                args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, volume, work_mins, rest_mins, True, 0, memo],
                id=job_id
            )

            embed = discord.Embed(
                title="🍅 ポモドーロ・タイマー開始",
                description=f"「**{memo}**」の作業セッションを開始しました。集中しましょう！" if memo else "作業セッションを開始しました。集中しましょう！",
                color=discord.Color.red()
            )
            embed.add_field(name="✍️ 作業時間", value=f"{work_mins}分", inline=True)
            embed.add_field(name="☕ 休憩時間", value=f"{rest_mins}分", inline=True)
            embed.add_field(name="⏰ 作業終了予定", value=f"`{work_end.strftime('%H:%M:%S')}`", inline=False)
            embed.set_footer(text="途中で止める場合は /cancel を使用してください")

            # 開始メッセージを本人にのみ表示
            await interaction.response.send_message(embed=embed, ephemeral=True) # エフェメラル化
        except Exception as e:
            logger.exception(f"Pomodoro start failed: {e}")
            await interaction.response.send_message("⚠️ タイマーの開始に失敗しました。", ephemeral=True)


async def setup(bot: commands.Bot):
    global _bot
    _bot = bot
    await bot.add_cog(PomodoroCog(bot))
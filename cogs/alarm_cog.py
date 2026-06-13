from datetime import datetime, timedelta
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.jobstores.base import JobLookupError
from utils import JST, parse_days_to_cron, alarm_id_autocomplete, day_of_week_autocomplete, time_autocomplete
from cogs.voice_cog import task_execute_alarm, task_pre_notify

class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def stop_playback(self, job_id: str):
        engine = self.bot.get_cog('VoiceCog')
        if engine: await engine.stop_playback(job_id)

    @app_commands.command(name="alarm", description="アラームをセットします")
    @app_commands.autocomplete(day_of_week=day_of_week_autocomplete, time_str=time_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, time_str: str, memo: str = None, repeat: bool = True, day_of_week: str = "毎日", volume: float = 0.5):
        if self.bot.storage: await self.bot.storage.grant_storage_access(interaction.user)
        if not interaction.user.voice: return await interaction.response.send_message("❌ VCに入ってください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            # 相対時間（数値のみ）か絶対時間（HH:MM）かを判定
            if time_str.isdigit():
                target_time = now + timedelta(minutes=int(time_str))
                actual_time_str = target_time.strftime('%H:%M')
            else:
                time_obj = datetime.strptime(time_str, "%H:%M")
                target_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)
                actual_time_str = time_str

            time_id = target_time.strftime('%H%M')
            cron_days = parse_days_to_cron(day_of_week)
            
            for prefix in ['alarm', 'once']:
                for p in ['', 'pre_']:
                    try: self.bot.scheduler.remove_job(f"{p}{prefix}_{interaction.user.id}_{time_id}")
                    except: pass

            if repeat:
                # 確認メッセージ用の時刻計算（既に過ぎている場合は明日を表示）
                confirm_time = target_time
                if confirm_time <= now:
                    confirm_time += timedelta(days=1)
                ts = int(confirm_time.timestamp())
                
                # 繰り返し設定 (cron)
                job_id = f"alarm_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'cron', day_of_week=cron_days, hour=target_time.hour, minute=target_time.minute,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, volume, actual_time_str, memo, day_of_week],
                    id=job_id
                )
                # 5分前通知の登録
                pre_time = target_time - timedelta(minutes=5)
                self.bot.scheduler.add_job(
                    task_pre_notify, 'cron', day_of_week=cron_days, hour=pre_time.hour, minute=pre_time.minute,
                    args=[interaction.channel.id, job_id, actual_time_str, memo],
                    id=f"pre_{job_id}"
                )
                description = f"指定した曜日（{day_of_week}）に繰り返します。\n次は <t:{ts}:t> (**<t:{ts}:R>**) です。"
            else:
                if target_time <= now: target_time += timedelta(days=1)
                
                m = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                target_weekdays = [m[d] for d in cron_days.split(",")] if cron_days != "*" else list(range(7))
                while target_time.weekday() not in target_weekdays:
                    target_time += timedelta(days=1)
                
                job_id = f"once_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'date', run_date=target_time,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, volume, actual_time_str, memo, "一度きり"],
                    id=job_id
                )
                # 5分前通知
                pre_time = target_time - timedelta(minutes=5)
                if pre_time > now:
                    self.bot.scheduler.add_job(
                        task_pre_notify, 'date', run_date=pre_time,
                        args=[interaction.channel.id, job_id, actual_time_str, memo],
                        id=f"pre_{job_id}"
                    )
                ts = int(target_time.timestamp())
                description = f"🗓️ <t:{ts}:d> に一度のみ実行します。\n予定: <t:{ts}:t> (**<t:{ts}:R>**)"
            await interaction.response.send_message(f"✅ アラームをセットしました。\n{description}", ephemeral=True)
        except:
            await interaction.response.send_message("⚠️ 時刻形式エラー (`HH:mm` または `10` のような分数を入力してください)", ephemeral=True)

    @app_commands.command(name="alarms", description="予約中の自分のアラームを表示します")
    async def list_alarms(self, interaction: discord.Interaction):
        if self.bot.storage: await self.bot.storage.grant_storage_access(interaction.user)
        jobs = self.bot.scheduler.get_jobs()
        user_jobs = sorted([j for j in jobs if str(interaction.user.id) in j.id and not j.id.startswith('pre_')], key=lambda x: x.next_run_time)

        if not user_jobs: return await interaction.response.send_message("予約なし", ephemeral=True)

        lines = []
        for j in user_jobs:
            ts = int(j.next_run_time.timestamp())
            # ジョブの引数からメモ(memo)を抽出して表示を分かりやすくする
            # アラームなら index 6, ポモドーロなら index 9
            memo = ""
            if j.id.startswith('pomo_'):
                memo = j.args[9] if len(j.args) > 9 else "ポモドーロ"
            else:
                memo = j.args[6] if len(j.args) > 6 else "なし"
            
            lines.append(f"<t:{ts}:t> (<t:{ts}:R>) - **{memo}** (`{j.id[:8]}...`)")
        
        await interaction.response.send_message(f"⏰ **現在の予約スケジュール**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="cancel", description="セットしたアラームを一覧から選択して解除します")
    @app_commands.autocomplete(alarm_selection=alarm_id_autocomplete)
    async def cancel_alarm(self, interaction: discord.Interaction, alarm_selection: str):
        try:
            self.bot.scheduler.remove_job(alarm_selection)
            try: self.bot.scheduler.remove_job(f"pre_{alarm_selection}")
            except: pass
            await interaction.response.send_message("🗑️ キャンセルしました。", ephemeral=True)
        except:
            await interaction.response.send_message("❌ 見つかりません。", ephemeral=True)

async def setup(bot): await bot.add_cog(AlarmCog(bot))
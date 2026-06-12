import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone # timezoneも必要

import discord
from discord.ext import commands
from discord import app_commands

from utils import JST, AUDIO_DIR, parse_days_to_cron, alarm_id_autocomplete, day_of_week_autocomplete
from views import AlarmView

# main.pyからJSTをインポートしているので、ここで再定義は不要
# from utils import JST # utilsからインポート済み
logger = logging.getLogger(__name__)

class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def pre_notify(self, text_channel_id: int, job_id: str, time_str: str):
        """アラームの5分前に通知するタスク"""
        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)
        if text_channel:
            embed = discord.Embed(
                title="⏳ もうすぐ時間です",
                description=f"**{time_str}** にアラームが鳴ります。（あと5分）\nID: `{job_id}`",
                color=discord.Color.blue()
            )
            await text_channel.send(embed=embed)

    async def execute_alarm(self, guild_id: int, text_channel_id: int, voice_channel_id: int, job_id: str, volume: float, time_str: str):
        """指定された時刻にボイスチャンネルへ参加し、音声を再生して切断するタスク"""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        voice_channel = self.bot.get_channel(voice_channel_id) or await self.bot.fetch_channel(voice_channel_id)
        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)

        if not voice_channel:
            logger.error(f"Voice Channel ID {voice_channel_id} not found.")
            return

        # 権限の確認
        permissions = voice_channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            if text_channel:
                await text_channel.send(f"⚠️ ボイスチャンネル `{voice_channel.name}` に接続、または発言する権限がありません。")
            return

        try:
            # すでに接続中の場合は一度切断
            if guild.voice_client:
                await guild.voice_client.disconnect()

            # 接続
            vc = await voice_channel.connect()
            logger.info(f"Connected to {voice_channel.name} for Job: {job_id}")

            # soundsフォルダからランダムにファイルを選択
            files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(('.mp3', '.wav', '.ogg'))]
            if not files:
                if text_channel:
                    await text_channel.send(f"⚠️ `{AUDIO_DIR}` フォルダに音声ファイルが見つかりません。")
                await vc.disconnect()
                return
            
            selected_sound = random.choice(files)
            audio_source = os.path.join(AUDIO_DIR, selected_sound)

            # 再生終了後のクリーンアップ処理
            def after_playing(error):
                if error:
                    logger.error(f"Playback error: {error}")
                
                coro = vc.disconnect()
                future = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Error during disconnect: {e}")

            # 音声再生
            ffmpeg_audio = discord.FFmpegPCMAudio(audio_source)
            volume_audio = discord.PCMVolumeTransformer(ffmpeg_audio)
            volume_audio.volume = volume
            vc.play(volume_audio, after=after_playing)
            # ポモドーロなどの他機能での呼び出し時は、そちらでメッセージを出すため、アラーム/スヌーズ時のみ表示
            if text_channel and (job_id.startswith("alarm_") or job_id.startswith("snooze_")):
                embed = discord.Embed(
                    title=f"🔔 {time_str} です！",
                    description=f"予定の時間になりました。通話の区切りなどに活用してください。\n\n停止するかスヌーズを選択してください。",
                    color=discord.Color.gold()
                )
                view = AlarmView(self.bot, guild_id, voice_channel_id, text_channel_id, volume, time_str)
                await text_channel.send(embed=embed, view=view)

        except Exception as e:
            logger.exception(f"Failed to execute alarm: {e}")
            if guild.voice_client:
                await guild.voice_client.disconnect()

    @app_commands.command(name="alarm", description="アラームをセットします（曜日の指定）")
    @app_commands.describe(
        time_str="時刻を入力してください (例: 07:30)",
        day_of_week="鳴らしたい曜日を入力してください (例: 月水金)",
        volume="音量を指定してください (0.1〜1.0)"
    )
    @app_commands.autocomplete(day_of_week=day_of_week_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, time_str: str, day_of_week: str, volume: float = 0.5):
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ ボイスチャンネルに入った状態で実行してください。", ephemeral=True)

        if not (0.1 <= volume <= 1.0):
            return await interaction.response.send_message("⚠️ 音量は 0.1 から 1.0 の間で指定してください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            target_time = datetime.strptime(time_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day, second=0, microsecond=0, tzinfo=JST
            )
            cron_days = parse_days_to_cron(day_of_week)
            job_id = f"alarm_{interaction.user.id}_{cron_days}_{target_time.strftime('%H%M%S')}"
            pre_time = target_time - timedelta(minutes=5)
            pre_job_id = f"pre_{job_id}"

            if self.bot.scheduler.get_job(job_id):
                self.bot.scheduler.remove_job(job_id)
            if self.bot.scheduler.get_job(pre_job_id):
                self.bot.scheduler.remove_job(pre_job_id)

            self.bot.scheduler.add_job(
                self.execute_alarm, 'cron', day_of_week=cron_days, hour=target_time.hour, minute=target_time.minute,
                args=[interaction.guild.id, interaction.channel.id, interaction.user.voice.channel.id, job_id, volume, time_str],
                id=job_id
            )

            self.bot.scheduler.add_job(
                self.pre_notify, 'cron', day_of_week=cron_days, hour=pre_time.hour, minute=pre_time.minute,
                args=[interaction.channel.id, job_id, time_str],
                id=pre_job_id
            )

            embed = discord.Embed(title="✅ アラーム予約完了", description=f"指定した曜日（{day_of_week}）に繰り返します", color=discord.Color.green())
            embed.add_field(name="⏰ 設定時刻", value=f"`{target_time.strftime('%H:%M')}`", inline=True)
            embed.add_field(name="🔁 曜日", value=f"`{day_of_week}`", inline=True)
            embed.add_field(name="🔊 音量", value=f"`{volume}`", inline=True)
            embed.add_field(name="🆔 ジョブID", value=f"`{job_id}`", inline=False)

            # 履歴をJSONとして保存
            self.bot.history.append({
                "user_id": interaction.user.id,
                "time": time_str,
                "days": day_of_week,
                "set_at": now.isoformat(),
                "category": "alarm"
            })
            self.bot.save_history()

            await interaction.response.send_message(embed=embed)
        except ValueError:
            await interaction.response.send_message("⚠️ 時刻は `HH:mm` 形式で指定してください。", ephemeral=True)

    @app_commands.command(name="alarms", description="予約中の自分のアラームを表示します")
    async def list_alarms(self, interaction: discord.Interaction):
        jobs = self.bot.scheduler.get_jobs()
        user_id_str = str(interaction.user.id)
        user_jobs = [j for j in jobs if user_id_str in j.id and not j.id.startswith("snooze_")] # スヌーズは除外

        if not user_jobs:
            return await interaction.response.send_message("現在予約されているアラームはありません。", ephemeral=True)

        embed = discord.Embed(title="⏰ あなたのアラーム一覧", color=discord.Color.blue(), timestamp=datetime.now(JST))
        for i, job in enumerate(user_jobs, 1):
            # ジョブIDから曜日情報を取得 (alarm_user_days_time)
            job_id_parts = job.id.split('_')
            days_info = f" ({job_id_parts[2]})" if len(job_id_parts) >= 3 else ""
            embed.add_field(
                name=f"{i}. 繰り返しアラーム{days_info}: {job.next_run_time.astimezone(JST).strftime('%H:%M')}",
                value=f"ID: `{job.id}`",
                inline=False
            )
        await interaction.response.send_message(embed=embed)
    @app_commands.command(name="cancel", description="指定したIDのアラームをキャンセルします")
    @app_commands.autocomplete(job_id=alarm_id_autocomplete)
    async def cancel_alarm(self, interaction: discord.Interaction, job_id: str):
        if str(interaction.user.id) not in job_id:
            return await interaction.response.send_message("❌ 自分のアラームIDのみキャンセルできます。", ephemeral=True)

        if self.bot.scheduler.get_job(job_id):
            self.bot.scheduler.remove_job(job_id)
            pre_job_id = f"pre_{job_id}"
            if self.bot.scheduler.get_job(pre_job_id):
                self.bot.scheduler.remove_job(pre_job_id)
            await interaction.response.send_message(f"🗑️ アラーム `{job_id}` をキャンセルしました。")
        else:
            await interaction.response.send_message(f"⚠️ 指定された ID `{job_id}` が見つかりませんでした。", ephemeral=True)
    @app_commands.command(name="history", description="過去にセットしたアラームの履歴（最新10件）を表示します")
    @app_commands.describe(query="検索したい時間や曜日を入力してください (任意)")
    async def alarm_history(self, interaction: discord.Interaction, query: str = None):
        user_history = [h for h in self.bot.history if h["user_id"] == interaction.user.id]
        
        if query: # クエリがある場合はフィルタリング
            query_lower = query.lower()
            user_history = [
                h for h in user_history 
                if query_lower in h.get("time", "").lower() or query_lower in h.get("days", "").lower()
            ]

        if not user_history:
            return await interaction.response.send_message("過去の設定履歴は見つかりませんでした。", ephemeral=True)

        embed = discord.Embed(title="📜 アラーム設定履歴", color=discord.Color.light_grey(), timestamp=datetime.now(JST))
        # 最新の10件を新しい順で表示
        for h in reversed(user_history[-10:]):
            set_at_dt = datetime.fromisoformat(h['set_at'])
            icon = "🍅" if h.get("category") == "pomodoro" else "⏰"
            embed.add_field(
                name=f"{icon} {h['time']} ({h['days']})",
                value=f"記録日時: {set_at_dt.strftime('%m/%d %H:%M')}",
                inline=False
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clear_history", description="自分のアラーム設定履歴をすべて削除します")
    async def clear_history(self, interaction: discord.Interaction):
        """ユーザー自身の履歴をデータベースから削除"""
        try:
            # ユーザーの履歴のみをフィルタリングして削除
            self.bot.history = [h for h in self.bot.history if h["user_id"] != interaction.user.id]
            self.bot.save_history()
            await interaction.response.send_message("✅ あなたの履歴をすべて削除しました。", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to clear history: {e}")
            await interaction.response.send_message("⚠️ 履歴の削除に失敗しました。", ephemeral=True)
    @app_commands.command(name="stop", description="再生中のアラームを強制停止して退室させます")
    async def stop_alarm(self, interaction: discord.Interaction):
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect()
            await interaction.response.send_message("⏹️ アラームを停止して退室しました。")
        else:
            await interaction.response.send_message("❌ 現在ボイスチャンネルには接続していません。", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AlarmCog(bot))
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

# ボットの参照を保持するグローバル変数（タスク用）
_bot = None

async def task_execute_alarm(*args, **kwargs):
    """APSchedulerから呼び出されるグローバルなタスクハンドラー"""
    cog = _bot.get_cog('AlarmCog')
    if cog:
        await cog.execute_alarm(*args, **kwargs)

async def task_pre_notify(*args, **kwargs):
    """APSchedulerから呼び出される5分前通知ハンドラー"""
    cog = _bot.get_cog('AlarmCog')
    if cog:
        await cog.pre_notify(*args, **kwargs)


class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_alarm_playbacks = {} # {job_id: {'vc': VoiceClient, 'stop_event': asyncio.Event, 'audio_source': str, 'volume': float}}

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
        logger.info(f"⏰ アラームタスク開始: {job_id} ({time_str})")
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.error(f"Guild ID {guild_id} not found.")
            return

        voice_channel = self.bot.get_channel(voice_channel_id) or await self.bot.fetch_channel(voice_channel_id)
        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)

        if not voice_channel:
            logger.error(f"Voice Channel ID {voice_channel_id} not found.")
            return

        # 接続前に、ボイスチャンネルに人間がいるか確認（誰もいなければ鳴らさない）
        if not any(not member.bot for member in voice_channel.members):
            logger.info(f"Skipping alarm {job_id}: No human users in {voice_channel.name}.")
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

            # 停止イベントを作成し、アクティブな再生リストに追加
            stop_event = asyncio.Event()
            self.active_alarm_playbacks[job_id] = {
                'vc': None, # 後で設定
                'stop_event': stop_event,
                'audio_source': None, # 後で設定
                'volume': volume
            }

            # 接続
            vc = await voice_channel.connect()
            logger.info(f"Connected to {voice_channel.name} for Job: {job_id}")
            self.active_alarm_playbacks[job_id]['vc'] = vc

            # soundsフォルダからランダムにファイルを選択
            files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(('.mp3', '.wav', '.ogg'))]
            if not files:
                if text_channel:
                    await text_channel.send(f"⚠️ `{AUDIO_DIR}` フォルダに音声ファイルが見つかりません。")
                await self.stop_playback(job_id) # 音源がないので停止
                return
            
            selected_sound = random.choice(files)
            audio_source = os.path.join(AUDIO_DIR, selected_sound)
            self.active_alarm_playbacks[job_id]['audio_source'] = audio_source

            # 再生終了後の処理 (ループまたは切断)
            def play_next_segment(error):
                if error:
                    logger.error(f"Playback error for {job_id}: {error}")
                
                # 停止イベントがセットされていない、かつボイスクライアントが接続中の場合、ループ再生
                if job_id in self.active_alarm_playbacks and not self.active_alarm_playbacks[job_id]['stop_event'].is_set():
                    # 音声を再キュー
                    ffmpeg_audio_loop = discord.FFmpegPCMAudio(audio_source)
                    volume_audio_loop = discord.PCMVolumeTransformer(ffmpeg_audio_loop)
                    volume_audio_loop.volume = volume
                    vc.play(volume_audio_loop, after=play_next_segment)
                else:
                    # 停止が要求された、またはアラームがアクティブでなくなった場合、切断
                    if vc.is_connected():
                        coro = vc.disconnect()
                        asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                    if job_id in self.active_alarm_playbacks:
                        del self.active_alarm_playbacks[job_id]

            # 音声再生
            ffmpeg_audio = discord.FFmpegPCMAudio(audio_source)
            volume_audio = discord.PCMVolumeTransformer(ffmpeg_audio)
            volume_audio.volume = volume
            vc.play(volume_audio, after=play_next_segment)
            # ポモドーロなどの他機能での呼び出し時は、そちらでメッセージを出すため、アラーム/スヌーズ時のみ表示
            # once_ (一度きり) の場合もボタンを表示するように修正
            if text_channel and (job_id.startswith("alarm_") or job_id.startswith("snooze_") or job_id.startswith("once_")):
                embed = discord.Embed(
                    title=f"🔔 {time_str} です！",
                    description=f"予定の時間になりました。通話の区切りなどに活用してください。\n\n停止するかスヌーズを選択してください。",
                    color=discord.Color.gold()
                )
                view = AlarmView(self.bot, guild_id, voice_channel_id, text_channel_id, volume, time_str, job_id)
                await text_channel.send(embed=embed, view=view)

        except Exception as e:
            logger.exception(f"Failed to execute alarm: {e}")
            if guild.voice_client:
                await guild.voice_client.disconnect()
            if job_id in self.active_alarm_playbacks:
                del self.active_alarm_playbacks[job_id]

    async def stop_playback(self, job_id: str):
        """指定されたジョブIDのアラーム再生を停止する"""
        if job_id in self.active_alarm_playbacks:
            playback_info = self.active_alarm_playbacks[job_id]
            playback_info['stop_event'].set() # 停止イベントをセット
            vc = playback_info['vc']
            if vc and vc.is_playing():
                vc.stop() # 現在の再生を停止し、after_playingをトリガー
            elif vc and vc.is_connected():
                # 再生中でないが接続中の場合、直接切断
                await vc.disconnect()
            del self.active_alarm_playbacks[job_id]
            logger.info(f"Stopped playback for job: {job_id}")

    @app_commands.command(name="alarm", description="アラームをセットします")
    @app_commands.describe(
        time_str="時刻を入力してください (例: 07:30)",
        repeat="繰り返しにするかどうかを選択してください",
        day_of_week="鳴らしたい曜日を入力してください (例: 月水金)",
        volume="音量を指定してください (0.1〜1.0)"
    )
    @app_commands.autocomplete(day_of_week=day_of_week_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, time_str: str, repeat: bool = True, day_of_week: str = "毎日", volume: float = 0.5):
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ ボイスチャンネルに入った状態で実行してください。", ephemeral=True)

        if not (0.1 <= volume <= 1.0):
            return await interaction.response.send_message("⚠️ 音量は 0.1 から 1.0 の間で指定してください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            time_obj = datetime.strptime(time_str, "%H:%M")
            cron_days = parse_days_to_cron(day_of_week)
            
            # 同時刻・同ユーザーの既存ジョブを徹底的に掃除
            for existing_job in self.bot.scheduler.get_jobs():
                if str(interaction.user.id) in existing_job.id and time_obj.strftime('%H%M') in existing_job.id and not existing_job.id.startswith("snooze_"):
                    self.bot.scheduler.remove_job(existing_job.id)
                    pre_id = f"pre_{existing_job.id}"
                    if self.bot.scheduler.get_job(pre_id):
                        self.bot.scheduler.remove_job(pre_id)

            target_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)

            if repeat:
                # 繰り返し設定 (cron)
                job_id = f"alarm_{interaction.user.id}_{cron_days}_{target_time.strftime('%H%M%S')}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'cron', day_of_week=cron_days, hour=target_time.hour, minute=target_time.minute,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.voice.channel.id, job_id, volume, time_str],
                    id=job_id
                )
                # 5分前通知の登録
                pre_time = target_time - timedelta(minutes=5)
                self.bot.scheduler.add_job(
                    task_pre_notify, 'cron', day_of_week=cron_days, hour=pre_time.hour, minute=pre_time.minute,
                    args=[interaction.channel.id, job_id, time_str],
                    id=f"pre_{job_id}"
                )
                description = f"指定した曜日（{day_of_week}）に繰り返します"
            else:
                # 一度きり設定 (date) - 指定された曜日のうち最も近い日を探す
                if target_time <= now:
                    target_time += timedelta(days=1)
                
                day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                target_weekdays = [day_map[d] for d in cron_days.split(",")] if cron_days != "*" else list(range(7))
                while target_time.weekday() not in target_weekdays:
                    target_time += timedelta(days=1)
                
                job_id = f"once_{interaction.user.id}_{target_time.strftime('%m%d%H%M%S')}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'date', run_date=target_time,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.voice.channel.id, job_id, volume, time_str],
                    id=job_id
                )
                # 5分前通知
                pre_time = target_time - timedelta(minutes=5)
                if pre_time > now:
                    self.bot.scheduler.add_job(
                        task_pre_notify, 'date', run_date=pre_time,
                        args=[interaction.channel.id, job_id, time_str],
                        id=f"pre_{job_id}"
                    )
                description = f"{target_time.strftime('%m/%d')} に一度のみ実行します"

            embed = discord.Embed(title="✅ アラーム予約完了", description=description, color=discord.Color.green())
            embed.add_field(name="⏰ 設定時刻", value=f"`{target_time.strftime('%H:%M')}`", inline=True)
            embed.add_field(name="🔁 繰り返し", value="はい" if repeat else "いいえ", inline=True)
            embed.add_field(name="🔊 音量", value=f"`{volume}`", inline=True)
            embed.add_field(name="🆔 ジョブID", value=f"`{job_id}`", inline=False)

            # 履歴をJSONとして保存
            self.bot.history.append({
                "user_id": interaction.user.id,
                "time": time_str,
                "days": day_of_week if repeat else "一度きり",
                "set_at": now.isoformat(),
                "category": "alarm"
            })
            self.bot.save_history()
            # ストレージへの同期を確実に完了させる
            await self.bot.upload_data_to_channel()

            await interaction.response.send_message(embed=embed)
        except ValueError:
            await interaction.response.send_message("⚠️ 時刻は `HH:mm` 形式で指定してください。", ephemeral=True)

    @app_commands.command(name="alarms", description="予約中の自分のアラームを表示します")
    async def list_alarms(self, interaction: discord.Interaction):
        jobs = self.bot.scheduler.get_jobs()
        user_id_str = str(interaction.user.id)
        user_jobs = [j for j in jobs if user_id_str in j.id and not j.id.startswith("snooze_")] # スヌーズは除外

        if not user_jobs:
            return await interaction.response.send_message("現在予約されているアラームはありません。", ephemeral=True) # 修正なし、元々ephemeral

        embed = discord.Embed(title="⏰ 現在のアラーム一覧", color=discord.Color.blue(), timestamp=datetime.now(JST))
        for i, job in enumerate(user_jobs, 1):
            mode = "一度きり" if job.id.startswith("once_") else "繰り返し"
            time_str = job.next_run_time.astimezone(JST).strftime('%H:%M')
            date_str = job.next_run_time.astimezone(JST).strftime('%m/%d')
            embed.add_field(
                name=f"{i}. {time_str} ({mode} | {date_str})",
                value=f"ID: `{job.id}`",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @app_commands.command(name="cancel", description="指定したIDのアラームをキャンセルします")
    @app_commands.autocomplete(job_id=alarm_id_autocomplete)
    async def cancel_alarm(self, interaction: discord.Interaction, job_id: str):
        if str(interaction.user.id) not in job_id:
            return await interaction.response.send_message("❌ 自分のアラームIDのみキャンセルできます。", ephemeral=True) # 修正なし、元々ephemeral

        if self.bot.scheduler.get_job(job_id):
            self.bot.scheduler.remove_job(job_id)
            pre_job_id = f"pre_{job_id}"
            if self.bot.scheduler.get_job(pre_job_id):
                self.bot.scheduler.remove_job(pre_job_id)

            await interaction.response.send_message(f"🗑️ アラーム `{job_id}` をキャンセルしました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ 指定された ID `{job_id}` が見つかりませんでした。", ephemeral=True) # 修正なし、元々ephemeral

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
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    global _bot
    _bot = bot
    await bot.add_cog(AlarmCog(bot))
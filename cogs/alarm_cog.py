import os
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone # timezoneも必要

import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.jobstores.base import JobLookupError

from utils import JST, AUDIO_DIR, parse_days_to_cron, alarm_id_autocomplete, day_of_week_autocomplete, time_autocomplete
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

    async def pre_notify(self, text_channel_id: int, job_id: str, time_str: str, memo: str = None):
        """アラームの5分前に通知するタスク"""
        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)
        if text_channel:
            title = f"⏳ もうすぐ「{memo}」の時間です" if memo else "⏳ もうすぐ時間です"
            embed = discord.Embed(
                title=title,
                description=f"**{time_str}** にアラームが鳴ります。（あと5分）\nID: `{job_id}`",
                color=discord.Color.blue()
            )
            await text_channel.send(embed=embed, silent=True)

    async def execute_alarm(self, guild_id: int, text_channel_id: int, user_id: int, job_id: str, volume: float, time_str: str, memo: str = None, repeat_info: str = "一度きり"):
        """指定された時刻にボイスチャンネルへ参加し、音声を再生して切断するタスク"""
        logger.info(f"⏰ アラームタスク開始: {job_id} ({time_str})")
        guild = self.bot.get_guild(guild_id)
        if not guild: return

        text_channel = self.bot.get_channel(text_channel_id) or await self.bot.fetch_channel(text_channel_id)
        
        # ユーザーを検索して、現在入っているボイスチャンネルを特定する (ユーザー追従機能)
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if not member or not member.voice or not member.voice.channel:
            logger.info(f"Skipping alarm {job_id}: User {user_id} is not in any voice channel.")
            return
        voice_channel = member.voice.channel

        # 権限の確認
        permissions = voice_channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            if text_channel:
                await text_channel.send(f"⚠️ ボイスチャンネル `{voice_channel.name}` に接続、または発言する権限がありません。", silent=True)
            return

        try:
            # ボイス接続の最適化
            if guild.voice_client:
                if guild.voice_client.channel.id == voice_channel.id:
                    vc = guild.voice_client
                else:
                    await guild.voice_client.move_to(voice_channel)
                    vc = guild.voice_client
            else:
                vc = await voice_channel.connect()
                logger.info(f"Connected to {voice_channel.name} for Job: {job_id}")

            # 停止イベントを作成し、アクティブな再生リストに追加
            stop_event = asyncio.Event()
            self.active_alarm_playbacks[job_id] = {
                'vc': None, # 後で設定
                'stop_event': stop_event,
                'audio_source': None, # 後で設定
                'volume': volume
            }

            self.active_alarm_playbacks[job_id]['vc'] = vc

            # soundsフォルダからランダムにファイルを選択
            files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(('.mp3', '.wav', '.ogg'))]
            if not files:
                if text_channel:
                    await text_channel.send(f"⚠️ `{AUDIO_DIR}` フォルダに音声ファイルが見つかりません。", silent=True)
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

            # 履歴をJSONとして保存 (アラーム本体の実行完了時のみ記録)
            if job_id.startswith(('alarm_', 'once_')):
                self.bot.history.append({
                    "user_id": user_id,
                    "time": f"{time_str} ({memo})" if memo else time_str,
                    "days": repeat_info,
                    "set_at": datetime.now(JST).isoformat(),
                    "category": "alarm"
                })
                self.bot.save_history()

            # ポモドーロなどの他機能での呼び出し時は、そちらでメッセージを出すため、アラーム/スヌーズ時のみ表示
            # once_ (一度きり) の場合もボタンを表示するように修正
            if text_channel and (job_id.startswith("alarm_") or job_id.startswith("snooze_") or job_id.startswith("once_")):
                title = f"🔔 {time_str}「{memo}」" if memo else f"🔔 {time_str} です！"
                embed = discord.Embed(
                    title=title,
                    description=f"予定の時間になりました。通話の区切りなどに活用してください。\n\n停止するかスヌーズを選択してください。",
                    color=discord.Color.gold()
                )
                view = AlarmView(self.bot, guild_id, user_id, text_channel_id, volume, time_str, job_id, memo)
                await text_channel.send(embed=embed, view=view, silent=True)

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
        memo="アラームの内容（例: 会議、ゲームなど）",
        repeat="繰り返しにするかどうかを選択してください",
        day_of_week="鳴らしたい曜日を入力してください (例: 月水金)",
        volume="音量を指定してください (0.1〜1.0)"
    )
    @app_commands.autocomplete(day_of_week=day_of_week_autocomplete, time_str=time_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, time_str: str, memo: str = None, repeat: bool = True, day_of_week: str = "毎日", volume: float = 0.5):
        await self.bot.grant_storage_access(interaction.user)

        if not interaction.user.voice:
            return await interaction.response.send_message("❌ ボイスチャンネルに入った状態で実行してください。", ephemeral=True)

        if not (0.1 <= volume <= 1.0):
            return await interaction.response.send_message("⚠️ 音量は 0.1 から 1.0 の間で指定してください。", ephemeral=True)

        try:
            now = datetime.now(JST)
            time_obj = datetime.strptime(time_str, "%H:%M")
            time_id = time_obj.strftime('%H%M')
            cron_days = parse_days_to_cron(day_of_week)
            
            # 既存の同時刻のアラームを掃除 (安定したIDで狙い撃ち)
            for prefix in ['alarm', 'once']:
                try: self.bot.scheduler.remove_job(f"{prefix}_{interaction.user.id}_{time_id}")
                except: pass
                try: self.bot.scheduler.remove_job(f"pre_{prefix}_{interaction.user.id}_{time_id}")
                except: pass

            target_time = now.replace(hour=time_obj.hour, minute=time_obj.minute, second=0, microsecond=0)

            if repeat:
                # 繰り返し設定 (cron)
                job_id = f"alarm_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'cron', day_of_week=cron_days, hour=target_time.hour, minute=target_time.minute,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, volume, time_str, memo, day_of_week],
                    id=job_id
                )
                # 5分前通知の登録
                pre_time = target_time - timedelta(minutes=5)
                self.bot.scheduler.add_job(
                    task_pre_notify, 'cron', day_of_week=cron_days, hour=pre_time.hour, minute=pre_time.minute,
                    args=[interaction.channel.id, job_id, time_str, memo],
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
                
                job_id = f"once_{interaction.user.id}_{time_id}"
                self.bot.scheduler.add_job(
                    task_execute_alarm, 'date', run_date=target_time,
                    args=[interaction.guild.id, interaction.channel.id, interaction.user.id, job_id, volume, time_str, memo, "一度きり"],
                    id=job_id
                )
                # 5分前通知
                pre_time = target_time - timedelta(minutes=5)
                if pre_time > now:
                    self.bot.scheduler.add_job(
                        task_pre_notify, 'date', run_date=pre_time,
                        args=[interaction.channel.id, job_id, time_str, memo],
                        id=f"pre_{job_id}"
                    )
                description = f"🗓️ **{target_time.strftime('%m/%d')}** に一度のみ実行します。"

            if repeat:
                description = f"🗓️ **{day_of_week}** の **{target_time.strftime('%H:%M')}** に定期実行します。"

            embed = discord.Embed(title="✅ アラームをセットしました", description=description, color=discord.Color.green())
            
            # カテゴリ分けして見やすく整理
            content_text = f"📝 **内容**: `{memo or 'なし'}`\n🔊 **音量**: `{int(volume * 100)}%`"
            embed.add_field(name="📋 アラーム詳細", value=content_text, inline=False)

            # 動的タイムスタンプでカウントダウンを表示
            timestamp_code = f"<t:{int(target_time.timestamp())}:R>"
            embed.add_field(name="⏳ 次の実行", value=timestamp_code, inline=True)
            embed.set_footer(text=f"ID: {job_id}")

            # 設定完了メッセージを本人にのみ表示（プライバシー保護）
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except ValueError:
            await interaction.response.send_message("⚠️ 時刻は `HH:mm` 形式で指定してください。", ephemeral=True)

    @app_commands.command(name="alarms", description="予約中の自分のアラームを表示します")
    async def list_alarms(self, interaction: discord.Interaction):
        await self.bot.grant_storage_access(interaction.user)

        jobs = self.bot.scheduler.get_jobs()
        user_id_str = str(interaction.user.id)
        # ユーザー本人の予約を表示（通知用の pre_ 以外を表示）
        user_jobs = [j for j in jobs if user_id_str in j.id and not j.id.startswith('pre_')]

        if not user_jobs:
            return await interaction.response.send_message(" 現在、稼働中のアラームやポモドーロはありません。新しくセットするには `/alarm` や `/pomodoro` を使ってみてください。", ephemeral=True)

        # 実行予定が近い順に並び替え
        user_jobs.sort(key=lambda x: x.next_run_time)

        embed = discord.Embed(title=f"⏰ {interaction.user.display_name}さんの予約スケジュール", color=discord.Color.blue())
        embed.set_footer(text=f"合計 {len(user_jobs)} 件 | 現在時刻: {datetime.now(JST).strftime('%H:%M')}")

        for i, job in enumerate(user_jobs, 1):
            # 種別の判定
            if job.id.startswith("once_"): icon, mode = "🔔", "一度"
            elif job.id.startswith("snooze_"): icon, mode = "💤", "スヌーズ"
            elif job.id.startswith("pomo_"): icon, mode = "🍅", "ポモ"
            else: icon, mode = "🔁", "毎週"

            next_run = job.next_run_time.astimezone(JST)
            # Discordの動的タイムスタンプ (UNIX秒)
            timestamp_code = f"<t:{int(next_run.timestamp())}:R>"

            # ジョブ引数から音量とメモを取得
            vol_val = job.args[4] if len(job.args) > 4 else 0.5
            vol_display = f"{int(vol_val * 100)}%"
            
            # ポモドーロとアラームでメモの引数位置が異なるのを吸収
            memo = (job.args[9] if len(job.args) > 9 else "作業中") if job.id.startswith("pomo_") else (job.args[6] if len(job.args) > 6 else "なし")

            embed.add_field(
                name=f"#{i} {icon} {next_run.strftime('%H:%M')} ({mode})",
                value=f"⏳ **実行**: {timestamp_code}\n└ 📝 `{memo}` | 🔊 `{vol_display}` | 🆔 `{job.id}`",
                inline=False
            )

        embed.set_footer(text="キャンセルは /cancel、過去の履歴は /history で確認できます")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @app_commands.command(name="cancel", description="セットしたアラームを一覧から選択して解除します")
    @app_commands.autocomplete(alarm_selection=alarm_id_autocomplete)
    async def cancel_alarm(self, interaction: discord.Interaction, alarm_selection: str):
        await self.bot.grant_storage_access(interaction.user)

        if str(interaction.user.id) not in alarm_selection:
            return await interaction.response.send_message("❌ 自分のアラームのみキャンセルできます。", ephemeral=True)

        try:
            self.bot.scheduler.remove_job(alarm_selection)
            try:
                self.bot.scheduler.remove_job(f"pre_{alarm_selection}")
            except JobLookupError:
                pass
            
            await interaction.response.send_message(f"🗑️ 選択したアラームをキャンセルしました。", ephemeral=True)
        except JobLookupError:
            await interaction.response.send_message(f"⚠️ 指定されたアラームが見つかりませんでした。", ephemeral=True)

    @app_commands.command(name="history", description="過去に鳴ったアラームや完了したポモドーロの履歴（最新10件）を表示します")
    @app_commands.describe(query="検索したい時間や曜日を入力してください (任意)")
    async def alarm_history(self, interaction: discord.Interaction, query: str = None):
        await self.bot.grant_storage_access(interaction.user)

        # 数値でも文字列でも比較できるように ID を文字列化して判定
        user_history = [h for h in self.bot.history if str(h.get("user_id")) == str(interaction.user.id)]
        
        if query: # クエリがある場合はフィルタリング
            query_lower = query.lower()
            user_history = [
                h for h in user_history 
                if query_lower in h.get("time", "").lower() or query_lower in h.get("days", "").lower()
            ]

        if not user_history:
            return await interaction.response.send_message("過去の設定履歴は見つかりませんでした。", ephemeral=True)

        embed = discord.Embed(title=f"📜 {interaction.user.display_name}さんの履歴", color=discord.Color.light_grey(), timestamp=datetime.now(JST))
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

    @app_commands.command(name="now", description="ボットが認識している現在の時刻を確認します")
    async def show_now(self, interaction: discord.Interaction):
        """ボットの現在時刻（JST）を表示する"""
        now = datetime.now(JST)
        await interaction.response.send_message(f"🕙 現在のボットの時刻（JST）は `{now.strftime('%H:%M:%S')}` です。", ephemeral=True)


async def setup(bot: commands.Bot):
    global _bot
    _bot = bot
    await bot.add_cog(AlarmCog(bot))
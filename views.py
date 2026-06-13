import discord
import logging
from datetime import datetime, timedelta
from utils import JST

logger = logging.getLogger(__name__)

class AlarmView(discord.ui.View):
    """アラーム鳴動時に表示されるインタラクティブなボタン"""
    def __init__(self, bot, guild_id: int, user_id: int, text_channel_id: int, volume: float, time_str: str, job_id: str, memo: str = None):
        super().__init__(timeout=300) # 5分間でタイムアウト
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.text_channel_id = text_channel_id
        self.volume = volume
        self.time_str = time_str
        self.job_id = job_id
        self.memo = memo

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ この操作は本人のみ可能です。", ephemeral=True, silent=True)
            return False
        return True

    async def disable_buttons(self, interaction: discord.Interaction, exclude_delete=False):
        """ボタンを無効化してメッセージを更新"""
        for item in self.children:
            if not (exclude_delete and getattr(item, 'label', '') == "🗑️"):
                item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="停止 (Stop)", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction, exclude_delete=True)
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)
        await interaction.response.send_message("✅ アラームを停止しました。", ephemeral=True, silent=True)

    @discord.ui.button(label="スヌーズ (5分)", style=discord.ButtonStyle.primary)
    async def snooze_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id) # スヌーズ前に現在の再生を停止

        # JSTを指定して現在の時刻を取得
        run_time = datetime.now(JST) + timedelta(minutes=5)
        new_time_str = run_time.strftime('%H:%M')
        new_snooze_id = f"snooze_{self.user_id}_{run_time.strftime('%H%M')}"
        # alarm_cog モジュールから直接タスク関数をインポートするか、文字列パスで指定
        from cogs.alarm_cog import task_execute_alarm
        
        # ボットのスケジューラーにタスクを追加
        self.bot.scheduler.add_job(
            task_execute_alarm, 'date', run_date=run_time,
            args=[self.guild_id, self.text_channel_id, self.user_id, new_snooze_id, self.volume, new_time_str, self.memo],
            id=new_snooze_id
        )
        
        # 通知メッセージ
        await interaction.response.send_message(f"💤 スヌーズ: {new_time_str} に再度通知します。", ephemeral=True, silent=True)
        self.stop()

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.secondary)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)
        
        try:
            await interaction.message.delete()
        except:
            pass
        self.stop()

    async def on_timeout(self):
        """Viewがタイムアウトした場合、ボットを切断する"""
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)
        # タイムアウト時はinteraction.response.send_messageは使えない

class PomodoroView(discord.ui.View):
    """ポモドーロセッション終了時に次のセッションを確認するボタン"""
    def __init__(self, bot, guild_id: int, user_id: int, text_channel_id: int, volume: float, work_mins: int, rest_mins: int, was_work: bool, cycle_count: int, memo: str = None, job_id: str = None):
        super().__init__(timeout=600) # 10分間待機
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.text_channel_id = text_channel_id
        self.volume = volume
        self.work_mins = work_mins
        self.rest_mins = rest_mins
        self.was_work = was_work
        self.cycle_count = cycle_count
        self.memo = memo
        self.job_id = job_id

        # 次のフェーズに合わせてボタンのラベルを分かりやすく変更
        next_label = "☕ 休憩を始める" if was_work else "✍️ 次の作業へ"
        self.next_button.label = next_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ この操作は本人のみ可能です。", ephemeral=True, silent=True)
            return False
        return True

    async def disable_buttons(self, interaction: discord.Interaction, exclude_delete=False):
        """ボタンを無効化"""
        for item in self.children:
            if not (exclude_delete and getattr(item, 'label', '') == "🗑️"):
                item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="次を開始 (Next)", style=discord.ButtonStyle.success)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        pomo_cog = self.bot.get_cog('PomodoroCog')
        if not pomo_cog:
            return await interaction.response.send_message("⚠️ エラー: Pomodoro機能が見つかりません。", ephemeral=True, silent=True)

        # 次へ進む前に現在の音を止める
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)

        now = datetime.now(JST)
        is_next_work = not self.was_work
        # 次のセッションの時間 (次のセッションが作業ならwork_mins, 休憩ならrest_mins)
        next_session_duration = self.work_mins if is_next_work else self.rest_mins
        end_time = now + timedelta(minutes=next_session_duration)
        
        mode = "work" if is_next_work else "rest"
        job_id = f"pomo_{mode}_{interaction.user.id}_{end_time.strftime('%H%M%S')}"
        
        from cogs.pomodoro_cog import task_execute_pomodoro
        self.bot.scheduler.add_job(
            task_execute_pomodoro, 'date', run_date=end_time,
            args=[self.guild_id, self.text_channel_id, self.user_id, job_id, self.volume, self.work_mins, self.rest_mins, is_next_work, self.cycle_count, self.memo],
            id=job_id
        )

        title = "✍️ 作業開始" if is_next_work else "☕ 休憩開始"
        await interaction.response.send_message(f"✅ {title}しました。終了予定: `{end_time.strftime('%H:%M:%S')}`", ephemeral=True, silent=True)
        self.stop()

    @discord.ui.button(label="終了 (Stop)", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction, exclude_delete=True)
        # 終了時に音を止める
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)
            
        await interaction.response.send_message("✅ ポモドーロタイマーを終了しました。", ephemeral=True, silent=True)

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.secondary)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 再生停止が必要な場合は止める
        alarm_cog = self.bot.get_cog('AlarmCog')
        if alarm_cog:
            await alarm_cog.stop_playback(self.job_id)

        try:
            await interaction.message.delete()
        except:
            pass
        self.stop()
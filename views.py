import discord
from datetime import datetime, timedelta
from utils import JST

class AlarmView(discord.ui.View):
    """アラーム鳴動時に表示されるインタラクティブなボタン"""
    def __init__(self, bot, guild_id: int, voice_channel_id: int, text_channel_id: int, volume: float, time_str: str):
        super().__init__(timeout=300) # 5分間でタイムアウト
        self.bot = bot
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.volume = volume
        self.time_str = time_str

    async def disable_buttons(self, interaction: discord.Interaction):
        """ボタンを無効化してメッセージを更新"""
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="停止 (Stop)", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()
        await interaction.response.send_message("✅ アラームを停止しました。")
        self.stop()

    @discord.ui.button(label="スヌーズ (5分)", style=discord.ButtonStyle.primary)
    async def snooze_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()

        # JSTを指定して現在の時刻を取得
        run_time = datetime.now(JST) + timedelta(minutes=5)
        new_time_str = run_time.strftime('%H:%M')
        job_id = f"snooze_{interaction.user.id}_{run_time.strftime('%H%M%S')}"
        cog = self.bot.get_cog('AlarmCog')
        
        # ボットのスケジューラーにタスクを追加
        self.bot.scheduler.add_job(
            cog.execute_alarm, 'date', run_date=run_time,
            args=[self.guild_id, self.text_channel_id, self.voice_channel_id, job_id, self.volume, new_time_str],
            id=job_id
        )
        await interaction.response.send_message(f"💤 スヌーズ設定完了: {run_time.strftime('%H:%M')} に再度通知します。")
        self.stop()

class PomodoroView(discord.ui.View):
    """ポモドーロセッション終了時に次のセッションを確認するボタン"""
    def __init__(self, bot, guild_id: int, text_channel_id: int, voice_channel_id: int, volume: float, work_mins: int, rest_mins: int, was_work: bool, cycle_count: int):
        super().__init__(timeout=600) # 10分間待機
        self.bot = bot
        self.guild_id = guild_id
        self.text_channel_id = text_channel_id
        self.voice_channel_id = voice_channel_id
        self.volume = volume
        self.work_mins = work_mins
        self.rest_mins = rest_mins
        self.was_work = was_work
        self.cycle_count = cycle_count

    async def disable_buttons(self, interaction: discord.Interaction):
        """ボタンを無効化"""
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="次を開始 (Next)", style=discord.ButtonStyle.success)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        pomo_cog = self.bot.get_cog('PomodoroCog')
        if not pomo_cog:
            return await interaction.response.send_message("⚠️ エラー: Pomodoro機能が見つかりません。", ephemeral=True)

        now = datetime.now(JST)
        is_next_work = not self.was_work
        # 次のセッションの時間 (次のセッションが作業ならwork_mins, 休憩ならrest_mins)
        next_session_duration = self.work_mins if is_next_work else self.rest_mins
        end_time = now + timedelta(minutes=next_session_duration)
        
        mode = "work" if is_next_work else "rest"
        job_id = f"pomo_{mode}_{interaction.user.id}_{end_time.strftime('%H%M%S')}"
        
        self.bot.scheduler.add_job(
            pomo_cog.execute_pomodoro, 'date', run_date=end_time,
            args=[self.guild_id, self.text_channel_id, self.voice_channel_id, job_id, self.volume, self.work_mins, self.rest_mins, is_next_work, self.cycle_count], # cycle_countを引き継ぐ
            id=job_id
        )
        
        title = "✍️ 作業開始" if is_next_work else "☕ 休憩開始"
        await interaction.response.send_message(f"✅ {title}しました。終了予定: `{end_time.strftime('%H:%M:%S')}`")
        self.stop()

    @discord.ui.button(label="終了 (Stop)", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.disable_buttons(interaction)
        await interaction.response.send_message("✅ ポモドーロタイマーを終了しました。")
        self.stop()
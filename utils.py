import os
import discord
from discord import app_commands
from datetime import timezone, timedelta

# 日本標準時 (JST) の設定
JST = timezone(timedelta(hours=9))

# 音声ファイルを格納するディレクトリ
AUDIO_DIR = "sounds"

def parse_days_to_cron(day_str: str) -> str:
    """日本語の曜日文字列をAPSchedulerの形式に変換"""
    mapping = {"月": "mon", "火": "tue", "水": "wed", "木": "thu", "金": "fri", "土": "sat", "日": "sun"}
    res = [en for jp, en in mapping.items() if jp in day_str]
    # 指定がない場合は毎日(*)として扱う
    return ",".join(res) if res else "*"

async def alarm_id_autocomplete(interaction: discord.Interaction, current: str):
    """ジョブIDの入力補完"""
    try:
        jobs = interaction.client.scheduler.get_jobs()
        user_id_str = f"_{interaction.user.id}_"
        choices = []
        for job in jobs:
            # ユーザーのジョブであり、かつ内部管理用(pre_, snooze_)ではないものだけを表示
            is_internal = job.id.startswith(('pre_', 'snooze_'))
            if user_id_str in job.id and not is_internal and current.lower() in job.id.lower():
                if not job.next_run_time: continue # 実行予定がないものはスキップ
                time_str = job.next_run_time.astimezone(JST).strftime('%H:%M')
                icon = '🍅' if 'pomo' in job.id else '🔁'
                choices.append(app_commands.Choice(name=f"{icon} {time_str} (ID: {job.id})", value=job.id))
        return choices[:25]
    except:
        return []

async def day_of_week_autocomplete(interaction: discord.Interaction, current: str):
    """曜日の入力補完"""
    days = ["月", "火", "水", "木", "金", "土", "日"]
    return [app_commands.Choice(name=d, value=d) for d in days if current in d]
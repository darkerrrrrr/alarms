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
    if "平日" in day_str:
        return "mon,tue,wed,thu,fri"
    if "週末" in day_str or "休日" in day_str:
        return "sat,sun"
    if "毎日" in day_str:
        return "*"
    mapping = {"月": "mon", "火": "tue", "水": "wed", "木": "thu", "金": "fri", "土": "sat", "日": "sun"}
    res = [en for jp, en in mapping.items() if jp in day_str]
    # 指定がない場合は毎日(*)として扱う
    return ",".join(res) if res else "*"

async def time_autocomplete(interaction: discord.Interaction, current: str):
    """時刻の入力補完 (30分刻みの候補)"""
    times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
    return [app_commands.Choice(name=t, value=t) for t in times if current in t][:25]

async def alarm_id_autocomplete(interaction: discord.Interaction, current: str):
    """ジョブIDの入力補完"""
    try:
        jobs = interaction.client.scheduler.get_jobs()
        user_id_str = f"_{interaction.user.id}_"
        choices = []
        for job in jobs:
            # ユーザー自身の予約であり、かつシステム内部用(pre_)ではないものを表示
            if user_id_str in job.id and not job.id.startswith('pre_'):
                if not job.next_run_time: continue
                
                next_run = job.next_run_time.astimezone(JST)
                time_str = next_run.strftime('%H:%M')
                date_str = next_run.strftime('%m/%d')
                
                # アイコンを出し分け (ポモドーロ:🍅, 一度きり:🔔, 繰り返し:🔁, スヌーズ:💤)
                if 'pomo' in job.id: icon = '🍅'
                elif job.id.startswith('once_'): icon = '🔔'
                elif job.id.startswith('snooze_'): icon = '💤'
                else: icon = '🔁'

                # ラベルに日付とアイコン、IDの一部を含めて分かりやすくする
                label = f"{icon} {time_str} ({date_str}) - {job.id[:8]}..."
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=job.id))

        return choices[:25]
    except:
        return []

async def day_of_week_autocomplete(interaction: discord.Interaction, current: str):
    """曜日の入力補完"""
    options = ["毎日", "平日 (月-金)", "週末 (土日)", "月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
    return [app_commands.Choice(name=o, value=o) for o in options if current in o][:25]
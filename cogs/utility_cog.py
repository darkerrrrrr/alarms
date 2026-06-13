import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
from utils import JST

class UtilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="history", description="過去の履歴（最新10件）を表示します")
    @app_commands.describe(query="検索ワード (任意)")
    async def alarm_history(self, interaction: discord.Interaction, query: str = None):
        if self.bot.storage: await self.bot.storage.grant_storage_access(interaction.user)
        
        history_source = self.bot.storage.history if self.bot.storage else []
        user_history = [h for h in history_source if str(h.get("user_id")) == str(interaction.user.id)]
        
        if query:
            q = query.lower()
            user_history = [h for h in user_history if q in h.get("time", "").lower() or q in h.get("days", "").lower()]

        if not user_history:
            return await interaction.response.send_message("過去の履歴は見つかりませんでした。", ephemeral=True)

        embed = discord.Embed(title=f"📜 {interaction.user.display_name}さんの履歴", color=discord.Color.light_grey())
        for h in reversed(user_history[-10:]):
            set_at = datetime.fromisoformat(h['set_at'])
            icon = "🍅" if h.get("category") == "pomodoro" else "⏰"
            embed.add_field(
                name=f"{icon} {h['time']} ({h['days']})",
                value=f"記録日時: {set_at.strftime('%m/%d %H:%M')}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="now", description="ボットの現在時刻を確認します")
    async def show_now(self, interaction: discord.Interaction):
        now = datetime.now(JST)
        await interaction.response.send_message(f"🕙 現在のボットの時刻（JST）は `{now.strftime('%H:%M:%S')}` です。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(UtilityCog(bot))
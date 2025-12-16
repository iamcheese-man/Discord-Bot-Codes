import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button
import asyncio
import subprocess
import paramiko
import requests
import psutil
import platform
import socket
import os
from datetime import datetime

# -----------------------------
# CONFIGURATION
# -----------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN")
OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "123456789012345678"))

# Audit log configuration
AUDIT_LOG_ENABLED = True
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "./logs/audit.log")  # customizable path

# Command safety
BLOCKED_SHELL_COMMANDS = ["rm", "sudo", ":(){", "mkfs", "dd"]
MAX_OUTPUT_LENGTH = 1900
COMMAND_TIMEOUT = 10  # seconds

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
def owner_only(interaction: discord.Interaction):
    return interaction.user.id == OWNER_ID

def log_command(user_id: int, guild_id: int, command: str, target: str):
    if not AUDIT_LOG_ENABLED:
        return
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] User: {user_id} | Guild: {guild_id} | Command: {command} | Target: {target}\n"
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(entry)

def truncate_output(output: str):
    if len(output) > MAX_OUTPUT_LENGTH:
        return output[:MAX_OUTPUT_LENGTH] + "\n[TRUNCATED]"
    return output

# -----------------------------
# BOT SETUP
# -----------------------------
class DiagnosticBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = DiagnosticBot()

# -----------------------------
# /ping - Full server info
# -----------------------------
@bot.tree.command(name="ping", description="Show full server diagnostics")
async def ping(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)

    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    hostname = socket.gethostname()
    os_info = platform.platform()
    uptime_seconds = (datetime.utcnow() - datetime.fromtimestamp(psutil.boot_time())).total_seconds()
    uptime = f"{int(uptime_seconds//3600)}h {(int(uptime_seconds)%3600)//60}m"

    msg = f"""
🖥 **SERVER INFO**
Host: `{hostname}`
OS: `{os_info}`

⚙ **SYSTEM**
CPU Usage: `{cpu}%`
RAM Usage: `{ram.percent}%` / {round(ram.total/1024**3,1)} GB
Disk Usage: `{disk.percent}%` / {round(disk.total/1024**3,1)} GB
Uptime: `{uptime}`

🤖 **BOT**
Latency: `{round(bot.latency*1000)}ms`
Python: `{platform.python_version()}`
"""
    await interaction.response.send_message(msg, ephemeral=True)
    log_command(interaction.user.id, interaction.guild.id if interaction.guild else 0, "ping", "server_info")

# -----------------------------
# COMMAND CONFIRMATION VIEW
# -----------------------------
class ConfirmView(View):
    def __init__(self, user_id: int, on_confirm):
        super().__init__(timeout=15)
        self.user_id = user_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Execute ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        await self.on_confirm(interaction)
        self.stop()

    @discord.ui.button(label="Cancel ❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Command cancelled.", ephemeral=True)
        self.stop()

# -----------------------------
# /shell modal
# -----------------------------
class ShellModal(Modal, title="Shell Command"):
    command = TextInput(label="Shell Command", style=discord.TextStyle.long)

    async def on_submit(self, interaction: discord.Interaction):
        cmd = self.command.value.strip()
        if any(block in cmd for block in BLOCKED_SHELL_COMMANDS):
            return await interaction.response.send_message("Blocked command detected.", ephemeral=True)

        async def execute(interaction: discord.Interaction):
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT)
                output = truncate_output((stdout+stderr).decode() or "[no output]")
                await interaction.followup.send(f"```\n{output}\n```", ephemeral=True)
            except asyncio.TimeoutError:
                await interaction.followup.send("Command timed out.", ephemeral=True)

        await interaction.response.send_message(
            "Are you sure you want to execute this shell command?",
            ephemeral=True,
            view=ConfirmView(interaction.user.id, execute)
        )
        log_command(interaction.user.id, interaction.guild.id if interaction.guild else 0, "shell", "local")

@bot.tree.command(name="shell", description="Run a local shell command")
async def shell(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(ShellModal())

# -----------------------------
# /ssh modal
# -----------------------------
class SSHModal(Modal, title="SSH Command"):
    host = TextInput(label="Host")
    username = TextInput(label="Username")
    password = TextInput(label="Password", style=discord.TextStyle.short)
    command = TextInput(label="Command", style=discord.TextStyle.long)

    async def on_submit(self, interaction: discord.Interaction):
        async def execute(interaction: discord.Interaction):
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(self.host.value, username=self.username.value, password=self.password.value, timeout=5)
                stdin, stdout, stderr = ssh.exec_command(self.command.value, timeout=COMMAND_TIMEOUT)
                output = truncate_output(stdout.read().decode() + stderr.read().decode() or "[no output]")
                ssh.close()
                await interaction.followup.send(f"```\n{output}\n```", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"SSH Error:\n```\n{e}\n```", ephemeral=True)

        await interaction.response.send_message(
            f"Execute SSH command on `{self.host.value}`?",
            ephemeral=True,
            view=ConfirmView(interaction.user.id, execute)
        )
        log_command(interaction.user.id, interaction.guild.id if interaction.guild else 0, "ssh", self.host.value)

@bot.tree.command(name="ssh", description="Run SSH command")
async def ssh(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(SSHModal())

# -----------------------------
# /http_get modal
# -----------------------------
class HTTPGetModal(Modal, title="HTTP GET"):
    url = TextInput(label="URL")

    async def on_submit(self, interaction: discord.Interaction):
        async def execute(interaction: discord.Interaction):
            try:
                r = requests.get(self.url.value, timeout=10)
                body = truncate_output(r.text)
                await interaction.followup.send(f"Status: {r.status_code}\n```\n{body}\n```", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"HTTP GET Error:\n```\n{e}\n```", ephemeral=True)

        await interaction.response.send_message(
            f"Execute HTTP GET on `{self.url.value}`?",
            ephemeral=True,
            view=ConfirmView(interaction.user.id, execute)
        )
        log_command(interaction.user.id, interaction.guild.id if interaction.guild else 0, "http_get", self.url.value)

@bot.tree.command(name="http_get", description="HTTP GET request")
async def http_get(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(HTTPGetModal())

# -----------------------------
# /http_post modal
# -----------------------------
class HTTPPostModal(Modal, title="HTTP POST"):
    url = TextInput(label="URL")
    data = TextInput(label="POST Data", style=discord.TextStyle.long)

    async def on_submit(self, interaction: discord.Interaction):
        async def execute(interaction: discord.Interaction):
            try:
                r = requests.post(self.url.value, data=self.data.value, timeout=10)
                body = truncate_output(r.text)
                await interaction.followup.send(f"Status: {r.status_code}\n```\n{body}\n```", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"HTTP POST Error:\n```\n{e}\n```", ephemeral=True)

        await interaction.response.send_message(
            f"Execute HTTP POST on `{self.url.value}`?",
            ephemeral=True,
            view=ConfirmView(interaction.user.id, execute)
        )
        log_command(interaction.user.id, interaction.guild.id if interaction.guild else 0, "http_post", self.url.value)

@bot.tree.command(name="http_post", description="HTTP POST request")
async def http_post(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(HTTPPostModal())

# -----------------------------
# RUN BOT
# -----------------------------
bot.run(TOKEN)

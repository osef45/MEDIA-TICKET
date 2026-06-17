import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import re
import time
import datetime
import io
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────
TOKEN                 = os.getenv("DISCORD_TOKEN")
STAFF_ROLE_ID         = 1485025940257112164
TRANSCRIPT_CHANNEL_ID = 1485083648641208410
TICKET_CATEGORY_ID    = 1485031875931148462
NOTIFY_COOLDOWN_SEC   = 900  # 15 minutes

# ──────────────────────────────────────────────────────────────
#  Bot setup
# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix="!", intents=intents)

media_soon_enabled = False
notify_cooldowns: dict[int, float] = {}


def is_staff(member: discord.Member, guild: discord.Guild) -> bool:
    staff_role = guild.get_role(STAFF_ROLE_ID)
    return staff_role is not None and staff_role in member.roles


def sanitize_channel_name(name: str, max_len: int = 50) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:max_len] or "ticket"


def find_user_ticket(category: discord.CategoryChannel, user_id: int) -> discord.TextChannel | None:
    for ch in category.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.topic and (f"ID: {user_id}" in ch.topic or f"ID : {user_id}" in ch.topic):
            return ch
        if ch.name == f"ticket-{user_id}":
            return ch
    return None


def get_claimed_staff_id(topic: str | None) -> int | None:
    if not topic:
        return None
    for pattern in (r"Claimed by: (\d+)", r"Pris en charge : (\d+)"):
        match = re.search(pattern, topic)
        if match:
            return int(match.group(1))
    return None


async def create_ticket_channel(
    guild: discord.Guild,
    user: discord.Member,
    platforms: str,
    content_type: str,
    description: str,
    links: str,
) -> discord.TextChannel | None:
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if category is None:
        return None

    staff_role = guild.get_role(STAFF_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        ),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        )

    channel = await guild.create_text_channel(
        name=f"ticket-{user.id}",
        category=category,
        topic=f"Ticket by {user} | ID: {user.id}",
        overwrites=overwrites,
    )

    embed = discord.Embed(
        title="🎬  Media Request",
        description=f"New request from {user.mention}",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow(),
    )
    embed.add_field(name="📱  Platforms",     value=platforms,      inline=False)
    embed.add_field(name="🎥  Content type", value=content_type,   inline=False)
    embed.add_field(name="📝  Description",   value=description,    inline=False)
    if links.strip():
        embed.add_field(name="🔗  Links / handles", value=links, inline=False)
    embed.add_field(
        name="📊  Statistics",
        value="Send a **live stats screenshot** in this channel (TikTok, Instagram, YouTube…).",
        inline=False,
    )
    embed.add_field(name="✋  Assigned to", value="Waiting for staff…", inline=False)
    embed.set_footer(text="Media Ticket System")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    mention_text = staff_role.mention if staff_role else "@Staff"
    msg = await channel.send(
        content=f"{mention_text} — new ticket from {user.mention}!",
        embed=embed,
        view=TicketView(),
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
    )
    await msg.pin()
    return channel


# ══════════════════════════════════════════════════════════════
#  VIEW — Ticket inside (Notify + Claim + Close)
# ══════════════════════════════════════════════════════════════
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔔  Notify Staff",
        style=discord.ButtonStyle.secondary,
        custom_id="media_ticket:notify",
    )
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        now        = time.time()
        last       = notify_cooldowns.get(channel_id, 0)
        remaining  = NOTIFY_COOLDOWN_SEC - (now - last)

        if remaining > 0:
            mins = max(1, int(remaining // 60 + (1 if remaining % 60 else 0)))
            await interaction.response.send_message(
                f"⏳ You can notify staff again in **{mins} min**.",
                ephemeral=True,
            )
            return

        notify_cooldowns[channel_id] = now
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        mention    = staff_role.mention if staff_role else "@Staff"
        await interaction.response.send_message(
            f"🔔 {mention} — {interaction.user.mention} needs assistance!",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

    @discord.ui.button(
        label="✋  Claim Ticket",
        style=discord.ButtonStyle.success,
        custom_id="media_ticket:claim",
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild   = interaction.guild
        channel = interaction.channel

        if not is_staff(interaction.user, guild):
            await interaction.response.send_message(
                "❌ Only staff can claim a ticket.", ephemeral=True
            )
            return

        claimed_id = get_claimed_staff_id(channel.topic)
        if claimed_id is not None:
            if claimed_id == interaction.user.id:
                await interaction.response.send_message(
                    "ℹ️ You have already claimed this ticket.", ephemeral=True
                )
            else:
                claimer = guild.get_member(claimed_id)
                name    = claimer.mention if claimer else f"<@{claimed_id}>"
                await interaction.response.send_message(
                    f"❌ This ticket is already claimed by {name}.", ephemeral=True
                )
            return

        await interaction.response.defer()

        base_topic = channel.topic or ""
        new_topic  = f"{base_topic} | Claimed by: {interaction.user.id}"
        await channel.edit(topic=new_topic)

        user_id_match = re.search(r"ID:?\s*(\d+)", base_topic)
        user_slug     = "user"
        if user_id_match:
            owner = guild.get_member(int(user_id_match.group(1)))
            if owner:
                user_slug = sanitize_channel_name(owner.display_name, max_len=20)

        staff_slug = sanitize_channel_name(interaction.user.display_name, max_len=20)
        await channel.edit(name=f"ticket-{user_slug}-{staff_slug}")

        claim_embed = discord.Embed(
            title="✋  Ticket Claimed",
            description=f"{interaction.user.mention} is handling this request.",
            color=0x57F287,
            timestamp=datetime.datetime.utcnow(),
        )
        await channel.send(embed=claim_embed)

        pinned = await channel.pins()
        for pin in pinned:
            if pin.author == guild.me and pin.embeds:
                embed = pin.embeds[0]
                new_embed = embed.copy()
                for i, field in enumerate(new_embed.fields):
                    if field.name in ("✋  Assigned to", "✋  Prise en charge"):
                        new_embed.set_field_at(
                            i,
                            name="✋  Assigned to",
                            value=f"{interaction.user.mention}",
                            inline=False,
                        )
                        break
                await pin.edit(embed=new_embed)
                break

    @discord.ui.button(
        label="🔒  Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="media_ticket:close",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild

        if not is_staff(interaction.user, guild):
            await interaction.response.send_message(
                "❌ Only staff can close a ticket.", ephemeral=True
            )
            return

        await interaction.response.defer()
        channel = interaction.channel

        close_embed = discord.Embed(
            title="🔒  Closing Ticket",
            description=(
                f"Ticket closed by {interaction.user.mention}.\n"
                "Generating transcript…"
            ),
            color=0xED4245,
            timestamp=datetime.datetime.utcnow(),
        )
        await channel.send(embed=close_embed)

        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            "║               TRANSCRIPT — MEDIA TICKET                  ║",
            "╚══════════════════════════════════════════════════════════╝",
            f"  Ticket    : {channel.name}",
            f"  Server    : {guild.name} ({guild.id})",
            f"  Closed by : {interaction.user} ({interaction.user.id})",
            f"  Date      : {datetime.datetime.utcnow().strftime('%m/%d/%Y at %H:%M:%S')} UTC",
            "─" * 62,
        ]

        async for msg in channel.history(limit=None, oldest_first=True):
            ts     = msg.created_at.strftime("%m/%d/%Y %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            lines.append(f"[{ts}] {author}")
            if msg.content:
                lines.append(f"  ➜ {msg.content}")
            for att in msg.attachments:
                lines.append(f"  📎 Attachment: {att.url}")
            if msg.embeds:
                lines.append("  📌 [Embed sent]")
            lines.append("")

        lines.append("─" * 62)
        lines.append("End of transcript.")

        transcript_bytes = "\n".join(lines).encode("utf-8")
        filename = (
            f"transcript-{channel.name}-"
            f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
        )

        trans_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)
        if trans_channel:
            t_embed = discord.Embed(
                title="📄  New Transcript",
                color=0x57F287,
                timestamp=datetime.datetime.utcnow(),
            )
            t_embed.add_field(name="📁 Ticket",     value=f"`{channel.name}`",           inline=True)
            t_embed.add_field(name="🔒 Closed by", value=f"{interaction.user.mention}", inline=True)
            t_embed.add_field(
                name="📅 Date",
                value=datetime.datetime.utcnow().strftime("%m/%d/%Y %H:%M"),
                inline=True,
            )
            t_embed.set_footer(text="Media Ticket System")
            await trans_channel.send(
                embed=t_embed,
                file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
            )

        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket closed by {interaction.user}")


# ══════════════════════════════════════════════════════════════
#  MODAL — Ticket opening form
# ══════════════════════════════════════════════════════════════
class TicketOpenModal(discord.ui.Modal, title="🎬  Media Request"):
    platforms = discord.ui.TextInput(
        label="Active platforms",
        placeholder="e.g. TikTok, Instagram, YouTube, Twitch…",
        max_length=200,
        required=True,
    )
    content_type = discord.ui.TextInput(
        label="Content type",
        placeholder="Videos, Live, Other…",
        max_length=100,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Request description",
        placeholder="Partnership, promotion, visibility…",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )
    links = discord.ui.TextInput(
        label="Links / handles (optional)",
        placeholder="Links to your profiles or @handles",
        style=discord.TextStyle.paragraph,
        max_length=400,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild    = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)

        if category is None:
            await interaction.response.send_message(
                "❌ Category not found. Contact an administrator.", ephemeral=True
            )
            return

        existing = find_user_ticket(category, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"❌ You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel = await create_ticket_channel(
            guild,
            interaction.user,
            self.platforms.value,
            self.content_type.value,
            self.description.value,
            self.links.value or "",
        )

        if channel is None:
            await interaction.followup.send(
                "❌ Could not create the ticket. Contact an administrator.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Your ticket has been opened: {channel.mention}", ephemeral=True
        )


# ══════════════════════════════════════════════════════════════
#  VIEW — Ticket Panel (Open button)
# ══════════════════════════════════════════════════════════════
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📩  Open a Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="media_ticket:open",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if media_soon_enabled:
            embed = discord.Embed(
                title="🎬  Media Soon",
                description=(
                    "Media tickets are not open yet.\n"
                    "Check back soon — we'll let you know when they're available!"
                ),
                color=0xFEE75C,
                timestamp=datetime.datetime.utcnow(),
            )
            embed.set_footer(text="Media Ticket System")
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        guild    = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)

        if category is None:
            await interaction.response.send_message(
                "❌ Category not found. Contact an administrator.", ephemeral=True
            )
            return

        existing = find_user_ticket(category, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"❌ You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.send_modal(TicketOpenModal())


# ──────────────────────────────────────────────────────────────
#  Events
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketView())

    try:
        synced = await bot.tree.sync()
        print(f"✅  {len(synced)} slash command(s) synced")
    except Exception as e:
        print(f"❌  Sync error: {e}")

    print(f"✅  Bot connected: {bot.user} ({bot.user.id})")


# ──────────────────────────────────────────────────────────────
#  Slash command : /setup
# ──────────────────────────────────────────────────────────────
@bot.tree.command(
    name="setup",
    description="Deploy the media ticket panel in this channel",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬  Media Ticket",
        description=(
            "**Welcome to the Media Ticket system!**\n\n"
            "Want to collaborate with us or get visibility?\n"
            "Click the button below to fill out the form\n"
            "and our team will get back to you as soon as possible.\n\n"
            "**You'll need:**\n"
            "┣ 📱  Your active platforms\n"
            "┣ 🎥  Content type (videos / live)\n"
            "┣ 📝  A description of your request\n"
            "┗ 📊  A stats screenshot (to send in the ticket)"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Media Ticket System • Click the button to get started")
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)

    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message(
        "✅ Ticket panel deployed successfully!", ephemeral=True
    )


@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to use this command.",
            ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────
#  Slash command : /mediasoon
# ──────────────────────────────────────────────────────────────
@bot.tree.command(
    name="mediasoon",
    description="Enable or disable Media Soon mode (blocks ticket creation)",
)
@app_commands.describe(state="Enable blocks tickets, Disable reopens them")
@app_commands.choices(state=[
    app_commands.Choice(name="Enable",  value="on"),
    app_commands.Choice(name="Disable", value="off"),
])
@app_commands.checks.has_permissions(administrator=True)
async def mediasoon(interaction: discord.Interaction, state: app_commands.Choice[str]):
    global media_soon_enabled
    media_soon_enabled = state.value == "on"

    if media_soon_enabled:
        await interaction.response.send_message(
            "🔒 **Media Soon enabled** — users can no longer open tickets.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "✅ **Media Soon disabled** — tickets are open again.",
            ephemeral=True,
        )


@mediasoon.error
async def mediasoon_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need **Administrator** permission to use this command.",
            ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────
if TOKEN is None:
    raise ValueError("❌  DISCORD_TOKEN environment variable is missing.")

bot.run(TOKEN)

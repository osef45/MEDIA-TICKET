import discord

import guard
from discord.ext import commands, tasks
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
# Fermeture automatique : heures sans message humain avant fermeture (variable Railway ; 0 = désactivé)
TICKET_INACTIVITY_HOURS = int(os.getenv("TICKET_INACTIVITY_HOURS", "24") or 0)
TICKET_INACTIVITY_CHECK_MINUTES = 10                    # Fréquence de la vérification d'inactivité
# Alerte avant fermeture : heures d'inactivité avant d'avertir le créateur (variable Railway ; 0 = pas d'alerte)
TICKET_INACTIVITY_WARN_HOURS = int(os.getenv("TICKET_INACTIVITY_WARN_HOURS", "12") or 0)
INACTIVITY_WARNING_TITLE = "⚠️ Inactive ticket"          # Titre de l'avis posté dans le ticket (sert aussi de marqueur)
# Salon du panneau, cible des liens « rouvrir un ticket » (variable Railway ; sinon /setup, sinon détection)
TICKET_PANEL_CHANNEL_ID = int(os.getenv("TICKET_PANEL_CHANNEL_ID", "0") or 0)
PANEL_BUTTON_CUSTOM_ID  = "media_ticket:open"

# ──────────────────────────────────────────────────────────────
#  Bot setup
# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix="!", intents=intents, status=discord.Status.online)  # pastille verte « En ligne »

media_soon_enabled = False
notify_cooldowns: dict[int, float] = {}
# Un seul flux de fermeture par salon (double clic, fermeture auto en parallèle)
_ticket_close_locks: dict[int, asyncio.Lock] = {}
# Salon du panneau par serveur : {guild_id: channel_id}
_panel_channel_cache: dict[int, int] = {}


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


# ──────────────────────────────────────────────────────────────
#  Fermeture (bouton Close + fermeture automatique)
# ──────────────────────────────────────────────────────────────
def is_media_ticket(ch: discord.abc.GuildChannel) -> bool:
    if not isinstance(ch, discord.TextChannel) or ch.category_id != TICKET_CATEGORY_ID:
        return False
    topic = ch.topic or ""
    return "ID: " in topic or "ID : " in topic or re.fullmatch(r"ticket-\d+", ch.name) is not None


def ticket_creator_id(ch: discord.TextChannel) -> int | None:
    match = re.search(r"ID ?: ?(\d+)", ch.topic or "")
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"ticket-(\d+)", ch.name)
    return int(match.group(1)) if match else None


async def ticket_activity_state(channel: discord.TextChannel) -> tuple[datetime.datetime, bool]:
    """(date du dernier message humain, avertissement d'inactivité déjà posté depuis ce message).
    Les messages du bot (embed d'ouverture, claim, notices, avertissement) ne comptent pas
    comme activité. Sans message humain : date de création du salon."""
    oldest_seen = None
    warned = False
    async for msg in channel.history(limit=100):
        oldest_seen = msg.created_at
        if not msg.author.bot:
            return msg.created_at, warned
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == INACTIVITY_WARNING_TITLE:
            warned = True
    return oldest_seen or channel.created_at, warned


def _remaining_str(delta: datetime.timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}min" if hours else f"{minutes} min"


async def find_panel_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Salon du panneau : TICKET_PANEL_CHANNEL_ID, puis le dernier /setup, puis détection du bouton."""
    if TICKET_PANEL_CHANNEL_ID:
        ch = guild.get_channel(TICKET_PANEL_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch
    cached = _panel_channel_cache.get(guild.id)
    if cached:
        ch = guild.get_channel(cached)
        if isinstance(ch, discord.TextChannel):
            return ch
        _panel_channel_cache.pop(guild.id, None)
    for ch in guild.text_channels:
        if ch.category_id == TICKET_CATEGORY_ID:
            continue
        perms = ch.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        try:
            async for msg in ch.history(limit=25):
                if msg.author.id != bot.user.id or not msg.components:
                    continue
                for row in msg.components:
                    for comp in getattr(row, "children", []):
                        if getattr(comp, "custom_id", None) == PANEL_BUTTON_CUSTOM_ID:
                            _panel_channel_cache[guild.id] = ch.id
                            return ch
        except (discord.Forbidden, discord.HTTPException):
            continue
    return None


def _reopen_line(panel_channel: discord.TextChannel | None) -> str:
    if panel_channel is None:
        return "If you still need help, feel free to open a new ticket on the server."
    return (
        "If you still need help, feel free to open a new ticket here: "
        f"**[#{panel_channel.name}]({panel_channel.jump_url})** ({panel_channel.mention})"
    )


async def send_inactivity_warning(ch: discord.TextChannel, last_activity: datetime.datetime) -> bool:
    """Avis dans le ticket (avec ping) puis MP au créateur avec le temps restant.
    L'avis sert de marqueur : posté en premier, donc jamais de second MP après un redémarrage."""
    now = datetime.datetime.now(datetime.timezone.utc)
    close_at = last_activity + datetime.timedelta(hours=TICKET_INACTIVITY_HOURS)
    remaining = _remaining_str(close_at - now)
    inactive_for = _remaining_str(now - last_activity)
    close_ts = int(close_at.timestamp())
    uid = ticket_creator_id(ch)
    guild = ch.guild

    notice = discord.Embed(
        title=INACTIVITY_WARNING_TITLE,
        description=(
            f"No activity for **{inactive_for}**.\n"
            f"This ticket will be **closed automatically** <t:{close_ts}:R> "
            f"(**{remaining}** left) unless someone replies here."
        ),
        color=0xFEE75C,
    )
    notice.set_footer(text=f"Media Ticket System  •  Automatic closure after {TICKET_INACTIVITY_HOURS}h of inactivity")
    try:
        await ch.send(
            content=f"<@{uid}>" if uid else None,
            embed=notice,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except (discord.NotFound, discord.Forbidden):
        return False

    if not uid:
        return True
    dm_status = "MP envoyé"
    try:
        creator = bot.get_user(uid) or await bot.fetch_user(uid)
        dm = discord.Embed(
            title="⏳ Your ticket will be closed soon",
            description=(
                f"Your ticket **#{ch.name}** on **{guild.name}** has had no activity for **{inactive_for}**.\n\n"
                f"It will be **automatically closed** <t:{close_ts}:R> (in about **{remaining}**) unless you reply.\n\n"
                f"👉 To keep it open, simply send a message in **[#{ch.name}]({ch.jump_url})**."
            ),
            color=0xFEE75C,
        )
        dm.set_footer(text=f"{guild.name}  •  Inactivity reminder")
        await creator.send(embed=dm)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        dm_status = f"MP impossible ({type(exc).__name__}) : l'avis dans le ticket reste"
    print(f"[⏳] Ticket #{ch.name} : avertissement posté · {dm_status}")
    return True


async def build_transcript(channel: discord.TextChannel, closed_by_text: str) -> bytes:
    guild = channel.guild
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║               TRANSCRIPT — MEDIA TICKET                  ║",
        "╚══════════════════════════════════════════════════════════╝",
        f"  Ticket    : {channel.name}",
        f"  Server    : {guild.name} ({guild.id})",
        f"  Closed by : {closed_by_text}",
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
    return "\n".join(lines).encode("utf-8")


async def close_media_ticket(
    channel: discord.TextChannel,
    *,
    closed_by: discord.abc.User | None,
    inactive_hours: int | None = None,
) -> bool:
    """Annonce, transcription dans le salon des transcriptions, suppression.
    closed_by=None => fermeture automatique : le créateur reçoit aussi un MP avec la transcription
    et le lien du panneau. Retourne False si une fermeture est déjà en cours."""
    guild = channel.guild
    lock = _ticket_close_locks.setdefault(channel.id, asyncio.Lock())
    if lock.locked():
        return False

    async with lock:
        try:
            auto = closed_by is None
            closer_mention = closed_by.mention if closed_by else f"⏰ Automatic ({inactive_hours}h inactive)"
            closer_text = f"{closed_by} ({closed_by.id})" if closed_by else f"Automatic closure ({inactive_hours}h without activity)"

            close_embed = discord.Embed(
                title="🔒  Closing Ticket",
                description=(
                    (f"Ticket closed automatically: no activity for **{inactive_hours} hours**.\n" if auto
                     else f"Ticket closed by {closer_mention}.\n")
                    + "Generating transcript…"
                ),
                color=0xED4245,
                timestamp=datetime.datetime.utcnow(),
            )
            try:
                await channel.send(embed=close_embed)
            except (discord.NotFound, discord.Forbidden):
                return True

            try:
                transcript_bytes = await build_transcript(channel, closer_text)
            except discord.NotFound:
                return True
            filename = f"transcript-{channel.name}-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"

            trans_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)
            if trans_channel:
                t_embed = discord.Embed(title="📄  New Transcript", color=0x57F287, timestamp=datetime.datetime.utcnow())
                t_embed.add_field(name="📁 Ticket",     value=f"`{channel.name}`", inline=True)
                t_embed.add_field(name="🔒 Closed by", value=closer_mention,      inline=True)
                t_embed.add_field(name="📅 Date", value=datetime.datetime.utcnow().strftime("%m/%d/%Y %H:%M"), inline=True)
                if auto:
                    t_embed.add_field(name="💤 Reason", value=f"No activity for {inactive_hours}h", inline=True)
                t_embed.set_footer(text="Media Ticket System")
                try:
                    await trans_channel.send(embed=t_embed, file=discord.File(io.BytesIO(transcript_bytes), filename=filename))
                except discord.HTTPException as exc:
                    print(f"[✗] Transcript not posted for #{channel.name}: {exc!r}")

            # Fermeture automatique : MP au créateur avec la transcription et le lien du panneau
            if auto:
                uid = ticket_creator_id(channel)
                if uid:
                    try:
                        creator = bot.get_user(uid) or await bot.fetch_user(uid)
                        panel_channel = await find_panel_channel(guild)
                        dm = discord.Embed(
                            title="⏰ Your ticket was closed for inactivity",
                            description=(
                                f"Your ticket **#{channel.name}** on **{guild.name}** has been "
                                f"**automatically closed** because there was no activity for **{inactive_hours} hours**.\n\n"
                                f"{_reopen_line(panel_channel)}\n"
                                "We'll be happy to assist you.\n\n"
                                "Find the full transcript of your conversation attached below."
                            ),
                            color=0x5865F2,
                        )
                        dm.set_footer(text=f"{guild.name}  •  Automatic closure")
                        try:
                            await creator.send(embed=dm, file=discord.File(io.BytesIO(transcript_bytes), filename=filename))
                        except discord.Forbidden:
                            await creator.send(embed=dm)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        pass  # MPs fermés ou utilisateur introuvable

            await asyncio.sleep(5)
            try:
                reason = f"Ticket closed automatically: {inactive_hours}h without activity" if auto else f"Ticket closed by {closed_by}"
                await channel.delete(reason=reason)
            except discord.NotFound:
                pass
            return True
        finally:
            _ticket_close_locks.pop(channel.id, None)


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

        lock = _ticket_close_locks.get(interaction.channel.id)
        if lock is not None and lock.locked():
            await interaction.response.send_message("This ticket is already being closed.", ephemeral=True)
            return
        await interaction.response.defer()
        await close_media_ticket(interaction.channel, closed_by=interaction.user)


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

    # Fermeture automatique des tickets inactifs (couvre aussi ceux déjà ouverts avant ce démarrage)
    if TICKET_INACTIVITY_HOURS > 0 and not inactivity_check.is_running():
        inactivity_check.start()
        warn = (
            f"alerte MP à {TICKET_INACTIVITY_WARN_HOURS} h"
            if 0 < TICKET_INACTIVITY_WARN_HOURS < TICKET_INACTIVITY_HOURS
            else "sans alerte préalable"
        )
        print(f"[⏰] Fermeture auto des tickets après {TICKET_INACTIVITY_HOURS} h d'inactivité, {warn} "
              f"(vérification toutes les {TICKET_INACTIVITY_CHECK_MINUTES} min)")


# ──────────────────────────────────────────────────────────────
#  Fermeture automatique des tickets inactifs
# ──────────────────────────────────────────────────────────────
@tasks.loop(minutes=TICKET_INACTIVITY_CHECK_MINUTES)
async def inactivity_check():
    if TICKET_INACTIVITY_HOURS <= 0:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=TICKET_INACTIVITY_HOURS)
    warn_cutoff = (
        now - datetime.timedelta(hours=TICKET_INACTIVITY_WARN_HOURS)
        if 0 < TICKET_INACTIVITY_WARN_HOURS < TICKET_INACTIVITY_HOURS
        else None
    )
    for guild in list(bot.guilds):
        for ch in list(guild.text_channels):
            if not is_media_ticket(ch):
                continue
            lock = _ticket_close_locks.get(ch.id)
            if lock is not None and lock.locked():
                continue
            try:
                last_activity, warned = await ticket_activity_state(ch)
                if last_activity <= cutoff:
                    if await close_media_ticket(ch, closed_by=None, inactive_hours=TICKET_INACTIVITY_HOURS):
                        print(f"[⏰] Ticket #{ch.name} fermé : inactif depuis {TICKET_INACTIVITY_HOURS} h")
                        await asyncio.sleep(3)
                elif warn_cutoff is not None and not warned and last_activity <= warn_cutoff:
                    if await send_inactivity_warning(ch, last_activity):
                        await asyncio.sleep(1)
            except discord.NotFound:
                continue
            except Exception as exc:  # une erreur sur un ticket ne doit pas arrêter la boucle
                print(f"[✗] Fermeture auto impossible pour #{ch.name} : {exc!r}")


@inactivity_check.before_loop
async def _inactivity_check_wait():
    await bot.wait_until_ready()
    await asyncio.sleep(30)


@inactivity_check.error
async def _inactivity_check_error(exc: BaseException):
    print(f"[✗] Boucle de fermeture auto en erreur : {exc!r}")


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
    _panel_channel_cache[interaction.guild.id] = interaction.channel.id  # cible des liens « rouvrir un ticket »
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

# Protection commune (guard.py) : verrou serveur, logs impossibles à effacer, alertes MP, surveillance.
guard.install(bot, name="Media Ticket", guild_ids=[1485021520160886784], protect=[TRANSCRIPT_CHANNEL_ID])
bot.run(TOKEN)

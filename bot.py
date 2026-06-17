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

# Mode "Media Soon" — bloque l'ouverture de nouveaux tickets
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
        if ch.topic and f"ID : {user_id}" in ch.topic:
            return ch
        if ch.name == f"ticket-{user_id}":
            return ch
    return None


def get_claimed_staff_id(topic: str | None) -> int | None:
    if not topic or "Pris en charge :" not in topic:
        return None
    match = re.search(r"Pris en charge : (\d+)", topic)
    return int(match.group(1)) if match else None


async def create_ticket_channel(
    guild: discord.Guild,
    user: discord.Member,
    plateformes: str,
    type_contenu: str,
    description: str,
    liens: str,
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
        topic=f"Ticket de {user} | ID : {user.id}",
        overwrites=overwrites,
    )

    embed = discord.Embed(
        title="🎬  Demande Média",
        description=f"Nouvelle demande de {user.mention}",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow(),
    )
    embed.add_field(name="📱  Plateformes",     value=plateformes,   inline=False)
    embed.add_field(name="🎥  Type de contenu", value=type_contenu,  inline=False)
    embed.add_field(name="📝  Description",      value=description, inline=False)
    if liens.strip():
        embed.add_field(name="🔗  Liens / pseudos", value=liens, inline=False)
    embed.add_field(
        name="📊  Statistiques",
        value="Envoie ta **capture d'écran de stats en direct** dans ce salon (TikTok, Instagram, YouTube…).",
        inline=False,
    )
    embed.add_field(name="✋  Prise en charge", value="En attente du staff…", inline=False)
    embed.set_footer(text="Media Ticket System")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    mention_text = staff_role.mention if staff_role else "@Staff"
    msg = await channel.send(
        content=f"{mention_text} — nouveau ticket de {user.mention} !",
        embed=embed,
        view=TicketView(),
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
    )
    await msg.pin()
    return channel


# ══════════════════════════════════════════════════════════════
#  VIEW — Ticket inside (Notify + Close)
# ══════════════════════════════════════════════════════════════
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ── Notify ────────────────────────────────────────────────
    @discord.ui.button(
        label="🔔  Notifier le Staff",
        style=discord.ButtonStyle.secondary,
        custom_id="media_ticket:notify"
    )
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        now        = time.time()
        last       = notify_cooldowns.get(channel_id, 0)
        remaining  = NOTIFY_COOLDOWN_SEC - (now - last)

        if remaining > 0:
            mins = max(1, int(remaining // 60 + (1 if remaining % 60 else 0)))
            await interaction.response.send_message(
                f"⏳ Tu pourras notifier le staff à nouveau dans **{mins} min**.",
                ephemeral=True,
            )
            return

        notify_cooldowns[channel_id] = now
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        mention    = staff_role.mention if staff_role else "@Staff"
        await interaction.response.send_message(
            f"🔔 {mention} — {interaction.user.mention} a besoin d'assistance !",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

    # ── Claim ─────────────────────────────────────────────────
    @discord.ui.button(
        label="✋  Prendre en charge",
        style=discord.ButtonStyle.success,
        custom_id="media_ticket:claim",
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild   = interaction.guild
        channel = interaction.channel

        if not is_staff(interaction.user, guild):
            await interaction.response.send_message(
                "❌ Seul le staff peut prendre en charge un ticket.", ephemeral=True
            )
            return

        claimed_id = get_claimed_staff_id(channel.topic)
        if claimed_id is not None:
            if claimed_id == interaction.user.id:
                await interaction.response.send_message(
                    "ℹ️ Tu as déjà pris en charge ce ticket.", ephemeral=True
                )
            else:
                claimer = guild.get_member(claimed_id)
                name    = claimer.mention if claimer else f"<@{claimed_id}>"
                await interaction.response.send_message(
                    f"❌ Ce ticket est déjà pris en charge par {name}.", ephemeral=True
                )
            return

        await interaction.response.defer()

        base_topic = channel.topic or ""
        new_topic  = f"{base_topic} | Pris en charge : {interaction.user.id}"
        await channel.edit(topic=new_topic)

        user_id_match = re.search(r"ID : (\d+)", base_topic)
        user_slug     = "user"
        if user_id_match:
            owner = guild.get_member(int(user_id_match.group(1)))
            if owner:
                user_slug = sanitize_channel_name(owner.display_name, max_len=20)

        staff_slug = sanitize_channel_name(interaction.user.display_name, max_len=20)
        await channel.edit(name=f"ticket-{user_slug}-{staff_slug}")

        claim_embed = discord.Embed(
            title="✋  Ticket pris en charge",
            description=f"{interaction.user.mention} s'occupe de cette demande.",
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
                    if field.name == "✋  Prise en charge":
                        new_embed.set_field_at(
                            i,
                            name="✋  Prise en charge",
                            value=f"{interaction.user.mention}",
                            inline=False,
                        )
                        break
                await pin.edit(embed=new_embed)
                break

    # ── Close ─────────────────────────────────────────────────
    @discord.ui.button(
        label="🔒  Fermer le Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="media_ticket:close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild

        if not is_staff(interaction.user, guild):
            await interaction.response.send_message(
                "❌ Seul le staff peut fermer un ticket.", ephemeral=True
            )
            return

        await interaction.response.defer()
        channel = interaction.channel

        # ── Closing notice ─────────────────────────────────────
        close_embed = discord.Embed(
            title="🔒  Fermeture du ticket",
            description=(
                f"Ticket fermé par {interaction.user.mention}.\n"
                "Génération de la transcription…"
            ),
            color=0xED4245,
            timestamp=datetime.datetime.utcnow(),
        )
        await channel.send(embed=close_embed)

        # ── Build .txt transcript ──────────────────────────────
        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            "║               TRANSCRIPTION — MEDIA TICKET               ║",
            "╚══════════════════════════════════════════════════════════╝",
            f"  Ticket    : {channel.name}",
            f"  Serveur   : {guild.name} ({guild.id})",
            f"  Fermé par : {interaction.user} ({interaction.user.id})",
            f"  Date      : {datetime.datetime.utcnow().strftime('%d/%m/%Y à %H:%M:%S')} UTC",
            "─" * 62,
        ]

        async for msg in channel.history(limit=None, oldest_first=True):
            ts     = msg.created_at.strftime("%d/%m/%Y %H:%M:%S")
            author = f"{msg.author} ({msg.author.id})"
            lines.append(f"[{ts}] {author}")
            if msg.content:
                lines.append(f"  ➜ {msg.content}")
            for att in msg.attachments:
                lines.append(f"  📎 Pièce jointe : {att.url}")
            if msg.embeds:
                lines.append(f"  📌 [Embed envoyé]")
            lines.append("")

        lines.append("─" * 62)
        lines.append("Fin de la transcription.")

        transcript_bytes = "\n".join(lines).encode("utf-8")
        filename = (
            f"transcript-{channel.name}-"
            f"{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
        )

        # ── Send transcript to dedicated channel ───────────────
        trans_channel = guild.get_channel(TRANSCRIPT_CHANNEL_ID)
        if trans_channel:
            t_embed = discord.Embed(
                title="📄  Nouvelle Transcription",
                color=0x57F287,
                timestamp=datetime.datetime.utcnow(),
            )
            t_embed.add_field(name="📁 Ticket",    value=f"`{channel.name}`",                          inline=True)
            t_embed.add_field(name="🔒 Fermé par", value=f"{interaction.user.mention}",                inline=True)
            t_embed.add_field(name="📅 Date",      value=datetime.datetime.utcnow().strftime("%d/%m/%Y %H:%M"), inline=True)
            t_embed.set_footer(text="Media Ticket System")
            await trans_channel.send(
                embed=t_embed,
                file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
            )

        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket fermé par {interaction.user}")


# ══════════════════════════════════════════════════════════════
#  MODAL — Ticket opening form
# ══════════════════════════════════════════════════════════════
class TicketOpenModal(discord.ui.Modal, title="🎬  Demande Média"):
    plateformes = discord.ui.TextInput(
        label="Plateformes actives",
        placeholder="Ex: TikTok, Instagram, YouTube, Twitch…",
        max_length=200,
        required=True,
    )
    type_contenu = discord.ui.TextInput(
        label="Type de contenu",
        placeholder="Vidéos, Live, Autre…",
        max_length=100,
        required=True,
    )
    description = discord.ui.TextInput(
        label="Description de ta demande",
        placeholder="Partenariat, promotion, visibilité…",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )
    liens = discord.ui.TextInput(
        label="Liens / pseudos (optionnel)",
        placeholder="Liens vers tes profils ou @pseudos",
        style=discord.TextStyle.paragraph,
        max_length=400,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild    = interaction.guild
        category = guild.get_channel(TICKET_CATEGORY_ID)

        if category is None:
            await interaction.response.send_message(
                "❌ Catégorie introuvable. Contacte un administrateur.", ephemeral=True
            )
            return

        existing = find_user_ticket(category, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel = await create_ticket_channel(
            guild,
            interaction.user,
            self.plateformes.value,
            self.type_contenu.value,
            self.description.value,
            self.liens.value or "",
        )

        if channel is None:
            await interaction.followup.send(
                "❌ Impossible de créer le ticket. Contacte un administrateur.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Ton ticket a été ouvert : {channel.mention}", ephemeral=True
        )


# ══════════════════════════════════════════════════════════════
#  VIEW — Ticket Panel (Open button)
# ══════════════════════════════════════════════════════════════
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📩  Ouvrir un Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="media_ticket:open"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if media_soon_enabled:
            embed = discord.Embed(
                title="🎬  Media Soon",
                description=(
                    "Les tickets média ne sont pas encore ouverts.\n"
                    "Reviens bientôt, on te préviendra dès que c'est disponible !"
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
                "❌ Catégorie introuvable. Contacte un administrateur.", ephemeral=True
            )
            return

        existing = find_user_ticket(category, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )
            return

        await interaction.response.send_modal(TicketOpenModal())


# ──────────────────────────────────────────────────────────────
#  Events
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    # Register persistent views so buttons keep working after restarts
    bot.add_view(TicketPanelView())
    bot.add_view(TicketView())

    try:
        synced = await bot.tree.sync()
        print(f"✅  {len(synced)} commande(s) slash synchronisée(s)")
    except Exception as e:
        print(f"❌  Erreur de synchronisation : {e}")

    print(f"✅  Bot connecté : {bot.user} ({bot.user.id})")


# ──────────────────────────────────────────────────────────────
#  Slash command : /setup
# ──────────────────────────────────────────────────────────────
@bot.tree.command(
    name="setup",
    description="Déploie le panel de tickets média dans ce salon"
)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬  Media Ticket",
        description=(
            "**Bienvenue sur le système de tickets Média !**\n\n"
            "Tu veux collaborer avec nous ou obtenir de la visibilité ?\n"
            "Clique sur le bouton ci-dessous pour remplir le formulaire\n"
            "et notre équipe te répondra dans les plus brefs délais.\n\n"
            "**Tu auras besoin de :**\n"
            "┣ 📱  Tes plateformes actives\n"
            "┣ 🎥  Le type de contenu (vidéos / live)\n"
            "┣ 📝  Une description de ta demande\n"
            "┗ 📊  Une capture de tes stats (à envoyer dans le ticket)"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Media Ticket System • Clique sur le bouton pour commencer")
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)

    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message(
        "✅ Panel de tickets déployé avec succès !", ephemeral=True
    )


@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Administrateur** pour utiliser cette commande.",
            ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────
#  Slash command : /mediasoon
# ──────────────────────────────────────────────────────────────
@bot.tree.command(
    name="mediasoon",
    description="Active ou désactive le mode Media Soon (bloque l'ouverture des tickets)"
)
@app_commands.describe(etat="Activer bloque les tickets, Désactiver les rouvre")
@app_commands.choices(etat=[
    app_commands.Choice(name="Activer",  value="on"),
    app_commands.Choice(name="Désactiver", value="off"),
])
@app_commands.checks.has_permissions(administrator=True)
async def mediasoon(interaction: discord.Interaction, etat: app_commands.Choice[str]):
    global media_soon_enabled
    media_soon_enabled = etat.value == "on"

    if media_soon_enabled:
        await interaction.response.send_message(
            "🔒 **Media Soon activé** — les clients ne peuvent plus ouvrir de tickets.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "✅ **Media Soon désactivé** — les tickets sont à nouveau ouverts.",
            ephemeral=True,
        )


@mediasoon.error
async def mediasoon_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Tu dois avoir la permission **Administrateur** pour utiliser cette commande.",
            ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────
if TOKEN is None:
    raise ValueError("❌  La variable d'environnement DISCORD_TOKEN est manquante.")

bot.run(TOKEN)

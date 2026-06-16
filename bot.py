import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
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

# ──────────────────────────────────────────────────────────────
#  Bot setup
# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Mode "Media Soon" — bloque l'ouverture de nouveaux tickets
media_soon_enabled = False


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
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        mention    = staff_role.mention if staff_role else "@Staff"
        await interaction.response.send_message(
            f"🔔 {mention} — {interaction.user.mention} a besoin d'assistance !",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )

    # ── Close ─────────────────────────────────────────────────
    @discord.ui.button(
        label="🔒  Fermer le Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="media_ticket:close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild      = interaction.guild
        staff_role = guild.get_role(STAFF_ROLE_ID)

        if staff_role not in interaction.user.roles:
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

        # Prevent duplicate tickets
        for ch in category.channels:
            if ch.name == f"ticket-{interaction.user.id}":
                await interaction.response.send_message(
                    f"❌ Tu as déjà un ticket ouvert : {ch.mention}", ephemeral=True
                )
                return

        # Channel permissions
        staff_role = guild.get_role(STAFF_ROLE_ID)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user:   discord.PermissionOverwrite(
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
            name=f"ticket-{interaction.user.id}",
            category=category,
            topic=f"Ticket de {interaction.user} | ID : {interaction.user.id}",
            overwrites=overwrites,
        )

        await interaction.response.send_message(
            f"✅ Ton ticket a été ouvert : {channel.mention}", ephemeral=True
        )

        # ── Welcome & instructions embed ──────────────────────
        embed = discord.Embed(
            title="🎬  Demande Média",
            description=(
                f"Bienvenue {interaction.user.mention} ! 👋\n\n"
                "Merci d'avoir ouvert un ticket.\n"
                "Pour traiter ta demande le plus rapidement possible,\n"
                "merci de **répondre aux 4 points ci-dessous** :"
            ),
            color=0x5865F2,
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(
            name="📊  1 · Statistiques",
            value=(
                "Envoie une **capture d'écran de tes statistiques en direct** :\n"
                "> TikTok · Instagram · YouTube · Twitch · autre"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎥  2 · Type de contenu",
            value=(
                "Quel type de contenu crées-tu ?\n"
                "> 📹 **Vidéos** — contenu pré-enregistré\n"
                "> 🔴 **Live** — streams en direct\n"
                "> 🎵 **Autre** — précise le format"
            ),
            inline=False,
        )
        embed.add_field(
            name="📱  3 · Plateformes actives",
            value="Liste toutes les plateformes sur lesquelles tu publies régulièrement.",
            inline=False,
        )
        embed.add_field(
            name="📝  4 · Description de ta demande",
            value="Explique brièvement ce que tu recherches (partenariat, promotion, autre…).",
            inline=False,
        )
        embed.set_footer(text="Notre équipe reviendra vers toi dès que possible • Media Ticket")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        mention_text = staff_role.mention if staff_role else "@Staff"
        msg = await channel.send(
            content=f"{mention_text} — nouveau ticket de {interaction.user.mention} !",
            embed=embed,
            view=TicketView(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )
        await msg.pin()


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
            "Ouvre un ticket en cliquant sur le bouton ci-dessous\n"
            "et notre équipe te répondra dans les plus brefs délais.\n\n"
            "**Avant d'ouvrir ton ticket, prépare :**\n"
            "┣ 📊  Une capture de tes stats (TikTok, Instagram, YouTube…)\n"
            "┣ 🎥  Le type de contenu que tu crées (vidéos / live)\n"
            "┣ 📱  La liste de tes plateformes actives\n"
            "┗ 📝  Une courte description de ta demande"
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

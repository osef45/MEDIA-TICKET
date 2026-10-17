"""
Protection commune des bots ARP / Staffloader / ARP Reseller (la même que le bot clés reseller).

- Verrou serveur : le bot quitte tout serveur hors liste ; commandes, boutons et formulaires
  venant d'un autre serveur sont refusés.
- Logs impossibles à effacer : chaque message du bot dans ses salons de logs est copié
  (MongoDB, fichiers compris). Supprimé, il est republié aussitôt avec une alerte 🚨 qui dit qui
  l'a supprimé (logs d'audit).
- Alertes en MP : log supprimé, salon de logs supprimé ou ses permissions modifiées, bot ajouté
  sur un autre serveur.
- Surveillance : chaque commande, bouton, formulaire et erreur est écrit dans le salon de logs.

Utilisation (juste avant bot.run) :
    import guard
    guard.install(bot, name="ARP Ticket", guild_ids=[...], protect=[salons de logs], watch=salon)

Variables Railway (facultatives, elles remplacent les valeurs du code) :
    GUARD_GUILD_IDS, GUARD_PROTECT_CHANNEL_IDS, GUARD_WATCH_CHANNEL_ID, GUARD_ALERT_USER_IDS,
    GUARD_MONGO_URI (sinon MONGO_URI / MONGODB_URI), GUARD_DB, GUARD_SURVEILLANCE (0 = off).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import discord

DEFAULT_ALERT_USER_IDS = "331079185546215425,1413080023493115994"
COPY_DAYS = 180                 # un log supprimé reste restaurable 180 jours
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MEMORY_COPIES = 3000        # sans MongoDB : copies gardées en mémoire (perdues au redémarrage)

C_GREEN, C_BLUE, C_RED, C_ORANGE = 0x57F287, 0x5865F2, 0xED4245, 0xE67E22
C_GRAY, C_DARKRED, C_TEAL = 0x99AAB5, 0x992D22, 0x1ABC9C

_log = logging.getLogger("guard")


def _print(*args) -> None:
    print(*args, flush=True)  # visible tout de suite dans les logs Railway


def _ids(raw) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, (int, str)):
        raw = str(raw).replace(";", ",").split(",")
    out = []
    for x in raw:
        x = str(x).strip()
        if x.isdigit() and len(x) >= 5 and int(x) not in out:  # 0 = salon désactivé
            out.append(int(x))
    return out


def _env_ids(name: str, default) -> list[int]:
    value = os.getenv(name, "").strip()
    return _ids(value) if value else _ids(default)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def who(user) -> str:
    """Mention + pseudo + ID : lisible même quand le pseudo affiché est « . »."""
    if user is None:
        return "Inconnu"
    return f"<@{user.id}> · **{getattr(user, 'name', user.id)}** · `{user.id}`"


class Card:
    """Log présenté comme ceux des bots logs : en-tête, phrase, lignes « > **Libellé** · valeur »."""

    def __init__(self, title: str, color: int, category: str, bot_name: str) -> None:
        self.title, self.color, self.category, self.bot_name = title, color, category, bot_name
        self.intro: str | None = None
        self.lines: list[str] = []
        self.sections: list[tuple[str, str]] = []
        self.author: tuple[str, str | None] | None = None

    def about(self, user) -> "Card":
        if user is not None:
            avatar = getattr(getattr(user, "display_avatar", None), "url", None)
            self.author = (str(getattr(user, "name", user)), avatar)
        return self

    def about_raw(self, name: str, avatar_url: str | None) -> "Card":
        self.author = (name, avatar_url)
        return self

    def line(self, label: str, value) -> "Card":
        if value not in (None, ""):
            self.lines.append(f"> **{label}** · {value}")
        return self

    def section(self, title: str, text: str) -> "Card":
        self.sections.append((title, text))
        return self

    def embed(self) -> discord.Embed:
        parts = [p for p in (self.intro, "\n".join(self.lines)) if p]
        parts += [f"**{t}**\n{x}" for t, x in self.sections]
        description = "\n\n".join(parts)
        if len(description) > 4000:
            description = description[:3997] + "..."
        embed = discord.Embed(title=self.title, description=description or None, color=self.color, timestamp=utcnow())
        if self.author:
            embed.set_author(name=self.author[0][:256], icon_url=self.author[1])
            if self.author[1]:
                embed.set_thumbnail(url=self.author[1])
        embed.set_footer(text=f"{self.bot_name} · {self.category}")
        return embed


# ─── Interactions brutes (avant que le bot ne les traite) ─────

def _raw_user(data: dict) -> dict:
    return (data.get("member") or {}).get("user") or data.get("user") or {}


def _raw_avatar(user: dict) -> str | None:
    if user.get("avatar") and user.get("id"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=128"
    return None


def _raw_who(user: dict) -> str:
    uid = user.get("id", "?")
    return f"<@{uid}> · **{user.get('username', '?')}** · `{uid}`"


def _option_text(option: dict) -> str:
    value = option.get("value")
    kind = option.get("type")
    if kind == 6:
        value = f"<@{value}>"
    elif kind == 7:
        value = f"<#{value}>"
    elif kind == 8:
        value = f"<@&{value}>"
    elif kind == 9:
        value = f"<@{value}>"
    elif isinstance(value, str) and len(value) > 120:
        value = value[:117] + "..."
    return f"`{option.get('name')}` {value}"


def _command_text(data: dict) -> tuple[str, str | None]:
    """« /reseller add » et ses options lisibles."""
    inner = data.get("data") or {}
    name = [inner.get("name", "?")]
    options = inner.get("options") or []
    while options and options[0].get("type") in (1, 2):  # sous-commande (groupe)
        name.append(options[0].get("name", "?"))
        options = options[0].get("options") or []
    text = " · ".join(_option_text(o) for o in options)
    return "/" + " ".join(name), (text or None)


def _component_label(data: dict) -> str:
    custom_id = (data.get("data") or {}).get("custom_id")
    for row in (data.get("message") or {}).get("components") or []:
        for comp in row.get("components") or []:
            if comp.get("custom_id") == custom_id:
                label = comp.get("label") or comp.get("placeholder")
                emoji = (comp.get("emoji") or {}).get("name")
                return " ".join(x for x in (emoji, label) if x) or custom_id
    return custom_id or "?"


class Guard:
    def __init__(self, bot, *, name: str, guild_ids, protect=(), watch=None, alert_user_ids=None, announce=True) -> None:
        self.bot = bot
        self.name = name
        self.guild_ids = _env_ids("GUARD_GUILD_IDS", guild_ids)
        self.protect = set(_env_ids("GUARD_PROTECT_CHANNEL_IDS", protect))
        watch_ids = _env_ids("GUARD_WATCH_CHANNEL_ID", watch if watch else (list(protect)[:1] or []))
        self.watch_id = watch_ids[0] if watch_ids else None
        if self.watch_id:
            self.protect.add(self.watch_id)  # les alertes elles-mêmes sont protégées
        self.alert_ids = _env_ids("GUARD_ALERT_USER_IDS", alert_user_ids or DEFAULT_ALERT_USER_IDS)
        self.surveillance = os.getenv("GUARD_SURVEILLANCE", "1").strip() not in ("0", "false", "off", "non")
        self.copies = None
        self.memory: "OrderedDict[int, dict]" = OrderedDict()
        self.storage = "mémoire (perdu au redémarrage)"
        uri = (os.getenv("GUARD_MONGO_URI") or os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or "").strip().lstrip("﻿")
        if uri:
            try:
                from pymongo import MongoClient

                self._mongo = MongoClient(uri, serverSelectionTimeoutMS=10000)
                self.copies = self._mongo[os.getenv("GUARD_DB", "bot_guard").strip() or "bot_guard"]["log_copies"]
                self.storage = "MongoDB"
            except Exception as e:  # pymongo absent ou URI invalide : on garde la mémoire
                _print(f"[guard] MongoDB indisponible, copies en mémoire : {e}")
        self._audit_seen: dict[int, int] = {}
        self._pending: list = []
        self._started = False
        self.auto_announce = announce  # False : le bot appelle lui-même bot.guard.announce(extra=...)
        self._announced = False
        self._audit_ok = False
        self._setup_done = asyncio.Event()
        self._recent_errors: dict[str, float] = {}
        self._self_deleted: set[int] = set()  # messages que le bot supprime lui-même

    # ─── Mise en place ────────────────────────────────────────
    def install(self) -> "Guard":
        bot = self.bot
        try:  # il faut voir les messages des salons (les siens) et leurs suppressions
            bot._connection._intents.guild_messages = True
            bot._connection._intents.guilds = True
        except Exception:
            pass

        original_dispatch = bot.dispatch

        def dispatch(event_name, /, *args, **kwargs):
            handler = getattr(self, f"_on_{event_name}", None)
            if handler is not None:
                self._spawn(handler(*args))
            return original_dispatch(event_name, *args, **kwargs)

        bot.guard = self  # le bot peut appeler bot.guard.announce(extra=[...]) dans son on_ready
        bot.dispatch = dispatch
        # discord.py garde sa propre référence à dispatch (prise à la création du bot) : c'est elle
        # qui reçoit les événements de Discord (ready, messages, suppressions…).
        bot._connection.dispatch = dispatch

        http = bot.http
        original_delete, original_bulk = http.delete_message, http.delete_messages

        def delete_message(channel_id, message_id, *args, **kwargs):
            self._self_deleted.add(int(message_id))
            return original_delete(channel_id, message_id, *args, **kwargs)

        def delete_messages(channel_id, message_ids, *args, **kwargs):
            self._self_deleted.update(int(m) for m in message_ids)
            return original_bulk(channel_id, message_ids, *args, **kwargs)

        http.delete_message, http.delete_messages = delete_message, delete_messages

        state = bot._connection
        parsers = getattr(state, "parsers", None)
        original_parse = parsers.get("INTERACTION_CREATE") if parsers else None
        if original_parse is not None:
            def parse_interaction_create(data):
                gid = data.get("guild_id")
                if gid is not None and int(gid) not in self.guild_ids:
                    try:
                        interaction = discord.Interaction(data=data, state=state)
                    except Exception:
                        interaction = None
                    self._spawn(self._refuse_foreign(interaction, data))
                    return
                original_parse(data)
                if self.surveillance and data.get("type") in (2, 3, 5):
                    self._spawn(self._log_interaction(data))

            parsers["INTERACTION_CREATE"] = parse_interaction_create
        else:
            _print("[guard] ⚠️ interactions non filtrées (version de discord.py inattendue)")

        tree = getattr(bot, "tree", None) or getattr(state, "_command_tree", None)
        if tree is not None:
            original_error = tree.on_error

            async def on_error(interaction, error):
                setattr(error, "_guard_logged", True)
                self._spawn(self.log_error(f"la commande {self._cmd_name(interaction)}", error, interaction))
                return await original_error(interaction, error)

            tree.on_error = on_error

        logging.getLogger("discord").addHandler(_ErrorHandler(self))
        return self

    def _spawn(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(self._safe(coro))
        except RuntimeError:
            coro.close()

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as e:  # la protection ne doit jamais faire tomber le bot
            _print(f"[guard] erreur interne : {e!r}")

    # ─── Envoi ────────────────────────────────────────────────
    def card(self, title: str, color: int, category: str) -> Card:
        return Card(title, color, category, self.name)

    async def send(self, log: "Card | discord.Embed", *, alert: bool = False, important: bool = True) -> None:
        """Poste dans le salon de surveillance ; alert=True l'envoie aussi en MP aux personnes d'alerte.
        Salon introuvable ou fermé : un log important part en MP pour ne pas être perdu
        (la simple surveillance non, pour ne pas inonder les MP)."""
        embed = log.embed() if isinstance(log, Card) else log
        # Alerte : les MP d'abord ; ceux qui ne peuvent pas recevoir de MP sont mentionnés dans le salon.
        failed = await self.alert_dm(embed) if alert else []
        sent = False
        if self.watch_id:
            channel = self.bot.get_channel(self.watch_id)
            content, mentions = None, discord.AllowedMentions.none()
            if failed:
                content = "🔔 " + " ".join(f"<@{u}>" for u in failed) + " · alerte pas reçue en MP (MP fermés aux bots) : la voici."
                mentions = discord.AllowedMentions(users=[discord.Object(id=u) for u in failed], roles=False, everyone=False)
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(self.watch_id)
                await channel.send(content=content, embed=embed, allowed_mentions=mentions)
                sent = True
            except discord.DiscordException as e:
                _print(f"[guard] log impossible dans {self.watch_id} : {e}")
        if not sent and important and not alert:
            embed = embed.copy()
            embed.set_footer(text=f"📭 Salon de logs inaccessible · {embed.footer.text or ''}")
            await self.alert_dm(embed)

    async def alert_dm(self, embed: discord.Embed) -> list[int]:
        """MP à chaque personne d'alerte. Retourne ceux qui n'ont pas pu le recevoir."""
        failed = []
        for user_id in self.alert_ids:
            user = self.bot.get_user(user_id)
            try:
                if user is None:
                    user = await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
            except discord.DiscordException as e:
                failed.append(user_id)
                _print(f"[guard] alerte MP impossible pour {user_id} : {e}")
        return failed

    # ─── Démarrage et verrou serveur ──────────────────────────
    async def _on_ready(self, *args) -> None:
        for guild in list(self.bot.guilds):
            await self._enforce(guild)
        if self._started:
            return
        self._started = True
        if self.copies is not None:
            try:
                await asyncio.to_thread(self._mongo.admin.command, "ping")
                await asyncio.to_thread(self.copies.create_index, "created_at", expireAfterSeconds=COPY_DAYS * 86400)
            except Exception as e:
                self.storage = f"⚠️ MongoDB injoignable ({e.__class__.__name__}), copies en mémoire"
                self.copies = None
        audit_ok = True
        for gid in self.guild_ids:
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            me = guild.me
            audit_ok = audit_ok and bool(me and me.guild_permissions.view_audit_log)
            await self._prime_audit(guild)
        self._audit_ok = audit_ok
        self._setup_done.set()
        _print(f"[guard] {self.name} protégé · serveurs {self.guild_ids} · logs {sorted(self.protect)} · {self.storage} · audit {'OK' if audit_ok else 'manquant'}")
        if self.auto_announce:
            await self.announce()
        else:  # le bot annonce lui-même son démarrage (avec ses infos) ; sinon on le fait au bout d'1 min
            self._spawn(self._announce_fallback())

    async def _announce_fallback(self) -> None:
        await asyncio.sleep(60)
        await self.announce()

    async def announce(self, extra=()) -> None:
        """Message de démarrage unique : état des protections + infos du bot (extra = [(libellé, valeur)])."""
        if self._announced:
            return
        self._announced = True
        try:
            await asyncio.wait_for(self._setup_done.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        servers = ", ".join(
            f"**{g.name}**" if (g := self.bot.get_guild(gid)) else f"`{gid}`" for gid in self.guild_ids
        )
        log = self.card("🛡️ Bot démarré · protections actives", C_GREEN, "Système").about(self.bot.user)
        log.intro = f"**{getattr(self.bot.user, 'name', self.name)}** est en ligne et protégé."
        log.line("Verrou serveur", f"✅ uniquement {servers}")
        log.line("Logs protégés", " ".join(f"<#{c}>" for c in sorted(self.protect)) or "—")
        log.line("Copies des logs", self.storage)
        log.line("Qui supprime un log", "✅ identifié (logs d'audit)" if self._audit_ok else "⚠️ il manque la permission « Voir les logs du serveur »")
        log.line("Surveillance", "✅ chaque commande, bouton, formulaire et erreur" if self.surveillance else "désactivée")
        log.line("Alertes en MP", " ".join(f"<@{u}>" for u in self.alert_ids) or "—")
        extra = [(label, value) for label, value in extra if value not in (None, "")]
        if extra:
            log.section("ℹ️ Le bot", "\n".join(f"> **{label}** · {value}" for label, value in extra))
        await self.send(log)

    async def _on_guild_join(self, guild) -> None:
        await self._enforce(guild)

    async def _enforce(self, guild) -> None:
        if guild.id in self.guild_ids:
            return
        _print(f"[guard] serveur non autorisé {guild.name} ({guild.id}) : départ")
        try:
            await guild.leave()
        except discord.HTTPException:
            pass
        log = self.card("⚠️ Serveur non autorisé", C_DARKRED, "Sécurité")
        log.intro = "Le bot était sur un autre serveur : il l'a quitté automatiquement."
        log.line("Serveur", f"**{guild.name}** · `{guild.id}`")
        await self.send(log, alert=True)

    async def _refuse_foreign(self, interaction, data: dict) -> None:
        try:
            if interaction is not None:
                await interaction.response.send_message("❌ This bot is not available here.", ephemeral=True)
        except Exception:
            pass
        user = _raw_user(data)
        log = self.card("⛔ Utilisation hors serveur", C_DARKRED, "Sécurité").about_raw(user.get("username", "?"), _raw_avatar(user))
        log.intro = f"**{user.get('username', '?')}** a tenté d'utiliser le bot sur un autre serveur."
        log.line("Membre", _raw_who(user))
        if data.get("type") in (2, 4):
            command, options = _command_text(data)
            log.line("Commande", command)
            log.line("Options", options)
        log.line("Serveur", f"`{data.get('guild_id')}`")
        await self.send(log, important=False)

    # ─── Surveillance ─────────────────────────────────────────
    async def _log_interaction(self, data: dict) -> None:
        user = _raw_user(data)
        kind = data.get("type")
        if kind == 2:
            command, options = _command_text(data)
            log = self.card(f"🧭 Commande {command}", C_TEAL, "Surveillance")
            log.intro = f"**{user.get('username', '?')}** a utilisé **{command}**."
            log.line("Options", options)
        elif kind == 3:
            label = _component_label(data)
            values = (data.get("data") or {}).get("values")
            log = self.card(f"🔘 {'Menu' if values else 'Bouton'} « {label} »"[:256], C_TEAL, "Surveillance")
            log.intro = f"**{user.get('username', '?')}** a cliqué sur **{label}**."
            log.line("Choix", ", ".join(f"`{v}`" for v in values) if values else None)
        else:
            log = self.card("📝 Formulaire envoyé", C_TEAL, "Surveillance")
            log.intro = f"**{user.get('username', '?')}** a envoyé un formulaire."
            log.line("Formulaire", f"`{(data.get('data') or {}).get('custom_id', '?')}`")
        log.about_raw(user.get("username", "?"), _raw_avatar(user))
        log.line("Membre", _raw_who(user))
        log.line("Salon", f"<#{data['channel_id']}>" if data.get("guild_id") and data.get("channel_id") else "En MP")
        await self.send(log, important=False)

    @staticmethod
    def _cmd_name(interaction) -> str:
        cmd = getattr(interaction, "command", None)
        return "/" + (getattr(cmd, "qualified_name", None) or (getattr(interaction, "data", None) or {}).get("name", "?"))

    async def log_error(self, context: str, error: BaseException, interaction=None) -> None:
        original = getattr(error, "original", error)
        key = f"{context}:{type(original).__name__}:{original}"
        now = asyncio.get_running_loop().time()
        if self._recent_errors.get(key, 0) > now:
            return
        self._recent_errors[key] = now + 60
        log = self.card("❌ Erreur du bot", C_RED, "Erreurs")
        log.intro = f"Une erreur est arrivée pendant **{context}**."
        if interaction is not None:
            user = getattr(interaction, "user", None)
            log.about(user)
            log.line("Membre", who(user))
            channel_id = getattr(interaction, "channel_id", None)
            log.line("Salon", f"<#{channel_id}>" if channel_id else None)
        log.section("Erreur", f"```\n{type(original).__name__}: {str(original)[:900]}\n```")
        await self.send(log, important=False)

    # ─── Protection des logs ──────────────────────────────────
    async def _on_message(self, message) -> None:
        me = self.bot.user
        if me is None or message.author.id != me.id or message.channel.id not in self.protect:
            return
        files, total = [], 0
        for attachment in message.attachments:
            if attachment.size > MAX_FILE_BYTES or total + attachment.size > 12 * 1024 * 1024:
                continue
            try:
                data = await attachment.read()
            except discord.HTTPException:
                continue
            total += len(data)
            files.append({"name": attachment.filename, "data": data})
        doc = {
            "bot": me.id,
            "channel_id": message.channel.id,
            "content": message.content or "",
            "embeds": [e.to_dict() for e in message.embeds],
            "files": files,
            "created_at": utcnow(),
        }
        if self.copies is not None:
            try:
                await asyncio.to_thread(self.copies.replace_one, {"_id": message.id}, doc, upsert=True)
                return
            except Exception as e:
                _print(f"[guard] copie MongoDB impossible, gardée en mémoire : {e}")
        self.memory[message.id] = doc
        while len(self.memory) > MAX_MEMORY_COPIES:
            self.memory.popitem(last=False)

    async def _pop_copy(self, message_id: int) -> dict | None:
        doc = self.memory.pop(message_id, None)
        if doc is None and self.copies is not None:
            try:
                doc = await asyncio.to_thread(self.copies.find_one_and_delete, {"_id": message_id})
            except Exception as e:
                _print(f"[guard] copie {message_id} illisible : {e}")
        return doc

    def _is_log_deletion(self, entry) -> bool:
        channel = getattr(entry.extra, "channel", None)
        return getattr(entry.target, "id", None) == self.bot.user.id and getattr(channel, "id", None) in self.protect

    async def _prime_audit(self, guild) -> None:
        try:
            async for entry in guild.audit_logs(limit=50, action=discord.AuditLogAction.message_delete):
                if self._is_log_deletion(entry):
                    self._audit_seen[entry.id] = getattr(entry.extra, "count", 1) or 1
        except Exception:
            pass

    async def _find_deleter(self, guild, channel_id: int, bulk: bool):
        if guild is None:
            return None
        await asyncio.sleep(1.5)  # laisser Discord écrire l'entrée d'audit
        now = utcnow()
        try:
            if bulk:
                async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_bulk_delete):
                    if getattr(entry.target, "id", None) == channel_id and (now - _aware(entry.created_at)).total_seconds() <= 60:
                        return entry.user
                return None
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_delete):
                if not self._is_log_deletion(entry):
                    continue
                count = getattr(entry.extra, "count", 1) or 1
                before = self._audit_seen.get(entry.id)
                self._audit_seen[entry.id] = count
                age = (now - _aware(entry.created_at)).total_seconds()
                new = count if before is None and age <= 60 else count - (before or count)
                if new > 0:
                    self._pending.extend([entry.user] * (new - 1))
                    return entry.user
            if self._pending:
                return self._pending.pop(0)
        except discord.Forbidden:
            _print("[guard] permission « Voir les logs du serveur » manquante : auteur inconnu")
        except Exception as e:
            _print(f"[guard] logs d'audit illisibles : {e}")
        return None

    async def _on_raw_message_delete(self, payload) -> None:
        if payload.channel_id in self.protect:
            await self._restore([payload.message_id], payload.channel_id, payload.guild_id, bulk=False)

    async def _on_raw_bulk_message_delete(self, payload) -> None:
        if payload.channel_id in self.protect:
            await self._restore(list(payload.message_ids), payload.channel_id, payload.guild_id, bulk=True)

    async def _restore(self, message_ids, channel_id: int, guild_id, bulk: bool) -> None:
        own = [m for m in message_ids if m in self._self_deleted]
        for message_id in own:  # supprimé par le bot lui-même : normal
            self._self_deleted.discard(message_id)
            await self._pop_copy(message_id)
        message_ids = [m for m in message_ids if m not in own]
        if not message_ids:
            return
        guild = self.bot.get_guild(guild_id or 0)
        deleter = await self._find_deleter(guild, channel_id, bulk)
        channel = self.bot.get_channel(channel_id)
        restored, lost, titles = 0, 0, []
        for message_id in sorted(message_ids):
            doc = await self._pop_copy(message_id)
            if doc is None:
                lost += 1
                continue
            embeds = [discord.Embed.from_dict(e) for e in doc.get("embeds") or []][:10]
            posted = f"<t:{int(discord.utils.snowflake_time(message_id).timestamp())}:f>"
            content = doc.get("content") or ""
            if embeds:
                first = embeds[0]
                footer = first.footer.text or ""
                if not footer.startswith("♻️"):
                    first.set_footer(text=f"♻️ Restauré après suppression · {footer}"[:2048], icon_url=first.footer.icon_url)
            elif not content.startswith("♻️"):
                content = f"♻️ *Restauré après suppression (posté {posted})*\n{content}"
            files = [discord.File(io.BytesIO(f["data"]), filename=f["name"]) for f in doc.get("files") or []]
            try:
                await channel.send(content=content[:2000] or None, embeds=embeds, files=files, allowed_mentions=discord.AllowedMentions.none())
                restored += 1
            except (discord.DiscordException, AttributeError) as e:
                _print(f"[guard] restauration impossible : {e}")
                lost += 1
                continue
            title = (embeds[0].title if embeds else None) or (doc.get("content") or "").split("\n")[0][:80]
            titles.append(title or ", ".join(f["name"] for f in doc.get("files") or []) or "Message")

        if restored == 0 and deleter is None:
            return  # pas un message de ce bot (ex. message d'un autre bot dans le même salon)

        alert = self.card("🚨 Log supprimé — restauré" if restored else "🚨 Log supprimé", C_DARKRED, "Sécurité")
        alert.intro = "Les logs ne peuvent pas être effacés : chaque suppression est **republiée** et **signalée**."
        alert.about(deleter)
        alert.line("Supprimé par", who(deleter) if deleter is not None else "Inconnu · donne au bot la permission « Voir les logs du serveur »")
        alert.line("Salon", f"<#{channel_id}>" + (f" · [ouvrir](https://discord.com/channels/{guild_id}/{channel_id})" if guild_id else ""))
        alert.line("Restauré(s)", f"**{restored}**")
        if lost:
            alert.line("Non restaurable(s)", f"**{lost}** · copie introuvable")
        if titles:
            alert.section("📄 Log(s) concerné(s)", "\n".join(f"> {t}" for t in titles[:15]))
        await self.send(alert, alert=True)

    # ─── Salons de logs supprimés ou modifiés ─────────────────
    async def _audit_entry(self, guild, actions: set, target_id: int):
        if guild is None:
            return None
        await asyncio.sleep(1.5)
        try:
            async for entry in guild.audit_logs(limit=10):
                if entry.action in actions and getattr(entry.target, "id", None) == target_id and (utcnow() - _aware(entry.created_at)).total_seconds() <= 60:
                    return entry
        except Exception:
            pass
        return None

    async def _on_guild_channel_delete(self, channel) -> None:
        if channel.id not in self.protect:
            return
        entry = await self._audit_entry(channel.guild, {discord.AuditLogAction.channel_delete}, channel.id)
        actor = getattr(entry, "user", None)
        alert = self.card("🚨 Salon de logs supprimé", C_DARKRED, "Sécurité").about(actor)
        alert.intro = f"Le salon de logs **#{channel.name}** du bot **{self.name}** a été supprimé."
        if channel.id == self.watch_id:
            alert.intro += " Les prochains logs vous seront envoyés **en MP** jusqu'à ce qu'un nouveau salon soit réglé."
        alert.line("Supprimé par", who(actor))
        alert.line("Salon", f"#{channel.name} · `{channel.id}`")
        await self.alert_dm(alert.embed())

    async def _on_guild_channel_update(self, before, after) -> None:
        if after.id not in self.protect or before.overwrites == after.overwrites:
            return
        actions = {discord.AuditLogAction.overwrite_create, discord.AuditLogAction.overwrite_update, discord.AuditLogAction.overwrite_delete}
        entry = await self._audit_entry(after.guild, actions, after.id)
        actor = getattr(entry, "user", None)
        if actor is not None and self.bot.user is not None and actor.id == self.bot.user.id:
            return
        changes = []
        for target, overwrite in after.overwrites.items():
            label = "@everyone" if getattr(target, "id", None) == after.guild.id else getattr(target, "mention", str(target))
            if target not in before.overwrites:
                changes.append(f"➕ {label} a reçu un accès")
            elif before.overwrites[target] != overwrite:
                changes.append(f"✏️ Accès de {label} modifié")
        for target in before.overwrites:
            if target not in after.overwrites:
                label = "@everyone" if getattr(target, "id", None) == after.guild.id else getattr(target, "mention", str(target))
                changes.append(f"➖ Accès de {label} retiré")
        alert = self.card("🔐 Salon de logs modifié", C_DARKRED, "Sécurité").about(actor)
        alert.intro = f"Les permissions du salon de logs {after.mention} ont été modifiées."
        alert.line("Modifié par", who(actor))
        alert.section("Changements", "\n".join(changes[:15]) or "—")
        await self.send(alert, alert=True)


class _ErrorHandler(logging.Handler):
    """Les erreurs que discord.py écrit dans ses logs (boutons, événements, tâches) arrivent dans le salon."""

    def __init__(self, guard: Guard) -> None:
        super().__init__(level=logging.ERROR)
        self.guard = guard

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("guard"):
            return
        error = record.exc_info[1] if record.exc_info else None
        if error is not None and getattr(error, "_guard_logged", False):
            return  # déjà logué par le gestionnaire des commandes
        if error is None:
            error = RuntimeError(record.getMessage())
        self.guard._spawn(self.guard.log_error(record.getMessage()[:200], error))


def install(bot, *, name: str, guild_ids, protect=(), watch=None, alert_user_ids=None, announce=True) -> Guard:
    """Active toutes les protections sur ce bot. À appeler une fois, juste avant bot.run()."""
    """announce=False : le bot poste lui-même son message de démarrage avec bot.guard.announce(extra=[...])."""
    return Guard(bot, name=name, guild_ids=guild_ids, protect=protect, watch=watch, alert_user_ids=alert_user_ids, announce=announce).install()

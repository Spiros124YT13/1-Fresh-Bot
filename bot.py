import io
import logging
import os
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.environ["DISCORD_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "/data/tickets.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ticketbot")

intents = discord.Intents.default()
intents.message_content = True  # gia na fainetai to periexomeno sta transcripts


# ----------------------------- Database -----------------------------
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                support_role_id INTEGER NOT NULL,
                log_channel_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            """
        )
        await db.commit()


async def get_config(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM config WHERE guild_id = ?", (guild_id,))
        return await cur.fetchone()


async def get_ticket_by_channel(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
        )
        return await cur.fetchone()


# ------------------------------ Helpers -----------------------------
def is_staff(member: discord.Member, support_role_id: int) -> bool:
    return (
        member.guild_permissions.manage_channels
        or any(r.id == support_role_id for r in member.roles)
    )


async def build_transcript(channel: discord.TextChannel) -> io.BytesIO:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = m.content or ""
        if m.attachments:
            content += " " + " ".join(a.url for a in m.attachments)
        lines.append(f"[{ts}] {m.author} ({m.author.id}): {content}")
    return io.BytesIO("\n".join(lines).encode("utf-8"))


# ------------------------------- Views ------------------------------
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Anoixe Ticket", emoji="🎫", style=discord.ButtonStyle.green, custom_id="ticket:open"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        cfg = await get_config(guild.id)
        if not cfg:
            return await interaction.followup.send("Το ticket system δεν έχει ρυθμιστεί ακόμα.", ephemeral=True)

        # Ena anoixto ticket ana xristi
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
                (guild.id, interaction.user.id),
            )
            existing = await cur.fetchone()
        if existing and guild.get_channel(existing[0]):
            return await interaction.followup.send(
                f"Έχεις ήδη ανοιχτό ticket: <#{existing[0]}>", ephemeral=True
            )

        category = guild.get_channel(cfg["category_id"])
        support_role = guild.get_role(cfg["support_role_id"])
        if not category or not support_role:
            return await interaction.followup.send(
                "Η κατηγορία ή το support role δεν υπάρχει πια. Ξανατρέξε `/ticket setup`.",
                ephemeral=True,
            )

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO tickets (guild_id, user_id, created_at) VALUES (?, ?, ?)",
                (guild.id, interaction.user.id, datetime.now(timezone.utc).isoformat()),
            )
            ticket_id = cur.lastrowid
            await db.commit()

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True
            ),
            support_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
            ),
        }
        channel = await guild.create_text_channel(
            name=f"ticket-{ticket_id:04d}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_id} | user_id={interaction.user.id}",
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE tickets SET channel_id=? WHERE id=?", (channel.id, ticket_id))
            await db.commit()

        embed = discord.Embed(
            title=f"Ticket #{ticket_id:04d}",
            description=(
                f"Γεια σου {interaction.user.mention}! Περίγραψε το θέμα σου και "
                f"κάποιος από το {support_role.mention} θα σε εξυπηρετήσει σύντομα."
            ),
            color=discord.Color.green(),
        )
        await channel.send(
            content=f"{interaction.user.mention} {support_role.mention}",
            embed=embed,
            view=TicketControls(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        await interaction.followup.send(f"Το ticket σου δημιουργήθηκε: {channel.mention}", ephemeral=True)
        log.info("Ticket %s opened by %s", ticket_id, interaction.user)


class ConfirmClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Ναι, κλείσιμο", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Κλείνει το ticket...", view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Άκυρο", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ακυρώθηκε.", view=None)


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Κλείσιμο", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await get_ticket_by_channel(interaction.channel.id)
        cfg = await get_config(interaction.guild.id)
        if not ticket or not cfg:
            return await interaction.response.send_message("Δεν βρέθηκε ticket.", ephemeral=True)
        if interaction.user.id != ticket["user_id"] and not is_staff(interaction.user, cfg["support_role_id"]):
            return await interaction.response.send_message("Δεν έχεις δικαίωμα.", ephemeral=True)
        await interaction.response.send_message(
            "Σίγουρα θες να κλείσεις το ticket;", view=ConfirmClose(), ephemeral=True
        )


async def close_ticket(channel: discord.TextChannel, closer: discord.Member):
    ticket = await get_ticket_by_channel(channel.id)
    cfg = await get_config(channel.guild.id)
    if not ticket or not cfg:
        return

    transcript = await build_transcript(channel)
    filename = f"transcript-ticket-{ticket['id']:04d}.txt"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), ticket["id"]),
        )
        await db.commit()

    embed = discord.Embed(title=f"Ticket #{ticket['id']:04d} έκλεισε", color=discord.Color.red())
    embed.add_field(name="Άνοιξε ο/η", value=f"<@{ticket['user_id']}>")
    embed.add_field(name="Έκλεισε ο/η", value=closer.mention)

    log_channel = channel.guild.get_channel(cfg["log_channel_id"])
    if log_channel:
        await log_channel.send(embed=embed, file=discord.File(io.BytesIO(transcript.getvalue()), filename))

    # DM sto xristi me to transcript (an exei anoixta ta DMs)
    user = channel.guild.get_member(ticket["user_id"])
    if user:
        try:
            await user.send(
                f"Το ticket σου στο **{channel.guild.name}** έκλεισε.",
                file=discord.File(io.BytesIO(transcript.getvalue()), filename),
            )
        except discord.Forbidden:
            pass

    log.info("Ticket %s closed by %s", ticket["id"], closer)
    await channel.delete(reason=f"Ticket closed by {closer}")


# ------------------------------- Bot --------------------------------
class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        self.add_view(PanelView())
        self.add_view(TicketControls())
        await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


bot = TicketBot()
ticket_group = app_commands.Group(name="ticket", description="Ticket system", guild_only=True)


@ticket_group.command(name="setup", description="Ρύθμιση του ticket system")
@app_commands.describe(
    category="Κατηγορία όπου θα δημιουργούνται τα tickets",
    support_role="Role του support team",
    log_channel="Κανάλι για logs/transcripts",
)
@app_commands.default_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    support_role: discord.Role,
    log_channel: discord.TextChannel,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO config (guild_id, category_id, support_role_id, log_channel_id) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, "
            "support_role_id=excluded.support_role_id, log_channel_id=excluded.log_channel_id",
            (interaction.guild.id, category.id, support_role.id, log_channel.id),
        )
        await db.commit()
    await interaction.response.send_message("✅ Η ρύθμιση αποθηκεύτηκε. Τρέξε `/ticket panel`.", ephemeral=True)


@ticket_group.command(name="panel", description="Στείλε το panel για άνοιγμα tickets σε αυτό το κανάλι")
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    if not await get_config(interaction.guild.id):
        return await interaction.response.send_message("Τρέξε πρώτα `/ticket setup`.", ephemeral=True)
    embed = discord.Embed(
        title="🎫 Support",
        description="Πάτα το κουμπί παρακάτω για να ανοίξεις ένα ticket.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=PanelView())
    await interaction.response.send_message("Το panel στάλθηκε.", ephemeral=True)


@ticket_group.command(name="add", description="Πρόσθεσε χρήστη στο ticket")
async def add_user(interaction: discord.Interaction, user: discord.Member):
    ticket, cfg = await get_ticket_by_channel(interaction.channel.id), await get_config(interaction.guild.id)
    if not ticket:
        return await interaction.response.send_message("Αυτό δεν είναι ticket κανάλι.", ephemeral=True)
    if not is_staff(interaction.user, cfg["support_role_id"]):
        return await interaction.response.send_message("Μόνο το support team.", ephemeral=True)
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(f"{user.mention} προστέθηκε.")


@ticket_group.command(name="remove", description="Αφαίρεσε χρήστη από το ticket")
async def remove_user(interaction: discord.Interaction, user: discord.Member):
    ticket, cfg = await get_ticket_by_channel(interaction.channel.id), await get_config(interaction.guild.id)
    if not ticket:
        return await interaction.response.send_message("Αυτό δεν είναι ticket κανάλι.", ephemeral=True)
    if not is_staff(interaction.user, cfg["support_role_id"]):
        return await interaction.response.send_message("Μόνο το support team.", ephemeral=True)
    if user.id == ticket["user_id"]:
        return await interaction.response.send_message("Δεν μπορείς να αφαιρέσεις τον ιδιοκτήτη.", ephemeral=True)
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(f"{user.mention} αφαιρέθηκε.")


bot.tree.add_command(ticket_group)

if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)

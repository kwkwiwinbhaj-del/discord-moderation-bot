import os
import asyncio
import discord
from discord.ext import commands
import logging
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
 
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
 
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
DATABASE_URL = os.getenv('DATABASE_URL')
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0'))
SUPER_MEMBER_ROLE_NAME = 'Super Member'
 
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
 
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
 
 
class Database:
    def __init__(self, url):
        self.url = url
        self.conn = None
 
    def get_conn(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(self.url)
        return self.conn
 
    def init_tables(self):
        conn = self.get_conn()
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS warnings (
                id SERIAL PRIMARY KEY, guild_id BIGINT, member_id BIGINT,
                reason TEXT, issued_by BIGINT, issued_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP, active BOOLEAN DEFAULT TRUE)''')
            conn.commit()
        logger.info('Database tables ready')
 
    def add_warning(self, guild_id, member_id, reason, issued_by):
        conn = self.get_conn()
        with conn.cursor() as cur:
            expires_at = datetime.utcnow() + timedelta(days=30)
            cur.execute('INSERT INTO warnings (guild_id, member_id, reason, issued_by, expires_at) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                        (guild_id, member_id, reason, issued_by, expires_at))
            wid = cur.fetchone()[0]
            conn.commit()
            return wid
 
    def get_warnings(self, guild_id, member_id):
        conn = self.get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT * FROM warnings WHERE guild_id=%s AND member_id=%s AND active=TRUE ORDER BY issued_at DESC',
                        (guild_id, member_id))
            return cur.fetchall() or []
 
    def remove_warning(self, warning_id):
        conn = self.get_conn()
        with conn.cursor() as cur:
            cur.execute('UPDATE warnings SET active=FALSE WHERE id=%s', (warning_id,))
            conn.commit()
 
    def clear_warnings(self, guild_id, member_id):
        conn = self.get_conn()
        with conn.cursor() as cur:
            cur.execute('UPDATE warnings SET active=FALSE WHERE guild_id=%s AND member_id=%s', (guild_id, member_id))
            conn.commit()
 
 
db = None
 
 
@bot.event
async def on_ready():
    global db
    if db is None:
        db = Database(DATABASE_URL)
        db.init_tables()
        logger.info(f'Bot logged in as {bot.user}')
 
    logger.info(f'py-cord version: {discord.__version__}')
    logger.info(f'Looking for guild with ID: {GUILD_ID}')
    logger.info(f'Bot is currently in guilds: {[(g.id, g.name) for g in bot.guilds]}')
 
    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f'Found guild: {guild.name} ({guild.id})')
        role = discord.utils.get(guild.roles, name=SUPER_MEMBER_ROLE_NAME)
        if not role:
            try:
                role = await guild.create_role(name=SUPER_MEMBER_ROLE_NAME, color=discord.Color.gold())
                logger.info(f'Created {SUPER_MEMBER_ROLE_NAME} role')
            except Exception as e:
                logger.error(f'Failed to create role: {e}')
        else:
            logger.info(f'Found existing {SUPER_MEMBER_ROLE_NAME} role: {role.id}')
 
        # Make sure member cache is fully populated so on_member_update
        # fires reliably for everyone, not just members already cached.
        logger.info(f'Member count before chunk: {len(guild.members)}')
        try:
            await guild.chunk()
            logger.info(f'Chunk complete. Member count after chunk: {len(guild.members)}')
        except Exception as e:
            logger.error(f'Failed to chunk guild members: {e}')
 
        # Reconcile roles on startup in case tag changes happened while the bot was offline.
        # Runs as a background task (not awaited) so on_ready doesn't block for
        # a long time on a large guild - check the logs for '[sync]' lines to
        # track progress.
        asyncio.create_task(sync_all_tag_roles(guild, role))
        logger.info('Kicked off sync_all_tag_roles as a background task.')
    else:
        logger.error(f'guild is None! bot.get_guild({GUILD_ID}) returned nothing. Check GUILD_ID and that the bot is actually in this server.')
 
 
async def sync_all_tag_roles(guild: discord.Guild, role: discord.Role, status_callback=None):
    """Check every cached member's current server-tag status against whether
    they hold the Super Member role, and fix any mismatch. Runs slowly on
    purpose (small delay per change) to stay well under Discord's rate limits
    on large guilds. Returns (added, removed, checked) counts."""
    if not role:
        logger.info('[sync] No role passed in, skipping sync entirely.')
        if status_callback:
            await status_callback('No Super Member role found - nothing to sync.')
        return 0, 0, 0
 
    members = guild.members
    logger.info(f'[sync] Iterating over {len(members)} cached members...')
    added = 0
    removed = 0
    for i, member in enumerate(members, 1):
        if member.bot:
            continue
        is_tagged = member_is_tagged(member, guild.id)
        has_role = role in member.roles
        try:
            if is_tagged and not has_role:
                await member.add_roles(role, reason='Server tag sync')
                added += 1
                logger.info(f'[sync] Added {SUPER_MEMBER_ROLE_NAME} to {member.display_name}')
                await asyncio.sleep(0.3)
            elif not is_tagged and has_role:
                await member.remove_roles(role, reason='Server tag sync')
                removed += 1
                logger.info(f'[sync] Removed {SUPER_MEMBER_ROLE_NAME} from {member.display_name}')
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f'[sync] Failed to update role for {member.display_name}: {e}')
 
        if i % 200 == 0:
            logger.info(f'[sync] Progress: {i}/{len(members)} checked, {added} added, {removed} removed so far')
 
    logger.info(f'[sync] Done. Checked {len(members)}, added {added}, removed {removed}.')
    if status_callback:
        await status_callback(f'Sync complete. Checked {len(members)} members, added role to {added}, removed from {removed}.')
    return added, removed, len(members)
 
 
def member_is_tagged(member: discord.Member, guild_id: int) -> bool:
    """True if this member currently has this server's tag displayed."""
    pg = getattr(member, 'primary_guild', None)
    if not pg:
        return False
    return bool(pg.identity_guild_id == guild_id and pg.identity_enabled)
 
 
@bot.event
async def on_member_update(before, after):
    logger.info(f'[event] on_member_update fired for {after.display_name}')
 
    guild = after.guild
    if guild.id != GUILD_ID:
        return
 
    role = discord.utils.get(guild.roles, name=SUPER_MEMBER_ROLE_NAME)
    if not role:
        logger.info('[event] role not found')
        return
 
    before_pg = getattr(before, 'primary_guild', 'NO_ATTR')
    after_pg = getattr(after, 'primary_guild', 'NO_ATTR')
    logger.info(f'[event] before.primary_guild={before_pg!r} after.primary_guild={after_pg!r}')
 
    before_is_tagged = member_is_tagged(before, GUILD_ID)
    after_is_tagged = member_is_tagged(after, GUILD_ID)
    logger.info(f'[event] before_tagged={before_is_tagged} after_tagged={after_is_tagged}')
 
    if not before_is_tagged and after_is_tagged:
        try:
            await after.add_roles(role, reason='Enabled server tag')
            logger.info(f'[event] added role to {after.display_name}')
            if LOG_CHANNEL_ID:
                ch = bot.get_channel(LOG_CHANNEL_ID)
                if ch:
                    em = discord.Embed(title='✅ Super Member Added', description=f'{after.mention} enabled server tag', color=discord.Color.gold())
                    await ch.send(embed=em)
        except Exception as e:
            logger.error(f'Failed to add role to {after.display_name}: {e}')
 
    elif before_is_tagged and not after_is_tagged:
        try:
            await after.remove_roles(role, reason='Disabled server tag')
            logger.info(f'[event] removed role from {after.display_name}')
            if LOG_CHANNEL_ID:
                ch = bot.get_channel(LOG_CHANNEL_ID)
                if ch:
                    em = discord.Embed(title='❌ Super Member Removed', description=f'{after.mention} disabled server tag', color=discord.Color.red())
                    await ch.send(embed=em)
        except Exception as e:
            logger.error(f'Failed to remove role from {after.display_name}: {e}')
 
 
@bot.slash_command(name='synctags', description='Manually sync the Super Member role for everyone based on current server tag')
async def synctags(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.respond('❌ No permission', ephemeral=True)
        return
 
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name=SUPER_MEMBER_ROLE_NAME)
    if not role:
        await ctx.respond(f'❌ Could not find a role named "{SUPER_MEMBER_ROLE_NAME}"', ephemeral=True)
        return
 
    await ctx.respond(f'🔄 Syncing {SUPER_MEMBER_ROLE_NAME} role for {len(guild.members)} members. This may take a bit - check the bot logs for progress.', ephemeral=True)
    added, removed, checked = await sync_all_tag_roles(guild, role)
    await ctx.followup.send(f'✅ Done. Checked {checked} members - added role to {added}, removed from {removed}.', ephemeral=True)
 
 
@bot.slash_command(name='checktag', description='Check a member\'s current server tag and Super Member role status')
async def checktag(ctx, member: discord.Member = None):
    member = member or ctx.author
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name=SUPER_MEMBER_ROLE_NAME)
 
    pg = getattr(member, 'primary_guild', None)
    is_tagged = member_is_tagged(member, guild.id)
    has_role = role in member.roles if role else False
 
    em = discord.Embed(title=f'Tag status for {member.display_name}', color=discord.Color.blurple())
    em.add_field(name='Has server tag displayed', value=str(is_tagged))
    em.add_field(name=f'Has {SUPER_MEMBER_ROLE_NAME} role', value=str(has_role))
    em.add_field(name='Raw primary_guild', value=str(pg) if pg else 'None', inline=False)
    await ctx.respond(embed=em, ephemeral=True)
 
 
 
async def warn(ctx, member: discord.Member, *, reason: str = 'No reason'):
    if not ctx.author.guild_permissions.administrator:
        await ctx.respond('❌ No permission', ephemeral=True)
        return
 
    wid = db.add_warning(ctx.guild.id, member.id, reason, ctx.author.id)
    warnings = db.get_warnings(ctx.guild.id, member.id)
    count = len(warnings)
 
    punishment = None
    if count == 1:
        punishment = '1 hour timeout'
        try:
            await member.timeout(timedelta(hours=1))
        except Exception:
            pass
    elif count == 2:
        punishment = '24 hour timeout'
        try:
            await member.timeout(timedelta(hours=24))
        except Exception:
            pass
    elif count >= 3:
        punishment = 'banned'
        try:
            await ctx.guild.ban(member, reason=reason)
        except Exception:
            pass
 
    try:
        em = discord.Embed(title='⚠️ Warning', color=discord.Color.orange())
        em.add_field(name='#', value=str(count))
        em.add_field(name='Reason', value=reason)
        em.add_field(name='By', value=ctx.author.mention)
        em.add_field(name='Punishment', value=punishment)
        await member.send(embed=em)
    except Exception:
        pass
 
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if ch:
            em = discord.Embed(title='⚠️ Warning Issued', color=discord.Color.orange())
            em.add_field(name='Member', value=member.mention)
            em.add_field(name='#', value=str(count))
            em.add_field(name='Reason', value=reason)
            em.add_field(name='Punishment', value=punishment)
            await ch.send(embed=em)
    except Exception:
        pass
 
    await ctx.respond(f'✅ Warned {member.mention} (#{count}) - {punishment}')
 
 
@bot.slash_command(name='warnings', description='View warnings')
async def warnings(ctx, member: discord.Member):
    warns = db.get_warnings(ctx.guild.id, member.id)
    if not warns:
        await ctx.respond(f'{member.mention} has no warnings', ephemeral=True)
        return
 
    em = discord.Embed(title=f'Warnings for {member.display_name}', color=discord.Color.orange())
    for i, w in enumerate(warns, 1):
        em.add_field(name=f'#{i} (ID: {w["id"]})', value=f'{w["reason"]}', inline=False)
    await ctx.respond(embed=em, ephemeral=True)
 
 
@bot.slash_command(name='removewarn', description='Remove a warning')
async def removewarn(ctx, warning_id: int):
    if not ctx.author.guild_permissions.administrator:
        await ctx.respond('❌ No permission', ephemeral=True)
        return
    db.remove_warning(warning_id)
    await ctx.respond(f'✅ Removed warning #{warning_id}')
 
 
@bot.slash_command(name='clearwarns', description='Clear all warnings')
async def clearwarns(ctx, member: discord.Member):
    if not ctx.author.guild_permissions.administrator:
        await ctx.respond('❌ No permission', ephemeral=True)
        return
    count = len(db.get_warnings(ctx.guild.id, member.id))
    db.clear_warnings(ctx.guild.id, member.id)
    await ctx.respond(f'✅ Cleared {count} warnings for {member.mention}')
 
 
if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
 

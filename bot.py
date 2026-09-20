import os
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

@bot.slash_command(name='warn', description='Warn a member')
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
        except:
            pass
    elif count == 2:
        punishment = '24 hour timeout'
        try:
            await member.timeout(timedelta(hours=24))
        except:
            pass
    elif count >= 3:
        punishment = 'banned'
        try:
            await ctx.guild.ban(member, reason=reason)
        except:
            pass
    
    try:
        em = discord.Embed(title='⚠️ Warning', color=discord.Color.orange())
        em.add_field(name='#', value=str(count))
        em.add_field(name='Reason', value=reason)
        em.add_field(name='By', value=ctx.author.mention)
        em.add_field(name='Punishment', value=punishment)
        await member.send(embed=em)
    except:
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
    except:
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

from datetime import datetime, time, timedelta, timezone
import discord
from discord.commands import Option
from discord.commands import SlashCommandGroup
from discord.ext import commands, tasks
from glowbot.config import global_config
from glowbot.db import HLL_Player, get_player_by_discord_id
from glowbot.hll_rcon_client import HLL_RCON_Client, rcon_time_str_to_datetime
import logging

class BotCommands(commands.Cog):
    """
    Cog to manage discord interactions.
    """

    hll = SlashCommandGroup('hll')

    def __init__(self, bot):
        self.bot = bot
        self.client = bot.client
        self.logger = logging.getLogger(__name__)

        # Initialize RCON connections
        self.client.connect()
    
    @hll.command()
    async def steam64id(self, ctx: discord.ApplicationContext, steam64: Option(
            str,
            'Your Steam ID (64 version, NOT 32 version)',
            required=True,
        )
    ):
        """Register your discord account to your steam64Id"""

        await ctx.defer()

        # See if the user already has an entry
        query_result = await HLL_Player.filter(steam_id_64=steam64)
        if len(query_result) != 1:
            self.logger.error('Player lookup during steam64id returned multiple results:')
            await ctx.respond(f'Found multiple players with that `steam64id` - that shouldn\t happen! Please contact an administrator.')
            return
        elif len(query_result) == 0:
            # No entry found, make a new one
            player = HLL_Player(
                steam_id_64=steam64,
                player_name=ctx.author.name,
                discord_id=ctx.author.id,
                seeding_time_balance=timedelta(minutes=0),
                total_seeding_time=timedelta(minutes=0),
                last_seed_check=datetime.now(),
            )
            self.logger.debug(f'Discord user {ctx.author.name} is registering steam64id `{steam64}` to {player.discord_id}')
            await player.save()
            await ctx.respond(f'{ctx.author.mention}: I\'ve registered your `steam64id` to your Discord account. Thanks!')
            return
        else:
            # Found one existing entry
            player = query_result[0]
            if player.discord_id is None:
                player.discord_id = ctx.author.id
                await player.save()
                self.logger.debug(f'Updated user {ctx.author.mention} with steam64id `{steam64}`')
                await ctx.respond(f'{ctx.author.mention}: I\'ve registered your `steam64id` to your Discord account. Thanks!')
                return
            elif player.discord_id == ctx.author.id:
                await ctx.respond(f'That `steam64id` is already registered to you!')
                return
            else:
                self.logger.debug(f'Discord user {ctx.author.name} attempted to register steam64id `{steam64}` but it is already owned by Discord user {player.discord_id}')
                await ctx.respond(f'That `steam64id` is already registered to someone else.')
                return
    
    @hll.command()
    async def seeder(self, ctx: discord.ApplicationContext):
        """Check your seeding statistics"""

        await ctx.defer()
        query_result = await HLL_Player.filter(discord_id=ctx.author.id)
        if len(query_result) == 0:
            await ctx.respond(f'Your Discord ID doesn\'t match any known `steam64id`. Use `/hll steam64id` to tie your ID to your discord.')
            return
        player = query_result[0]
        message = f'Seeding stats for {ctx.author.mention}:'
        message += f'\n 🌱 Total seeding time (hours): `{player.total_seeding_time}`'
        message += f'\n 🏦 Unspent seeding time balance (hours): `{player.seeding_time_balance}`'
        message += f'\n 🕰️ Last seeding time: `{player.last_seed_check}`'
        message += f'\n ℹ️ Turn your seeding hours into VIP time with `/hll claim`. '
        await ctx.respond(message)

    @hll.command()
    async def vip(self, ctx: discord.ApplicationContext):
        """Check your VIP status"""

        await ctx.defer()
        self.logger.debug(f'VIP query for `{ctx.author.id}/{ctx.author.name}`.')
        player = await get_player_by_discord_id(ctx.author.id)
        if player is None:
            await ctx.respond(f'Your Discord ID doesn\'t match any known `steam64id`. Use `/hll steam64id` to tie your ID to your discord.')
            return

        # We need to ensure we get the same VIP states for both RCON's.
        vip_dict = await self.client.get_vip(player.steam_id_64)
        vip_entries = []
        for key, vip in vip_dict.items():
            vip_entries.append(vip)

        if all(val != vip_entries[0] for val in vip_entries):
            # VIP from all RCON's didn't match, notify.
            await ctx.respond(f'{ctx.author.mention}: It looks like your VIP status is different between servers, please contact an admin.')
            return

        # All is well, return to the (identical) first in the list
        vip = vip_entries.pop()

        if vip == None or vip['vip_expiration'] == None:
            await ctx.respond(f'No VIP record found for {ctx.author.mention}.')
            return  

        expiration = rcon_time_str_to_datetime(vip['vip_expiration'])
        if expiration.timestamp() < datetime.now().timestamp():
            await ctx.respond(f'{ctx.author.mention}: your VIP appears to have expired.')
            return
        await ctx.respond(f'{ctx.author.mention}: your VIP expiration date is `{expiration}`')

    @hll.command()
    async def claim(self, ctx: discord.ApplicationContext, hours: Option(
            int,
            'Redeem seeding hours for VIP status',
            required=False,
        )
    ):
        """Redeem seeding hours for VIP status"""
        await ctx.defer()
        if hours is None:
            vip_value = global_config['hell_let_loose']['seeder_vip_reward_hours']
            message = f'{ctx.author.mention}:'
            message += f'\n💵 Use `/hll claim $HOURS` to turn seeding hours into VIP status.'
            message += f'\n🚜 One hour of seeding time is `{vip_value}` hour(s) of VIP status.'
            message += f'\nℹ️ Check your seeding hours with `/hll seeder`.'
            await ctx.respond(message)
        else:
            player = await get_player_by_discord_id(ctx.author.id)
            if player is None:
                message = f'{ctx.author.mention}: Can\'t find your ID to claim VIP.'
                message += f'\nMake sure you have run `/hll steam64id` and registered your Steam and Discord.'
                await ctx.respond(message)
                return
            else:
                self.logger.debug(f'User \"{ctx.author.name}/{player.steam_id_64}\" is attempting to claim {hours} seeder hours from their total of {player.seeding_time_balance}.')
                if hours > player.seeding_time_balance.seconds // 3600:
                    await ctx.respond(f'{ctx.author.mention}: ❌ Sorry, not enough banked time to claim `{hours}` hour(s) of VIP (Currently have `{player.seeding_time_balance.seconds // 3600}` banked hours).')
                    return
                else:
                    player.seeding_time_balance -= timedelta(hours=hours)

                    # Check the previous VIP values from both RCON's to ensure they are identical prior to proceeding
                    vip_dict = await self.client.get_vip(player.steam_id_64)
                    vip_entries = []
                    for key, vip in vip_dict.items():
                        vip_entries.append(vip)
                    if all(val != vip_entries[0] for val in vip_entries):
                        # VIP from all RCON's didn't match, notify.
                        await ctx.respond(f'{ctx.author.mention}: It looks like your VIP status is different between servers, please contact an admin.')
                        return

                    # All is well, return to the (identical) first in the list
                    vip = vip_entries.pop()
                    
                    grant_value = global_config['hell_let_loose']['seeder_vip_reward_hours'] * hours
                    if vip is None or vip['vip_expiration'] == None:
                        expiration = datetime.now() + timedelta(hours=grant_value)
                    else:
                        # Check if current expiration is in the past.  If it is, set it to current time.
                        cur_expiration = rcon_time_str_to_datetime(vip['vip_expiration'])
                        if cur_expiration.timestamp() < datetime.now().timestamp():
                            cur_expiration = datetime.now()

                        expiration = cur_expiration + timedelta(hours=grant_value)

                    # Make sure all RCON grants are successful.
                    result_dict = await self.client.grant_vip(player.player_name, player.steam_id_64, expiration.strftime("%Y-%m-%dT%H:%M:%S%z"))
                    for rcon, result in result_dict.items():
                        if result is False:
                            self.logger.error(f'Problem assigning VIP in `claim` for \"{rcon}\": {result}')
                            await ctx.respond(f'{ctx.author.mention}: There was a problem on one of the servers assigning your VIP.')
                            return

                    message = f'{ctx.author.mention}: You\'ve added `{grant_value}` hour(s) to your VIP status.'
                    message += f'\nYou have VIP until `{expiration}`'
                    message += f'\nYour remaining seeder balance is `{player.seeding_time_balance}` hour(s).'
                    message += f'\n💗 Thanks for seeding! 💗'
                    await player.save()
                    await ctx.respond(message)
                    return

        self.logger.fatal(f'Failed claiming VIP for \"{ctx.author.name}/{player.steam_id_64}\: {result}')
        await ctx.respond(f'{ctx.author.mention}: There was a problem claiming VIP.')
    
    @commands.Cog.listener()
    async def on_application_command_error(
        self, ctx: discord.ApplicationContext, error: discord.DiscordException):
        """Handle exceptions and discord errors, including permissions"""

        if isinstance(error, commands.NotOwner):
             await ctx.respond('Insufficient privileges to use that command.')
        else:
            await ctx.respond("Whoops! An internal error occurred. Please ping my maintainer!")
            raise error
    
    def cog_unload(self):
        pass

def setup(bot):
    bot.add_cog(BotCommands(bot))

def teardown(bot):
    pass

from os import replace
import database

import discord
from discord_slash import SlashCommand

from number_parser import parse_number
import random, ast, time, re

import yaml
try: from yaml import CLoader as Loader
except ImportError: from yaml import Loader

# Load configuration
bot_config = dict()
with open('./config.yml') as file:
    yml = yaml.load(file.read(), Loader=Loader)
    
    # Parse recursively to alter all dictionary keys
    def parse_ymlconfiguration(cfg:dict):
        result = dict()
        
        if isinstance(cfg, dict):
            for k, v in dict(cfg).items():
                if isinstance(v, dict) or isinstance(v, list):
                    v = parse_ymlconfiguration(v)
                result[str(k).lower().replace(' ', '_')] = v
        
        elif isinstance(cfg, list):
            result = list()
            for x in cfg:
                if isinstance(x, dict) or isinstance(x, list):
                    x = parse_ymlconfiguration(x)
                result.append(x)
        
        else:
            return cfg
              
        return result
    
    bot_config = parse_ymlconfiguration(dict(yml))         
    assert '<token>' not in repr(bot_config).lower() and 'token' in bot_config, 'Please add your token to the config!'

# Set up intents and create client
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
slash = SlashCommand(client, sync_commands=True)

settings = {
    'punishment': {
        'type': int,
        'default': 1,
        'description': '0: none\n\t1: shame once\n\t2: shame constantly\n\t3: mute\n\t4: kick loser\n\t5: tempban loser\n\t6: ban loser' # TODO: add musical chairs
    },
    'allow_global_punishments': {
        'type': bool,
        'default': True,
        'description': 'Punish those who failed in other servers (with this server\'s punishment)'
    },
    'punishment_duration': {
        'type': float,
        'default': 1.0*60,
        'description': 'Time, in minutes, losers are punished after their mistake'
    },
    'universal_counting': {
        'type': bool,
        'default': False,
        'description': 'The highest number across all counting channels goes!'
    }
}
server_config = database.Configuration('ServerConfig', default_settings={**{k: v['default'] for k, v in settings.items()}, **{
    'counting_channels': dict(), # {channel id: {'number': current number, 'user': userid}}
    'users_lost': dict() # {user id: last_lost timestamp}
}})

user_data = database.Configuration('UserData', {
    'number_info': dict(), # stores correct numbers as {number: times posted} ({1: 2})
    'times_lost': 0,
    'last_loss': 0.0,
    'times_cheated': 0,
    'times_attempted_to_cheat': 0,
    'last_cheated': 0.0
})

@client.event
async def on_ready():
    print('\n', 'Bot B00ted', f'Invite link: https://discord.com/oauth2/authorize?client_id={client.user.id}&scope=bot%20applications.commands', '\n', sep='\n')

@client.event
async def on_message(message):
    if message.author.bot: return
    
    server_settings = server_config.get_all(message.guild.id)
    if str(message.channel.id) in server_settings['counting_channels']:
        channel_info = server_settings['counting_channels'][str(message.channel.id)]
        
        # Make variables for values
        if server_settings['universal_counting']:
            last_number = max(map(lambda _, cinfo: cinfo['number'], server_settings['counting_channels'].items()))
        else:
            last_number = channel_info['number']
        
        expected_number = last_number + 1 # TODO: add alternate modes. Counting by 1 is lame        
        last_user = channel_info['user']
        said_number = parse_number(message.content)
        
        # If a number was found, and the said number is the right one, and the user is different...
        # TODO: split into different ifs 13 lines before to reduce sql calls
        if said_number:
            if said_number == expected_number and str(last_user) not in str(message.author.id):
                # The number is correct, so update channel info
                channel_info['user'] = str(message.author.id)
                channel_info['number'] = expected_number
                server_settings['counting_channels'][str(message.channel.id)] = channel_info
                server_config.set(message.guild.id, 'counting_channels', server_settings['counting_channels'])
                
                # Update user info
                usrinfo = user_data.get_all(message.author.id)
                if str(said_number) in usrinfo['number_info']: usrinfo['number_info'][str(said_number)] = int(usrinfo['number_info'][str(said_number)]) + 1
                else: usrinfo['number_info'][str(said_number)] = 1
                user_data.set_all(message.author.id, usrinfo)
                
                await message.add_reaction('✅')
            
            elif expected_number > 1: # No loss if no start
                # The user said a number and failed
                channel_info['user'] = '000000000000000000'
                channel_info['number'] = 0
                server_settings['users_lost'][str(message.author.id)] = time.time()
                server_settings['counting_channels'][str(message.channel.id)] = channel_info
                server_config.set(message.guild.id, 'counting_channels', server_settings['counting_channels'])
                
                # Update user info
                usrinfo = user_data.get_all(message.author.id)
                usrinfo['times_lost'] = int(usrinfo['times_lost']) + 1
                usrinfo['last_loss'] = time.time()
                user_data.set_all(message.author.id, usrinfo)
                
                await message.add_reaction('❌')
                
                
                quip = get_message(['loss', 'quips'], message=message)
                verb = get_message(['loss', 'verbs'], message=message)
                await message.channel.send(f'<@{message.author.id}> {verb}' + '\n' + quip)
                
                # Apply punishments
                await _apply_punishments(message, message.author, server_settings, usrinfo, apply_onetime=True)
    else:
        # If this is not a counting channel, apply constant punishments
        user_info = user_data.get_all(message.author.id)
        await _apply_punishments(message, message.author, server_settings, user_info, apply_onetime=False)

@client.event
async def on_member_join(member):
    # Find any counting channel
    server_settings = server_config.get_all(member.guild.id)
    channel_id = random.choice(list(server_settings['counting_channels'].items()))[0]
    member = member.guild.get_member(member.id)
    
    channel = client.get_channel(int(channel_id))
    await _apply_punishments(channel, member, server_settings, user_data.get_all(member.id))

async def _apply_punishments(message:discord.Message or discord.TextChannel, user:discord.User, server_settings:dict, user_info:dict, apply_onetime:bool=False):    
    timestamp = user_info['last_loss'] if server_settings['allow_global_punishments'] else server_settings['users_lost'][str(user.id)]
    if not time.time() <= timestamp + 60*server_settings['punishment_duration']: return
    
    channel = message.channel if isinstance(message, discord.Message) else message
    
    punishment = server_settings['punishment']
    if (punishment == 1 and apply_onetime) or punishment == 2:
        await channel.send(get_message(['punishments', 'shame'], user=user, message=message))
        
    elif punishment == 3 and isinstance(message, discord.Message):
        await message.delete()
        if random.random() < 0.33:
            await channel.send(get_message(['punishments', 'mute'], user=user, message=message))
    
    elif (punishment == 4 and apply_onetime) or punishment == 5:
        await user.kick(reason='Get good at counting')
    
    elif punishment == 6:
        await user.ban()
        
@slash.subcommand(
    base='counting',
    base_description='Learn 2 count',
    name='statistics',
    description='Get a user\'s statistics',
    options=[
            {
                'name': 'user',
                'description': 'Whose info do you want?',
                'type': 6,
                'required': False,
            }
        ]
    )
async def _counting_userinfo(ctx, user:discord.User=None):
    if not user: user = ctx.author
    user_info = user_data.get_all(user.id).copy()
    
    if user.id == client.user.id:
        await ctx.send(get_message(['statistics', 'me'], ctx=ctx))
        return
    elif user.bot:
        await ctx.send(get_message(['statistics', 'bot'], ctx=ctx))
        return
    
    # Delete internals
    for todel in bot_config['statistics']['ignore']:
        del user_info[todel]
    
    # Convert ugly numberinfo dict
    number_info = user_info['number_info']
    if len(number_info) > 0:
        user_info['favorite_number'] = max(number_info.items(), key=lambda t: int(t[1]))[0]
        user_info['numbers_counted'] = sum([v for _, v in number_info.items()])
    else:
        user_info['favorite_number'] = 'None'
        user_info['numbers_counted'] = '0'
    
    del user_info['number_info']
    
    n, t, a = '\n', '\t', '\''
    await ctx.send(f'''{user.name}{a}s Statistics:```{f'{n}'.join([f'{k.replace("_"," ").title()}{n}{t}{str(v).replace("_"," ").title()}' for k, v in user_info.items()])}```''')
    
@slash.subcommand(
    base='counting',
    base_description='Learn 2 count',
    name='toggle',
    description='Toggles counting in this channel. Requires manage server',
    )
async def _counting_toggle(ctx):
    n = '\n'
    server_channels = server_config.get(ctx.guild.id, 'counting_channels')
    if await has_permissions(ctx.author, ctx.channel):
        if str(ctx.channel.id) in server_channels:
            del server_channels[str(ctx.channel.id)]
            server_config.set(ctx.guild.id, 'counting_channels', server_channels)
            await ctx.send('Counting Disabled')
        else:
            server_channels[str(ctx.channel.id)] = {'number': 0, 'user': '000000000000000000'}
            server_config.set(ctx.guild.id, 'counting_channels', server_channels)
            await ctx.send('Counting Enabled')
        
    else:
        quip = get_message(['toggle', 'fail'], ctx=ctx)
        ed = 'Disabling' if str(ctx.channel.id) in server_channels else 'Enabling'
        await ctx.send(f'{ed} counting here requires manage server{n}{quip}')

@slash.subcommand(
    base='counting',
    base_description='Learn 2 count',
    name='list',
    description='View Stuff',
    options=[
        {
            'name': 'option',
            'description': 'What do you want to list?',
            'type': 3,
            'required': True,
            'choices': [
                {
                    'name': 'counting channels',
                    'value': 'channels'
                },
                {
                    'name': 'settings',
                    'value': 'settings'
                }
            ] 
        }
    ]
    )
async def _counting_list(ctx, option:str):
    if 'channel' in option.lower():
        await _counting_list_channels(ctx)
    elif 'setting' in option.lower():
        await _counting_settings_adjust.func(ctx, 'list')
    else:
        await ctx.send('Pick channels or settings\n' + get_message(['list', 'incorrect_option'], ctx=ctx))
        
async def _counting_list_channels(ctx):
    n, t, a = '\n', '\t', '\''
    channels = server_config.get(ctx.guild.id, 'counting_channels')
    if len(channels) > 0:
        await ctx.send(
            # Add entries with users
            '\n'.join(
                f'<#{k}>:{n}{t}Current Number: {v["number"]}{n}{t}Last Counter: <@{v["user"]}>' for k, v in channels.items() if '000000000000000000' not in str(v["user"])
            ) + 
            # Add entries without users
            '\n'.join(
                f'<#{k}>:{n}{t}Current Number: {v["number"]}' for k, v in channels.items() if '000000000000000000' in str(v["user"])
            ))
        
    elif await has_permissions(ctx.author, ctx.channel):
        await ctx.send(get_message(['list', 'admin'], ctx=ctx))
    else:
        quip = get_message(['list', 'no_channels'], ctx=ctx)
        await ctx.send(f'There are no counting channels{n}{quip}')
        

@slash.subcommand(
    base='counting',
    base_description='Learn 2 count',
    subcommand_group='settings',
    subcommand_group_description='View or adjust settings',
    name='view',
    description='View any or all settings',
    options=[
        {
            'name': 'setting',
            'description': 'Name of the setting or "list" to list',
            'type': 3,
            'required': False,
            'choices': [{'name': 'lists settings', 'value': 'list'}] + [{'name': k.replace('_',' ').title(), 'value': k} for k in settings.keys()]
        },
    ])
async def _counting_settings_view(ctx, setting:str='list'):
    await _counting_settings_adjust.func(ctx, setting)


@slash.subcommand(
    base='counting',
    base_description='Learn 2 count',
    subcommand_group='settings',
    subcommand_group_description='View or adjust settings',
    name='adjust',
    description='Change a setting. Requires manage server',
    options=[
        {
            'name': 'setting',
            'description': 'Name of the setting',
            'type': 3,
            'required': True,
            'choices': [{'name': k.replace('_',' ').title(), 'value': k} for k in settings.keys()]
        },
        {
            'name': 'value',
            'description': 'What to set the setting to. Overwrites.',
            'type': 3,
            'required': True
        }
    ])
async def _counting_settings_adjust(ctx, setting:str='list', value:str=None):
    server_settings = server_config.get_all(ctx.guild.id)
    has_perms = await has_permissions(ctx.author, channel=ctx.channel)
    
    # If you somehow mess up an option, complain
    additional_placeholders = {'setting': setting.replace("_"," ").title()}
    if setting.lower() not in settings or not has_perms:
        n, t, a = '\n', '\t','\''
        
        if value and not has_perms:
            quip = 'Changing settings requires Manage Server. ' + get_message(['settings', 'no_perms'], ctx=ctx, additional_kwds=additional_placeholders)
        else:
            quip = f'''Setting not found. {get_message(['settings', 'wrong_setting'], ctx=ctx, additional_kwds=additional_placeholders)}''' if not 'list' in setting.lower() else ''
        
        await ctx.send(f'''{quip}{n}```{f'{n}{n}'.join([f'{k.replace("_"," ").title()}:{n}{t}{v["description"]}{n}{t}Default: {v["default"]}{n}{t}Current: {server_settings[k]}' for k, v in settings.items()])}```''')
    
    # If you omit value, complain and resend help
    elif value == None:
        n, t = '\n', '\t'
        quip = get_message(['settings', 'set_to_none'], ctx=ctx, additional_kwds=additional_placeholders)
        await ctx.send(f'''{quip}{n}```{setting.replace("_"," ").title()}:{n}{t}{settings[setting]['description']}{n}{t}Default: {settings[setting]["default"]}{n}{t}Current: {server_settings[setting]}```''')
    
    # We know you entered a value and a valid command
    else:
        try: parsed = ast.literal_eval(value)
        except ValueError: parsed = value
        
        # If the value has the wrong type, complain
        if not isinstance(parsed, settings[setting]['type']) and not (type(parsed) in (int, float) and settings[setting]['type'] in (int, float)):
            quip = get_message(['settings', 'wrong_type'], ctx=ctx, additional_kwds=additional_placeholders)
            await ctx.send(f'{quip}. You gave a `{type(parsed).__name__}` instead of a `{settings[setting]["type"].__name__}`')
        
        # If everthing is good, update
        else:
            if settings[setting]['type'] == int: parsed = int(parsed)
            elif settings[setting]['type'] == float: parsed = float(parsed)
            #elif settings[setting]['type'] == bool and isinstance(parsed, str): parsed = bool('true' in parsed.lower())
            
            previous = server_settings[setting]
            print(parsed)
            server_config.set(ctx.guild.id, setting, parsed)
            await ctx.send(f'{setting.replace("_"," ").title()} updated from {previous} to {parsed}')

async def has_permissions(member:discord.Member, channel:discord.abc.GuildChannel = None):
    if channel:
        perms = channel.permissions_for(channel.guild.get_member(int(member.id)))
    else:
        if not isinstance(member, discord.Member): raise ValueError('Can only get perms of discord.Member. Supply a channel')
        perms = member.guild_permissions
    
    return (hasattr(perms, 'administrator') and perms.administrator) or (hasattr(perms, 'manage_channels') and perms.manage_channels)

def get_message(msg_path:list, ctx = None, message:discord.Message=None, user:discord.User=None, additional_kwds:dict=dict()):
    if 'token' in msg_path[0].lower(): raise ValueError('Never retreive token in a sendable format!')
    
    # Construct kwds dict
    kwds = dict()
    if ctx:
        if not message: message = ctx.message
        if not user: user = ctx.author
    
    if message:
        if not user: user = message.author
        kwds['channel'] = f'<#{message.channel.id}>'
        kwds['channel_name'] = f'{message.channel.name}'.title()
        kwds['message'] = message.content
        kwds['server'] = message.channel.guild.name
    
    if user:
        kwds['id'] = f'{user.id}'
        kwds['ping'] = f'<@{user.id}>'
        kwds['name'] = f'{user.name}'
    
        
    kwds = {**kwds, **additional_kwds}
    
    # Fetch msg
    msg = bot_config
    for k in msg_path:
        msg = msg[k]
        if isinstance(msg, str) or isinstance(msg, list):
            break
    
    # Pick a random message
    if isinstance(msg, dict): # Should never happen
        msg = random.choice(list(msg.values()))[1]
    if isinstance(msg, list):
        msg = random.choice(msg)
    if not isinstance(msg, str):
        raise ValueError('Message is not string. Double check config and config path!')
    
    
    # Replace all placeholders
    for k, v in kwds.items():
        msg = msg.replace('{' + str(k) + '}', v)
        
    # Replace all unknown placeholders
    msg = re.sub(r'{.*?}', '...', msg)
    return msg
        
client.run(bot_config['token'])
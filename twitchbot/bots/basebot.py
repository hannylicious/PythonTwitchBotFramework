import logging
import time
from asyncio import get_event_loop
from typing import Optional, TYPE_CHECKING

from ..poll import PollData
from .. import util, create_irc
from ..channel import Channel, channels
from ..command import Command, commands, CustomCommandAction, is_command_on_cooldown, get_time_since_execute, \
    update_command_last_execute
from ..config import cfg, get_nick, get_command_prefix
from ..config import generate_config
from ..database import get_custom_command
from ..disabled_commands import is_command_disabled
from ..emote import update_global_emotes
from ..enums import Event
from ..enums import MessageType, CommandContext
from ..events import trigger_event
from ..exceptions import InvalidArgumentsError
from ..irc import Irc
from ..message import Message
from ..modloader import Mod
from ..modloader import mods
from ..modloader import trigger_mod_event
from ..permission import perms
from ..shared import set_bot
from ..util import stop_all_tasks
from ..command_whitelist import is_command_whitelisted, send_message_on_command_whitelist_deny
from ..poll import poll_event_processor_loop
from ..event_util import forward_event_with_results, forward_event
from ..pubsub import PubSubClient

if TYPE_CHECKING:
    from ..pubsub import PubSubData


# noinspection PyMethodMayBeStatic
class BaseBot:
    def __init__(self):
        self.irc: Optional[Irc] = None
        self._running = False
        self.pubsub = PubSubClient()
        set_bot(self)

    # region events
    async def on_bot_banned_from_channel(self, msg: Message, channel: Channel):
        """
        triggered when the bot attempts to join a banned channel
        :param msg: the message that twitch sent saying the bot was banned
        :param channel: the channel the bot was banned from
        """

    async def on_bot_timed_out_from_channel(self, msg: Message, channel: Channel, seconds: int):
        """
        triggered when the bot is timed out on a channel
        :param msg: the message that twitch sent saying the bot was timed out
        :param channel: the channel the bot was timed out on
        :param seconds: how many seconds left in the timeout
        """

    async def on_mod_reloaded(self, mod: Mod):
        """
        triggered when a mod is reloaded using reload_mod() or !reloadmod
        :param mod: mod being reloaded
        """

    async def on_connected(self):
        """
        triggered when the bot connects to all the channels specified in the config file
        """

    async def on_raw_message(self, msg: Message):
        """
        triggered the instant a message is received,
        this message can be any message received,
        including twitches messages that do not have any useful information
        """

    async def on_privmsg_sent(self, msg: str, channel: str, sender: str) -> None:
        """
        triggered when the bot sends a privmsg
        """
        print(f'{sender}({channel}): {msg}')

    async def on_privmsg_received(self, msg: Message) -> None:
        """triggered when a privmsg is received, is not triggered if the msg is a command"""

    async def on_whisper_sent(self, msg: str, receiver: str, sender: str):
        """
        triggered when the bot sends a whisper to someone
        """
        print(f'{sender} -> {receiver}: {msg}')

    async def on_whisper_received(self, msg: Message):
        """
        triggered when a user sends the bot a whisper
        """

    async def on_permission_check(self, msg: Message, cmd: Command) -> bool:
        """
        triggered when a command permission check is requested
        :param msg: the message the command was found from
        :param cmd: the command that was found
        :return: bool indicating if the user has permission to call the command, True = yes, False = no
        """
        return True

    async def on_before_command_execute(self, msg: Message, cmd: Command) -> bool:
        """
        triggered before a command is executed
        :return bool, if return value is False, then the command will not be executed
        """
        return True

    async def on_after_command_execute(self, msg: Message, cmd: Command) -> None:
        """
        triggered after a command has executed
        """

    async def on_bits_donated(self, msg: Message, bits: int):
        """
        triggered when a bit donation is posted in chat
        """

    async def on_channel_raided(self, channel: Channel, raider: str, viewer_count: int):
        """
        triggered when the channel is raided
        :param channel: the channel who was raided
        :param raider: the user who raided
        :param viewer_count: the number of viewers who joined in the raid
        """

    async def on_channel_joined(self, channel: Channel):
        """
        triggered when the bot joins a channel
        """
        print(f'joined #{channel.name}')

    async def on_channel_points_redemption(self, msg: Message, reward: str):
        """
        triggered when a viewers redeems channel points for a reward
        """
        print(f'{msg.author} has redeemed channel points reward "{reward}" in #{msg.channel_name}')

    async def on_user_join(self, user: str, channel: Channel):
        """
        triggered when a user joins a channel the bot is in
        :param user: the user who joined
        :param channel: the channel that the user joined
        """

    async def on_user_part(self, user: str, channel: Channel):
        """
        triggered when a user leaves from a channel the bot is in
        :param user: the user who left
        :param channel: the channel that the user left
        """

    async def on_channel_subscription(self, subscriber: str, channel: Channel, msg: Message):
        """
        triggered when a user subscribes
        """

    async def on_poll_started(self, channel: Channel, poll: PollData):
        """
        triggered when a poll starts
        :param channel: channel the poll originated in
        :param poll: the poll that was started
        """

    async def on_poll_ended(self, channel: Channel, poll: PollData):
        """
        triggered when a poll ends
        :param channel: channel the poll originated in
        :param poll: the poll that has ended
        """

    async def on_pubsub_received(self, raw: 'PubSubData'):
        """
        triggered when data is received from the pubsub client
        """

    # endregion

    def _create_channels(self):
        for name in cfg.channels:
            chan = Channel(name, irc=self.irc)
            chan.start_update_loop()

    async def _create_irc(self):
        """
        creates the async reader/writer (using asyncio.open_connection() if not already exist),
        """
        self.irc = await create_irc()

    def _request_permissions(self):
        """requests permissions from twitch to be able to gets message tags, receive whispers, ect"""
        # enable receiving/sending whispers
        self.irc.send('CAP REQ :twitch.tv/commands')

        # enable seeing bit donations and such
        self.irc.send('CAP REQ :twitch.tv/tags')

        # enable seeing user joins
        self.irc.send('CAP REQ :twitch.tv/membership')

    async def _connect(self):
        """connects to twitch, sends auth info, and joins the channels in the config"""
        print(f'logging in as {get_nick()}')

        util.send_auth(self.irc)

        resp = (await self.irc.get_next_message()).lower()
        if 'authentication failed' in resp:
            print(
                '\n\n=========AUTHENTICATION FAILED=========\n\n'
                'check that your oauth is correct and valid and that the nick in the config is correct'
                '\nthere is a chance that oauth was good, but is not anymore\n'
                'the oauth token can be regenerated using this website: \n\n\thttps://twitchapps.com/tmi/')
            input('\n\npress enter to exit')
            exit(1)
        elif 'welcome' not in resp:
            print(
                f'\n\ntwitch gave a bad response to sending authentication to twitch server\nbelow is the message received from twitch:\n\n\t{resp}')
            input('\n\npress enter to exit')
            exit(1)

        self._request_permissions()

        for chan in channels.values():
            self.irc.send(f'JOIN #{chan.name}')

    async def get_command_from_msg(self, msg: Message) -> Optional[Command]:
        """
        checks if the start of the msg matches any command names

        if command is found: returns the command

        else: returns None
        """
        cmd = commands.get(msg.parts[0].lower())
        if cmd:
            return cmd

        cmd = get_custom_command(msg.channel_name, msg.parts[0].lower())
        if cmd:
            return CustomCommandAction(cmd)

        return None

    async def _run_command(self, msg: Message, cmd: Command):
        # [0] is needed here because get_sub_cmd() also returns the modified args relative to the level it recursively reached
        if not await cmd.get_sub_cmd(msg.args)[0].has_permission_to_run_from_msg(msg):
            await msg.reply(
                whisper=True,
                msg=f'you do not have permission to execute "{msg.content}" in #{msg.channel_name}'
            )
            return

        elif not isinstance(cmd, CustomCommandAction) and is_command_disabled(msg.channel_name, cmd.fullname):
            return await msg.reply(f'{cmd.fullname} is disabled for this channel')

        # also check if the command is whitelisted, (if its not a custom command)
        if not isinstance(cmd, CustomCommandAction) and not is_command_whitelisted(cmd.name):
            if send_message_on_command_whitelist_deny():
                await msg.reply(f'{msg.mention} "{cmd.fullname}" is not enabled in the command whitelist')
            return

        has_cooldown_bypass_permission = perms.has_permission(msg.channel_name, msg.author, cmd.cooldown_bypass)
        if (not has_cooldown_bypass_permission
                and is_command_on_cooldown(msg.channel_name, cmd.fullname, cmd.cooldown)):
            return await msg.reply(
                f'{cmd.fullname} is on cooldown, seconds left: {cmd.cooldown - get_time_since_execute(msg.channel_name, cmd.fullname)}')

        # check that all event listeners return True for this command executing
        if not all(await forward_event_with_results(Event.on_before_command_execute, msg, cmd, channel=msg.channel_name)):
            return

        try:
            await cmd.execute(msg)
            if not has_cooldown_bypass_permission:
                update_command_last_execute(msg.channel_name, cmd.fullname)
        except InvalidArgumentsError as e:
            await self._send_cmd_help(msg, cmd, e)
        else:
            forward_event(Event.on_after_command_execute, msg, cmd, channel=msg.channel_name)

    async def _send_cmd_help(self, msg: Message, cmd: Command, exc: InvalidArgumentsError):
        await msg.reply(
            f'{exc.reason} - "{cmd.fullname} {cmd.syntax}" - do "{get_command_prefix()}help {cmd.fullname}" for more details')

    # kept if needed later
    # def _load_overrides(self):
    #     for k, v in overrides.items():
    #         if k.value in self.__class__.__dict__ and k.value.startswith('on'):
    #             setattr(self, k.value, v)

    def shutdown(self):
        stop_all_tasks()
        for channel in channels:
            self.irc.send(f'PART #{channel}')
            time.sleep(.4)
        self.irc.send('QUIT')
        self._running = False

    def run(self):
        """runs/starts the bot, this is a blocking function that starts the mainloop"""
        self._running = True
        get_event_loop().run_until_complete(self.mainloop())

    async def mainloop(self):
        """starts the bot, connects to twitch, then starts the message event loop"""
        # check if user wants to input oauth info manually
        if not generate_config():
            stop_all_tasks()
            return

        await update_global_emotes()

        await self._create_irc()
        self._create_channels()
        await self._connect()
        await self.on_connected()
        await trigger_mod_event(Event.on_connected)
        await trigger_event(Event.on_connected)

        util.add_nameless_task(poll_event_processor_loop())

        while self._running:
            raw_msg = await self.irc.get_next_message()

            if not raw_msg:
                continue

            msg = Message(raw_msg, irc=self.irc, bot=self)

            forward_event(Event.on_raw_message, msg, channel=msg.channel_name)
            cmd: Command = (await self.get_command_from_msg(msg)
                            if msg.is_user_message
                            else None)

            if cmd and ((msg.is_whisper and cmd.context & CommandContext.WHISPER)
                        or (msg.is_privmsg and cmd.context & CommandContext.CHANNEL)):
                msg.safe_print()
                get_event_loop().create_task(self._run_command(msg, cmd))

            elif msg.type is MessageType.WHISPER:
                msg.safe_print()
                forward_event(Event.on_whisper_received, msg, channel=msg.channel_name)

            elif msg.type is MessageType.PRIVMSG:
                msg.safe_print()
                forward_event(Event.on_privmsg_received, msg, channel=msg.channel_name)

            elif msg.type is MessageType.USER_JOIN:
                # the bot has joined a channel
                if msg.author == get_nick():
                    forward_event(Event.on_channel_joined, msg.channel, channel=msg.channel_name)
                # user joined a channel the bot was in
                else:
                    forward_event(Event.on_user_join, msg.author, msg.channel, channel=msg.channel_name)

            elif msg.type is MessageType.USER_PART:
                forward_event(Event.on_user_part, msg.author, msg.channel, channel=msg.channel_name)

            elif msg.type is MessageType.SUBSCRIPTION:
                forward_event(Event.on_channel_subscription, msg.author, msg.channel, msg, channel=msg.channel_name)

            elif msg.type is MessageType.RAID:
                forward_event(Event.on_channel_raided, msg.channel, msg.author, msg.tags.raid_viewer_count, channel=msg.channel_name)

            elif msg.type is MessageType.PING:
                self.irc.send_pong()

            elif msg.type is MessageType.CHANNEL_POINTS_REDEMPTION:
                forward_event(Event.on_channel_points_redemption, msg, msg.reward, channel=msg.channel_name)

            elif msg.type is MessageType.BITS:
                forward_event(Event.on_bits_donated, msg, msg.tags.bits, channel=msg.channel_name)

            elif msg.type is MessageType.BOT_PERMANENTLY_BANNED:
                forward_event(Event.on_bot_banned_from_channel, msg, msg.channel, channel=msg.channel_name)

            elif msg.type is MessageType.BOT_TIMED_OUT:
                forward_event(Event.on_bot_timed_out_from_channel, msg, msg.channel, msg.timeout_seconds, channel=msg.channel_name)

        # clean up mods when the bot is exiting
        for mod in mods.values():
            # notify all mods of being unloaded,
            # this is put in a try/except
            # so that any exceptions raised from unloaded overrides will not cancel unloading the others
            try:
                await mod.unloaded()
            except Exception as e:
                print(f'\nwhen unloading mod "{mod.name}" this exception occurred:\n')
                logging.exception(e)

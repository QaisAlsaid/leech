# from enum import Enum, auto
# from typing import Any, Callable
#
# import discord
#
# import time
# import heapq
# import logging
#
# import leech.core.commands.resolver as resolver
# from leech.core.cogs.context import CogContext
# from leech.core.commands.command import Command, Argument
# from leech.core.permissions import is_owner
# from leech.core.error import BaseError
#
# TIMEOUT = 600 # 10 minutes
#
# _logger = logging.getLogger(__name__)
#
# class TimerElement[T]:
#     def __init__(self, elem: T, cb: Callable[[T], Any]):
#         self.callback: Callable[[T], Any] = cb
#         self.element: T = elem
#
# class TimerScheduler[T]:
#     def __init__(self):
#         self.timers: list[tuple[float, TimerElement[T]]] = []
#
#     def add_timer(self, elem: T, cb: Callable[[T], Any]):
#         heapq.heappush(self.timers, (time.monotonic() + TIMEOUT, TimerElement(elem=elem, cb=cb)))
#
#     async def check_expired(self):
#         now = time.monotonic()
#         while self.timers and self.timers[0][0] <= now:
#             expiration, elem = heapq.heappop(self.timers)
#             await elem.callback(elem.element)
#
# class States(str, Enum):
#     INIT = auto()
#     NAME = auto()
#     DESC = auto()
#     PROV = auto()
#     ARGS = auto()
#     SUBC = auto()
#     BACK = auto()
#     DONE = auto()
#     ERRO = auto()
#
# MAX_CMD = 25
#
# class AddInteractionIrrecoverableError(BaseError):
#     def __init__(self, message: str, is_user_error: bool = False):
#         super().__init__(message=message, is_user_error=is_user_error)
#
# class AddInteraction:
#     def __init__(self, author_id: int, channel_id: int, is_global: bool):
#         self.author_id: int = author_id
#         self.channel_id: int = channel_id
#         self.is_global: bool = is_global
#         self.state: States = States.NAME
#         self.commands: list[Command] = []
#
#     async def init(self, message: discord.Message, name: str = "command"):
#         await message.reply(f"**what do you want to call this {name}?**\nused to run this {name} either by prefix/mention or ai.");
#         self.commands.append(Command(name="", description="", arguments=[], subcommands={}))
#         self.state = States.NAME
#
#     async def name(self, message: discord.Message):
#         cmd = self.current_commnad()
#         strp = message.content.strip().lower()
#         if not strp or len(strp) > MAX_CMD or any(char.isspace() or char == '.' or char == ':' or char == '"' or char == "'" for char in strp):
#             raise BaseError(f"bad name, should be less than 25 chars and it can't contain spaces or dots or quotes.")
#
#         cmd.name = strp
#         await message.reply("**describe what this command does?**\nused to generate help commands, and to help llm to select commands contextualy.\n(reply with ``nil`` to leave empty).")
#         self.state = States.DESC
#
#     async def ask_args(self, cmd: Command, message: discord.Message):
#         await message.reply(f"**what arguments does this command take?**\n(e.g. ``>ban @opposingfork``).\narguments consist of ``args:list<arg_descriptor>``foramt is like: ``[name1:type1 name2:type2]``, you can leave as ``[]`` for no arguments.\nfor more info on supported types do ``>types help`` (or whatever prefix you have configured).")
#         self.state = States.ARGS
#
#     async def ask_provider(self, cmd: Command, message: discord.Message):
#         await message.reply("**what is the provider for this command?**\nproviders consist of ```\ntype:provider_type bot:user_id|mention path:str args:list<arg_descriptor>```\nfor more info on providers or types do ``>types help`` or ``>types help provider``.")
#         self.state = States.PROV
#
#     async def desc(self, message: discord.Message):
#         cmd = self.current_commnad()
#         strp = message.content.strip()
#         if strp.lower() == 'nil':
#             await self.ask_args(cmd, message)
#             return
#
#         cmd.description = strp
#         await self.ask_provider(cmd, message)
#
#     def parse_args(self, raw: str) -> list[Argument]:
#         tokens = []
#         ident = ''
#
#         def flush():
#             nonlocal ident
#             if ident:
#                 tokens.append(('IDENT', ident))
#             ident = ''
#
#         for char in raw:
#             match char:
#                 case '[':
#                     flush()
#                     tokens.append(('OPEN', char))
#                 case ']':
#                     flush()
#                     tokens.append(('CLOSE', char))
#                 case ':':
#                     flush()
#                     tokens.append(("COLON", char))
#                 case '.':
#                     raise BaseException("bad args: illegal token: '.'.")
#                 case '"':
#                     raise BaseException("bad args: illegal token: '\"'.")
#                 case "'":
#                     raise BaseException("bad args: illegal token: \"'\".")
#                 case _:
#                     ident += char;
#
#
#         if len(tokens) < 2 or tokens[0][0] != 'OPEN' or tokens[-1][0] != 'CLOSE':
#             raise BaseException("bad args: expected a list of args '[]'.")
#
#         index = 1
#         bound = len(tokens) - 1
#         def advance():
#             nonlocal index
#             index += 1
#             if index > bound:
#                 return None, None
#             return tokens[index][0], tokens[index][1]
#
#         args: dict[str, Argument] = {}
#         while True:
#             tn, vn = advance()
#             if not tn:
#                 if index == 2: break;
#                 raise BaseError("bad args: expected 'name'.")
#             tc, vc = advance()
#             if not tc:
#                 raise BaseError("bad args: expected ':'")
#             tt, vt = advance()
#             if not tt:
#                 raise BaseError("bad args: expected 'value'.")
#
#             if args.get(str(vn)):
#                 raise BaseError("bad arg: 'name' should be unique.")
#             type = resolver.parse_type_name(str(vt))
#             if type is None:
#                 raise BaseError(f"bad type: failed to parse type: '{vt}'")
#             args[str(vn)] = Argument(name=str(vn), type=type)
#
#         return list(args.values())
#
#     async def args(self, message: discord.Message):
#         cmd = self.current_commnad()
#         cmd.arguments = self.parse_args(message.content.strip().lower())
#         await self.subcommand_ask(message)
#
#     async def subcommand_ask(self, message: discord.Message):
#         other = "other " if self.state == States.BACK else " "
#         await message.reply(f"**do you wish to add any {other}subcommands?**\nreply with 'yes' or 'no'")
#         self.state = States.SUBC
#
#     async def end_command(self, message: discord.Message):
#         cmdname = "subcommand" if len (self.commands) > 1 else "command"
#         cmd = await self.finalize_command(message)
#         await message.reply(f"**the final json schema of your {cmdname} is:**\n```json\n{cmd.to_descriptor().json()}```")
#
#     async def subcommand_choice(self, message: discord.Message):
#         strp = message.content.strip().lower()
#         if strp == 'no':
#             await self.end_command(message)
#             return
#         await self.init(message)
#
#     async def on_message(self, message: discord.Message):
#         match self.state:
#             case States.INIT:
#                 await self.init(message)
#             case States.NAME:
#                 await self.name(message)
#             case States.DESC:
#                 await self.desc(message)
#             case States.ARGS:
#                 await self.args(message)
#             case States.SUBC:
#                 await self.subcommand_choice(message)
#
#
#     def current_commnad(self) -> Command:
#         if self.commands:
#             return self.commands[-1]
#
#         self.state = States.ERRO
#         raise AddInteractionIrrecoverableError(f"command stack is empty. (there is no current command).", is_user_error=False)
#
#
#     def root_command(self) -> Command:
#         if self.commands:
#             return self.commands[0]
#
#         self.state = States.ERRO
#         raise AddInteractionIrrecoverableError(f"command stack is empty. (there is not root command).", is_user_error=False)
#
#     def parent_command(self) -> Command | None:
#         return self.commands[-2] if len(self.commands) > 1 else None
#
#     async def finalize_command(self, message: discord.Message) -> Command:
#         current = self.current_commnad()
#         if current is None:
#             raise AddInteractionIrrecoverableError("command stack is empty. (can't finalized command).", is_user_error=False)
#         parent = self.parent_command()
#         if parent is None:
#             self.state = States.DONE
#             return current
#         if parent.subcommands.get(current.name):
#             raise BaseError(f"redefining command: '{parent.name}.{current.name}'. think of another unique name.")
#         parent.subcommands[current.name] = current
#         self.state = States.BACK
#         await self.subcommand_ask(message)
#         return self.commands.pop()
#
# class AddKey:
#     def __init__(self, author_id: int, channel_id: int):
#         self.author_id: int = author_id
#         self.channel_id: int = channel_id
#
# class AddWizard:
#     def __init__(self, context: CogContext):
#         self.context: CogContext = context
#         self.timers: TimerScheduler[AddKey] = TimerScheduler()
#         self.interactions: dict[AddKey, AddInteraction] = {}
#
#     async def on_interaction_expired(self, key: AddKey):
#         pass
#
#     async def remove_interaction(self, interaction: AddInteraction):
#         pass
#
#     async def add_interaction(self, author_id: int, channel_id: int, is_global: bool) -> AddInteraction:
#         key = AddKey(author_id, channel_id)
#         self.timers.add_timer(key, self.on_interaction_expired)
#         interaction = AddInteraction(author_id, channel_id, is_global)
#         self.interactions[key] = interaction
#         return interaction
#
#     async def start(self, message: discord.Message, is_global: bool):
#         if is_global and not is_owner(message.author.id):
#             raise BaseError("blud thinks he's an owner 😭🥀")
#         await self.add_interaction(message.author.id, message.channel.id, is_global)

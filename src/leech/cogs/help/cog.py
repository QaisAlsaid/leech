from dataclasses import dataclass

from leech.core.cog.cog import Cog, CogContext
from leech.core.command.binding import Binding
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.command.binder import CommandBinder
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.provider.runtime import Provider
from leech.core.command.native.binding import NativeBinding
from leech.core.command.self.binding import SelfBinding
from leech.core.command.slash.binding import SlashBinding
from leech.leech import Leech
from leech.core.permissions import is_owner
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.helpers import is_invocable
import leech.core.parser.lexer as lexer
import leech.core.helpers.text.paginator as paginator
import leech.core.command.resolver as resolver


def format_root_commands(commands: dict[str, Command]) -> str:
    sections = []

    for path, command in commands.items():
        if not command.is_root:
            continue

        sections.append(format_command_summary(command, path))

    if not sections:
        return "no commands available."

    return "# commands\n\n" + "\n\n".join(sections)


def format_command_type(binding: Binding) -> str:
    if isinstance(binding, NativeBinding):
        kind = "native"
    elif isinstance(binding, SelfBinding):
        kind = "self"
    elif isinstance(binding, SlashBinding):
        kind = "slash"
    elif isinstance(binding, MockBinding):
        kind = "group"
    else:
        kind = "unknown"

    return f"**type:** `{kind}`, invocable: `{is_invocable(binding)}`"


def format_command_summary(command: Command, path: str) -> str:
    lines = [
        f"`{' '.join(path.split('.'))}` - {f'alias to `{command.binding.target.name}` - ' if isinstance(command.binding, SelfBinding) else ''} {command.description}"
    ]

    if command.usage_example:
        lines.append(f"example: `{command.usage_example}`")

    lines.extend([format_command_type(command.binding)])

    if command.arguments:
        arguments = ", ".join(
            f"`{arg.name}: {arg.type.type.name}`"
            + ("" if arg.required else " (optional)")
            for arg in command.arguments
        )
        lines.append(f"arguments: {arguments}")

    return "\n".join(lines)


def format_command_details(command: Command, path: str) -> str:
    lines = [
        f"# `{' '.join(path.split('.'))}` {f'alias to `{command.binding.target.name}`' if isinstance(command.binding, SelfBinding) else ''}",
    ]
    lines.extend(
        [
            "",
            "**description**",
            command.description,
        ]
    )

    if command.usage_example:
        lines.extend([f"**example:** `{command.usage_example}`"])
    if command.help_content:
        lines.extend(["**additional help**", command.help_content])

    lines.extend(["", "**meta**"])
    lines.extend([format_command_type(command.binding)])
    if command.arguments:
        lines.extend(["**arguments**"])
    for position, arg in enumerate(command.arguments, start=1):
        required = "required" if arg.required else "optional"
        lines.append(f"- `{arg.name}` - `{arg.type.type.name}` ({required})")

        if arg.description:
            lines.append(f"  {arg.description}")

        if arg.name in command.defaults:
            lines.append(f"  default: `{command.defaults[arg.name]}`")

        if arg.usage_example:
            lines.append(f"  example: `{arg.usage_example}`")

    if command.subcommands:
        lines.extend(["", "## subcommands"])

        for child in command.subcommands.values():
            child_path = f"{path} {child.name}"
            lines.append(format_command_summary(child, child_path))
            lines.append("\n")

    return "\n".join(lines)


def format_argument_details(command: Command, argument: Argument, path: str) -> str:
    try:
        position = next(
            i for i, arg in enumerate(command.arguments, start=1) if arg is argument
        )
    except StopIteration:
        position = None

    required = "yes" if argument.required else "no"
    lines = [
        f"# argument `{argument.name}`",
        f"**meta**",
        f"command: `{' '.join(path.split('.'))}`",
    ]

    if position is not None:
        lines.append(f"declaration position: {position}")

    lines.extend(
        [
            f"type: `{argument.type.type.name}`",
            f"required: {required}",
        ]
    )

    if argument.name in command.defaults:
        lines.append(f"default: `{command.defaults[argument.name]}`")
    elif not argument.required:
        lines.append("default: not specified")

    if argument.description:
        lines.extend(["", "**description**", argument.description])

    if argument.usage_example:
        lines.extend([f"**example:** `{argument.usage_example}`"])

    if argument.help_content:
        lines.extend(["**additional help**", argument.help_content])

    if argument.type.description:
        lines.extend(
            [
                "",
                f"**type information**",
                f"**{argument.type.type}**: {argument.type.description}",
            ]
        )

    if argument.type.usage_example:
        lines.append(f"type example: `{argument.type.usage_example}`")

    return "\n".join(lines)


@dataclass
class Concept:
    blurb: str
    body: str


CONCEPTS: dict[str, Concept] = {
    "identifiers": Concept(
        blurb="how command names work, and why dots are special.",
        body=(
            "a command name is a single word: letters, digits, `_` and `-`, and it "
            "can't start with a digit. `todo`, `set-editor` and `fc2` are all valid "
            "names. `img google` is not one name, it's two **words**.\n\n"
            "dots are never part of a name. they are the registry's path "
            "separator. `todo.add` is the *path* of `add` inside `todo`. you "
            "invoke with spaces (`>todo add`). paths use dots, but user facing (`>help todo add`).\n\n"
            "template names may be dotted, and there it means nesting: a template "
            "called `fc.level` attaches as the `level` subcommand of the `fc` "
            "group, creating the group if needed. nesting only works under "
            "groups leech generated or flat template commands. never under "
            "built-in roots like `todo`, since injecting into them would corrupt "
            "them."
        ),
    ),
    "arguments": Concept(
        blurb="the parsing grammar: named, positional, greedy, defaults, quoting.",
        body=(
            "commands take arguments two ways, and they compose.\n\n"
            "**named**: `name=value`. the value is one token, except greedy (STR) "
            "arguments which swallow everything up to the next `known-name=`\n"
            "```\n"
            ">todo add task=buy milk\n"
            '>cmd invoke slash @fergun invocation="img google query=bird nice"\n'
            "```\n"
            "**positional**: bare tokens fill arguments in declaration order:\n"
            "```\n"
            ">tr hi -to arabic\n"
            "```\n"
            "the rules, exactly as they are applied:\n"
            "1. `name=` always wins: a token followed by `=` is a named argument, "
            "never a positional.\n"
            "2. positionals fill arguments in declaration order, skipping any "
            "argument already set by name or by default.\n"
            "3. arguments with defaults are invisible to positional filling. they "
            "never consume bare tokens.\n"
            "4. a greedy (STR) argument swallows all remaining tokens when every "
            "other unset argument is optional and no `name=` follows. otherwise it "
            "takes a single token.\n"
            "5. quoting collapses multi-word values into one token: "
            '`>img "hot femboy"`.\n\n'
            "rule of thumb: put greedy arguments last, name everything else, or quote."
        ),
    ),
    "types": Concept(
        blurb="the argument types and what they accept.",
        body=(
            "every argument has a type that parses and validates it:\n\n"
            "- `int`: a whole number. `index=2`\n"
            "- `num`: a decimal number. `value=6.7`\n"
            "- `bool`: truthy: `true`, `on`, `yes`; falsy: `false`, `off`, `no`. "
            "`active=yes`\n"
            "- `str`: text, greedy (see `>help concepts arguments`). quote values "
            "with spaces: `task='buy milk'`\n"
            "- `user`: a mention `<@id>` or a raw numeric id.\n"
            "- `channel`: a mention `<#id>` or a raw id.\n"
            "- `role`: a mention `<@&id>` or a raw id.\n"
            "- `attachment`: an https url to a file.\n\n"
            "mentions you type are rewritten by the discord client into their "
            "`<@id>` forms before leech ever sees them. which is why `bot=@fergun` "
            "and `bot=<@id>` are the same thing."
        ),
    ),
    "command-chain": Concept(
        blurb="how commands compose: handlers return the next command.",
        body=(
            "a handler may return the next command to run. that's a chain hop, "
            "and the dispatcher keeps invoking until nothing is returned. that's "
            "how composition works:\n\n"
            "- `>cmd invoke leech todo list` resolves `todo list`, parses your "
            "tokens against its arguments, and returns it as the next hop.\n"
            "- `>sudo <command>` re-resolves whatever you gave it and returns it "
            "elevated.\n\n"
            "what rides along a hop: the result bus (structured output), the "
            "execution journal, and `via` (who really started this chain. you or "
            "the assistant). what never rides along: elevation sudo grants it "
            "fresh every time, and nothing else can.\n\n"
            "chains are capped at 64 hops, so a loop can't run forever. don't try, ai is limited to only 3."
        ),
    ),
    "perms": Concept(
        blurb="owners, grants, scopes, sudo, and what the assistant may do.",
        body=(
            "owners (env `OWNER_ID`, comma separated) pass everything. everyone "
            "else is denied by default and unlocked with grants:\n"
            "```\n"
            ">opsec allow user=@opposingfork scope=command:todo.*\n"
            ">opsec allow user=@friend scope=*\n"
            "```\n"
            "scopes: `command:todo.*` (a subtree), `command:*`, `message:*` "
            "(talking to the assistant), `*`. the most specific rule wins. deny "
            "beats allow on a tie.\n\n"
            "**sudo** is not a permission - it's elevation, for commands that "
            "damage the bot itself (cog load/unload/reload, removing models). "
            "owner-only and human-only, structurally: the assistant can never "
            "elevate, no matter what.\n\n"
            "**the assistant** acts as you: it can never exceed your grants, and "
            "you can limit it per command (`>ai perms deny command=todo.discard`). "
            "it can also never touch `>auth` or `sudo`. this is hardcoded, not "
            "configuration."
        ),
    ),
    "cogs": Concept(
        blurb="hot-reloadable modules that own commands, listeners and tasks.",
        body=(
            "cogs are hot-swappable modules that own commands, message listeners, "
            "and background tasks:\n"
            "```\n"
            ">sudo cog list\n"
            ">sudo cog reload path=leech.cogs.ai.cog\n"
            "```\n"
            "on unload, everything a cog registered is cleaned: commands "
            "unregistered, listeners disposed, tasks cancelled. a reload never "
            "leaks. a failing load rolls back and logs; it can't take the bot "
            "down. one bad persisted template is a skipped line in the lo*g*, not a "
            "dead co*g*.\n\n"
            "load order is part of the contract: `auth` first (it owns the first "
            "listener slot (the permission gate)), `ai` last (its catalog wants "
            "the full registry)."
        ),
    ),
    "templates": Concept(
        blurb="persisted invocation shortcuts for leech, slash and prefix commands.",
        body=(
            "a template is a persisted shortcut for an invocation:\n"
            "```\n"
            '>cmd invoke template command=img type=leech invocation="fergun img google"\n'
            ">img hot femboy\n"
            "```\n"
            "it has three types: `leech` (an alias to one of my commands), `slash` "
            "(another bot's slash command. needs `bot=`), and `prefix` (another "
            "bot's prefix command. `bot=` optional: with it the reply is "
            "collected and echoed, without it it's fire and forget never echoed).\n\n"
            "dotted names nest: `fc.level` becomes `>fc level` under the `fc` "
            "group. a template pins its execution `channel` and `echo` policy at "
            "creation. both editable later (`>cmd invoke template edit`), and "
            "recreating under the same name replaces it. templates persist across "
            "restarts. the assistant can create them too. **but can never see created templates by default unless you add it to its catalog.**"
        ),
    ),
    "echo": Concept(
        blurb="what comes back when leech invokes another bot for you.",
        body=(
            "when leech invokes another bot for you, `echo` decides what comes "
            "back: `echo=<kinds>[,when]`\n\n"
            "- kinds: `text`, `image`, `video`, `audio`, `file`, `buttons` (or "
            "`media` = image+video+audio)\n"
            "- when: `auto` (default (only what you can't already see)), `always`, "
            "`never`\n\n"
            "examples: `echo=images`, `echo=always,media`, "
            "`echo=text,audio,buttons`.\n\n"
            "images echo as links and render inline; audio re-uploads so it plays "
            "in app. attachment links expire in a day approx. "
            "responses carrying buttons always get a pressable echo. reply to it "
            "with a number to press, it mirrors the original as it changes."
        ),
    ),
    "ai": Concept(
        blurb="the assistant: what it can run, what it sees, how to control it.",
        body=(
            "mention me (or reply to me) and ai answers. it can run commands on your "
            'behalf: "add buy milk to my todo". and it can see each '
            "command's results, so multi-step asks work in one message: \"make a "
            'list called our_songs and add bord bard bird".\n\n'
            "its catalog of commands is filtered by what you can do and what you've "
            "allowed it see (`>ai perms`)."
        ),
    ),
}

CONCEPT_ALIASES = {
    "parsing": "arguments",
    "named": "arguments",
    "positional": "arguments",
    "grammar": "arguments",
    "permissions": "perms",
    "chain": "command-chain",
    "cog": "cogs",
    "template": "templates",
    "assistant": "ai",
    "button": "echo",
    "buttons": "echo",
}


class HelpCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        help = Command(
            name="help",
            description="get help.",
            usage_example=">help",
            help_content="get help for all root commands or specific command path.",
            arguments=[
                Argument(
                    name="path",
                    description="the command path to get help for.",
                    usage_example="path=cmd invoke",
                    help_content="you can either leave blank to get help message for all root commands.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                )
            ],
            subcommands=self.get_subcommands(),
            binding=NativeBinding(self.help_handler),
            is_root=True,
        )
        if not self.context.add_command(help.name, help):
            raise CogError(f"failed to load '{help.name}' command.")
        sos_desc = CommandDescriptor(
            name="sos",
            description=help.description,
            arguments=[],
            subcommands={},
            provider=ProviderDescriptor(type=ProviderType.SELF, target=["help"]),
            is_root=help.is_root,
        )
        sos = self.context.client.binder.bind(sos_desc)
        if not self.context.add_command(sos.name, sos):
            raise CogError(f"failed to load '{sos.name}' self-command.")

    def get_subcommands(self) -> dict[str, Command]:
        return {**self.get_concepts()}

    def get_concepts(self) -> dict[str, Command]:
        return {
            "concepts": Command(
                name="concepts",
                description="get help regarding leech specific concepts",
                usage_example=">help concepts concept=types",
                help_content="concepts are leech specific stuff that can affect your usage experience.",
                arguments=[
                    Argument(
                        name="concept",
                        description="the concept you wish to get help for.",
                        usage_example="concept=types",
                        help_content="you can leave blank to get list of all avilable concepts.",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    )
                ],
                subcommands={},
                binding=NativeBinding(self.concepts_handler),
            )
        }

    async def help_handler(self, invocation: Invocation):
        raw_path = invocation.arguments.get("path") or ""

        if not raw_path.strip():
            text = format_root_commands(
                self.context.client.commands.registered_commands
            )
        else:
            lexed = lexer.lex(raw_path, prefixes=[])
            resolved = resolver.resolve_command(
                lexed.tokens, self.context.client.commands
            )

            if resolved is None:
                raise CogError(
                    f"command path: {raw_path} not found.\n(try ``>help``)",
                    is_user_error=True,
                )
            else:
                remaining = resolved.args

                if not remaining:
                    text = format_command_details(resolved.command, resolved.path)
                else:
                    arguments_by_name = {
                        arg.name: arg for arg in resolved.command.arguments
                    }

                    requested_names = [token.raw for token in remaining]

                    if len(requested_names) != len(set(requested_names)):
                        raise CogError(
                            "the same argument was requested more than once.",
                            is_user_error=True,
                        )

                    missing = [
                        name
                        for name in requested_names
                        if name not in arguments_by_name
                    ]

                    if missing:
                        raise CogError(
                            f"unknown argument(s): {', '.join(missing)}.",
                            is_user_error=True,
                        )

                    pages = [
                        format_argument_details(
                            resolved.command, arguments_by_name[name], resolved.path
                        )
                        for name in requested_names
                    ]

                    text = "\n\n---\n\n".join(pages)

        await paginator.paginate(
            invocation.message, text, client=self.context.client
        )

    async def concepts_handler(self, invocation: Invocation):
        raw = invocation.arguments.get("concept")

        if raw is None or not str(raw).strip():
            lines = ["**concepts**: leech specific things that affect the usage:"]
            for key, concept in CONCEPTS.items():
                lines.append(f"- `{key}` - {concept.blurb}")
            lines.append("")
            lines.append("pick one: `>help concepts concept=arguments`")
            text = "\n".join(lines)
        else:
            key = str(raw).strip().lower().replace(" ", "-")
            key = CONCEPT_ALIASES.get(key, key)
            concept = CONCEPTS.get(key)
            if concept is None:
                known = ", ".join(f"`{k}`" for k in CONCEPTS)
                raise CogError(
                    f"unknown concept: '{raw}'. available: {known}.",
                    is_user_error=True,
                )
            text = f"**concept: {key}**\n\n{concept.body}"

        await paginator.paginate(
            invocation.message, text, client=self.context.client
        )


def create_cog(context: CogContext):
    return HelpCog(context=context)

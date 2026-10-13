from leech.core.command.binding import Binding

from .runtime import Argument, Command
from .registry import CommandRegistry
from .model import CommandDescriptor
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.error import BaseError
from .self.binding import SelfBinding
from leech.core.parser.types.registry import Types, TypesRegistry
from .slash.binding import SlashBinding


class CommandBinder:
    def __init__(self, registry: CommandRegistry):
        self.registry = registry

    def bind(self, descriptor: CommandDescriptor) -> Command:
        if descriptor.provider.type == ProviderType.SELF:
            return self.bind_self(descriptor)

        subcommands: dict[str, Command] = {
            name: self.bind(subcommand)
            for name, subcommand in descriptor.subcommands.items()
        }
        arguments: list[Argument] = [
            Argument(
                name=arg.name,
                description=arg.description,
                type=TypesRegistry.require(arg.type),
                required=arg.required,
                usage_example=arg.usage_example,
                help_content=arg.help_content,
            )
            for arg in descriptor.arguments
        ]

        binding = self.bind_provider(descriptor)

        return Command(
            name=descriptor.name,
            description=descriptor.description,
            arguments=arguments,
            subcommands=subcommands,
            binding=binding,
            defaults=descriptor.defaults,
        )

    def bind_provider(self, descriptor: CommandDescriptor) -> Binding:
        provider = descriptor.provider
        if provider.type == ProviderType.SLASH:
            if provider.bot is None:
                raise BaseError("slash provider requires a bot id.")

            return SlashBinding(target=provider.target, bot=provider.bot)

        raise BaseError("unimplemented provider.", is_user_error=False)

    def bind_self(self, descriptor: CommandDescriptor) -> Command:
        if descriptor.arguments:
            raise BaseError("self command can't override arguments.")
        if descriptor.subcommands:
            raise BaseError("self command can't override subcommands.")

        target = self.registry.get_command(descriptor.provider.target[0])
        if target is None:
            raise BaseError(
                f"self provider target does not exist: '{descriptor.provider.target}'."
            )

        return Command(
            name=descriptor.name,
            description=descriptor.description,
            arguments=target.arguments,
            subcommands=target.subcommands,
            binding=SelfBinding(target=target),
            defaults=descriptor.defaults,
            usage_example=descriptor.usage_example,
            help_content=descriptor.help_content,
        )

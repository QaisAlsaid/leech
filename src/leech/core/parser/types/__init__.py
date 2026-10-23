from .types import (
    IntType,
    NumType,
    BoolType,
    RoleType,
    StrType,
    WordType,
    UserType,
    ChannelType,
)
from .attachment import AttachmentType
from .registry import TypesRegistry

TypesRegistry.register(IntType())
TypesRegistry.register(NumType())
TypesRegistry.register(BoolType())
TypesRegistry.register(StrType())
TypesRegistry.register(WordType())
TypesRegistry.register(UserType())
TypesRegistry.register(ChannelType())
TypesRegistry.register(AttachmentType())
TypesRegistry.register(RoleType())

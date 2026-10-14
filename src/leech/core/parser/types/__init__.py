from .types import IntType, NumType, BoolType, StrType, UserType, ChannelType
from .attachment import AttachmentType
from .registry import TypesRegistry

TypesRegistry.register(IntType())
TypesRegistry.register(NumType())
TypesRegistry.register(BoolType())
TypesRegistry.register(StrType())
TypesRegistry.register(UserType())
TypesRegistry.register(ChannelType())
TypesRegistry.register(AttachmentType())

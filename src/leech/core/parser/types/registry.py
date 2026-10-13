from .definition import TypeDefinition, Types
from .error import TypesError


class TypesRegistry:
    registered_types: dict[Types, TypeDefinition] = {}

    @staticmethod
    def register(type: TypeDefinition) -> bool:
        if TypesRegistry.registered_types.get(Types(type.type)) is not None:
            return False
        TypesRegistry.registered_types[Types(type.type)] = type
        return True

    @staticmethod
    def require(typename: Types) -> TypeDefinition:
        tp = TypesRegistry.registered_types.get(typename)
        if tp is None:
            raise TypesError(f"required type {typename} is not registered.")
        return tp

from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import Any


class Types(str, Enum):
    INT = "int"
    NUM = "num"
    BOOL = "bool"
    STR = "str"
    USER = "user"
    CHANNEL = "channel"
    ATTACHMENT = "attachment"


class Cardinality(Enum):
    ONE = auto()
    MANY = auto()


class TypeDefinition(ABC):
    def __init__(
        self,
        type: Types,
        description: str,
        cardinality: Cardinality = Cardinality.ONE,
        usage_example: str = "",
        help_content: str = "",
    ):
        self.type: Types = type
        self.description: str = description
        self.cardinality: Cardinality = cardinality
        self.usage_example: str = usage_example
        self.help_content: str = help_content

    @abstractmethod
    def parse(self, raw: str, arg) -> Any:
        pass

from enum import Enum, auto
from dataclasses import dataclass


class TokenType(Enum):
    RAW = auto()
    STR = auto()
    USER = auto()
    ROLE = auto()
    CHANNEL = auto()
    ASSIGN = auto()
    SKIP = auto()


@dataclass
class Token:
    type: TokenType
    raw: str

# Standard
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Local (only a type hint, so the runtime cycle is avoided)
    from ..a import SomeType


def helper() -> "SomeType":
    return None

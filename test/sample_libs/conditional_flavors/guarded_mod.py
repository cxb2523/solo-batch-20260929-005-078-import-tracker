"""TYPE_CHECKING guarded imports"""

# Standard
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Third Party (conditional)
    import alog
    from yaml import SafeLoader

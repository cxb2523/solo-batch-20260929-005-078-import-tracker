"""Sample library exercising the four conditional-import idioms:

* try/except ImportError fallback
* TYPE_CHECKING guard
* lazy imports inside function bodies
* relative imports (from . import ...)

Conditional third-party deps must land in the conditional column / graph list
but never in the direct set, so that a missing optional dep cannot fake a
cycle.
"""

# Standard
from typing import TYPE_CHECKING

# Local
from . import direct_mod, fallback_mod, guarded_mod, lazy_mod

# Third Party
import yaml

if TYPE_CHECKING:
    # Third Party (conditional: only imported under type checking)
    import alog


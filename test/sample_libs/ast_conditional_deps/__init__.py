"""
Sample library exercising the four conditional/import shapes tracked by the
AST-based import scanner:

  * except ImportError fallback imports
  * TYPE_CHECKING guarded imports
  * function-body deferred imports
  * ``from .`` style relative imports
"""
# Third Party
import yaml

# Local
from . import optional_mod, relative_mod, type_checking_mod

# Local
from .relative_mod import lazy_helper

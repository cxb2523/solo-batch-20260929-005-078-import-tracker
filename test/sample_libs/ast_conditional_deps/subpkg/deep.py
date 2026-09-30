"""Two-dot relative imports resolve to the parent package"""
# Local
from .. import optional_mod
from ..optional_mod import use_optional


def deep_lazy():
    # Local
    from ..deferred_mod import load_heavy

    return load_heavy

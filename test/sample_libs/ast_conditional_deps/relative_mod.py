"""Relative imports of all dot depths, including attribute disambiguation"""
# Local
from . import optional_mod
from .optional_mod import use_optional
from . import type_checking_mod


def lazy_helper():
    # Local
    from .deferred_mod import load_heavy

    return load_heavy

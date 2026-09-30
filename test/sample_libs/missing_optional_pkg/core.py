"""The heavy dep is only used behind an import-error fallback"""
try:
    # Third Party
    import definitely_not_installed_xyz
except ImportError:
    definitely_not_installed_xyz = None


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Third Party
    import also_not_installed_abc


def use_it():
    import also_not_installed_abc

    return also_not_installed_abc

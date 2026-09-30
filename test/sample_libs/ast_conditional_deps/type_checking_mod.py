"""Imports guarded by TYPE_CHECKING"""
# Standard
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    # Third Party
    from alog import AlogFormatterBase
    # Local
    from .optional_mod import use_optional


def build() -> "Dict[str, AlogFormatterBase]":
    return {}

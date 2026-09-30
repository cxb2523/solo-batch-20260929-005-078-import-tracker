"""A module that defers its heavy/optional imports into a function body"""
# Standard
import os


def load_heavy():
    # Third Party
    import yaml

    return yaml.safe_load("{}")


async def load_heavy_async():
    # Third Party
    import alog

    return alog

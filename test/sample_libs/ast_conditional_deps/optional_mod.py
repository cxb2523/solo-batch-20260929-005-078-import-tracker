"""Optional imports behind import-error fallbacks"""
# Standard
import typing

try:
    # Third Party
    import alog
except ImportError:
    # Standard
    import typing as fallback_typing
    alog = None


# try/finally without an import error handler is NOT conditional
try:
    # Third Party
    import yaml
finally:
    HAVE_YAML = True


def use_optional():
    return alog

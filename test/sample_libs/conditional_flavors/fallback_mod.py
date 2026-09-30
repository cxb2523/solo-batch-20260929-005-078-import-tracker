"""try/except ImportError fallback imports"""

# Third Party, direct
import yaml

try:
    # Third Party (conditional): guarded by an ImportError fallback
    import alog
except ImportError:
    alog = None

try:
    # Third Party (conditional): ModuleNotFoundError is an ImportError subclass
    import some_missing_optional_thing
except ModuleNotFoundError:
    some_missing_optional_thing = None

try:
    # This one is NOT conditional: try/finally has no import-catching handler
    import google.protobuf
finally:
    _have_google = True

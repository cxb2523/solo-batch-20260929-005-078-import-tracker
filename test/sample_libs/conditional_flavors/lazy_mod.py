"""Imports deferred into function bodies"""


def load_config():
    # Third Party (conditional: only imported when the function runs)
    import alog

    return alog


def nested_lazy():
    def inner():
        # Third Party (conditional, nested)
        import yaml

        return yaml

    return inner

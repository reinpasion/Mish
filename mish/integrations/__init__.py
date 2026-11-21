__all__ = ["AutomationHub"]


def __getattr__(name):
    if name == "AutomationHub":
        from .scheduler import AutomationHub

        globals()[name] = AutomationHub
        return AutomationHub
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
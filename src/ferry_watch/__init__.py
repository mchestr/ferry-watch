"""Ferry Watch: watch BC Ferries for open reservations, alert, and hold the sailing in a cart."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ferry-watch")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

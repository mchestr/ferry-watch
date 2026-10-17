"""Waitlist: watch reservation systems for openings and alert when one opens (BC Ferries first, with auto-cart)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("waitlist")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

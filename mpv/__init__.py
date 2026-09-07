"""Canonical MPV plugin package."""

from PluginCore.base import Plugin

from .mpv_ipc import MPV

__all__ = [
    "MPV",
    "Mpv",
]


class Mpv(Plugin):
    PLUGIN_NAME = "mpv"
    PLUGIN_VERSION = "2026-09-04.1"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "Play media in mpv with Medeia Lua UI, playlists, and pipeline helper."
    PLUGIN_ALIASES = (".mpv", ".pipe")
    EXPOSE_AS_FILE_PROVIDER = False
    CONFIG_HELP = (
        "Requires mpv on PATH. Use .mpv to control playback; Load URL in the player uses yt-dlp for YouTube.",
    )

    def validate(self) -> bool:
        return True

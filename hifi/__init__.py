"""HiFi is the same Tidal proxy client under a second plugin name."""

from __future__ import annotations

from plugins.tidal import Tidal


class HIFI(Tidal):
    PLUGIN_NAME = "hifi"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "HiFi Tidal proxy (same client as Tidal)."
    TABLE_AUTO_STAGES = {
        "hifi.track": ["download-file"],
    }
    URL = Tidal.URL_DOMAINS

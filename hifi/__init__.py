"""HiFi is the same Tidal proxy client under a second plugin name."""

from __future__ import annotations

from plugins.tidal import Tidal


class HIFI(Tidal):
    PLUGIN_NAME = "hifi"
    TABLE_AUTO_STAGES = {
        "hifi.track": ["download-file"],
    }
    URL = Tidal.URL_DOMAINS

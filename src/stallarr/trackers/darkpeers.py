"""DarkPeers — UNIT3D.

Config (data/config.json):

    "darkpeers": {
      "enabled": true,
      "url": "https://darkpeers.org",
      "api_key": "<Settings -> Security -> API Token>",
      "passkey": "<your RSS key / passkey>"
    }

If the announce host differs from the web host, add:

    "options": {"announce_template": "https://tracker.darkpeers.org/announce/{passkey}"}
"""

from __future__ import annotations

from . import register
from .unit3d import Unit3dPlugin


@register
class DarkPeers(Unit3dPlugin):
    id = "darkpeers"
    name = "DarkPeers"
    default_url = "https://darkpeers.org"
    hosts = ("darkpeers.org",)

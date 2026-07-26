"""OldToons World — UNIT3D.

Config (data/config.json):

    "oldtoons": {
      "enabled": true,
      "url": "https://oldtoons.world",
      "api_key": "<Settings -> Security -> API Token>",
      "passkey": "<your RSS key / passkey>"
    }

`extra_hosts` is the escape hatch if the site announces from a domain not listed
below — no code change needed.
"""

from __future__ import annotations

from . import register
from .unit3d import Unit3dPlugin


@register
class OldToons(Unit3dPlugin):
    id = "oldtoons"
    name = "OldToons World"
    default_url = "https://oldtoons.world"
    hosts = ("oldtoons.world", "oldtoons.org")

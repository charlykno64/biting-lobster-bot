from __future__ import annotations

import hashlib
import platform
import uuid


def get_hardware_id() -> str:
    seed = f"{platform.system()}|{platform.node()}|{uuid.getnode()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:24].upper()

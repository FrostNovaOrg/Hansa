from __future__ import annotations

import json
from pathlib import Path

from . import archive as archive_mod
from .config import Desk
from .packet import parse_packet
from .util import BellhopError, sha256_file, stamp, ts, write_json


def ring(desk: Desk, packet_path: Path, commit: bool = True) -> Path:
    packet_path = Path(packet_path).resolve()
    try:
        packet_path.relative_to(desk.inbox.resolve())
    except ValueError:
        raise BellhopError(f"packet must live under {desk.inbox}: {packet_path}")
    packet = parse_packet(packet_path)
    desk.lane(packet.lane)  # validates the lane exists
    for existing in list(desk.bells_pending.glob("*.bell")):
        data = json.loads(existing.read_text(encoding="utf-8"))
        if data.get("id") == packet.id:
            raise BellhopError(f"a bell for id {packet.id} is already pending: {existing.name}")
    bell = desk.bells_pending / f"{stamp()}-{packet.id}.bell"
    write_json(
        bell,
        {
            "id": packet.id,
            "lane": packet.lane,
            "packet": str(packet_path.relative_to(desk.root)),
            "sha256": sha256_file(packet_path),
            "created_at": ts(),
        },
    )
    # The packet and the bell enter git here, not when the receipt lands. Until they do,
    # the history says what came back but never what was asked for, and a packet edited
    # after the bell was rung leaves no trace of the edit. One commit, so the bell and
    # the packet whose bytes it hashes are never in the history apart.
    if commit:
        archive_mod.archive(desk, [packet_path, bell], f"ring {packet.id} {packet.slug}")
    return bell

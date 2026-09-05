from __future__ import annotations

from pathlib import Path

from .config import Desk
from .packet import parse_packet
from .util import BellhopError, sha256_file, stamp, ts, write_json


def ring(desk: Desk, packet_path: Path) -> Path:
    packet_path = Path(packet_path).resolve()
    try:
        packet_path.relative_to(desk.inbox.resolve())
    except ValueError:
        raise BellhopError(f"packet must live under {desk.inbox}: {packet_path}")
    packet = parse_packet(packet_path)
    desk.lane(packet.lane)  # validates the lane exists
    for existing in list(desk.bells_pending.glob("*.bell")):
        data = __import__("json").loads(existing.read_text(encoding="utf-8"))
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
    return bell

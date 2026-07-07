from __future__ import annotations
import logging
import re
from ecourts_client.vc.models import VCAccess, VCLinkType, VCRoomKey, VCVendor, make_key, normalize_designation

log = logging.getLogger(__name__)

# A compound officer holds >1 role ("Civil Judge (JD) cum JMIC"); a case may be
# listed under just one role, so we index each "cum"-separated part too.
_CUM_RE = re.compile(r"\bcum\b", re.IGNORECASE)


def _access_from_row(row: dict) -> VCAccess | None:
    try:
        vendor = VCVendor(str(row["vendor"]))
        link_type = VCLinkType(str(row.get("link_type", "join_url")))
    except (KeyError, ValueError) as e:
        log.warning("vc_rooms: skipping row %r (%s)", row, e)
        return None
    url = row.get("url")
    return VCAccess(
        vendor=vendor, link_type=link_type,
        url=(str(url) if url else None),
        meeting_id=(str(row["meeting_id"]) if row.get("meeting_id") else None),
        passcode=(str(row["passcode"]) if row.get("passcode") else None),
        requires_intimation=bool(row.get("requires_intimation", False)),
        persistent=bool(row.get("persistent", True)),
        room=(str(row["room"]) if row.get("room") else None),
    )


class CuratedMapProvider:
    """VCLinkProvider backed by an in-memory (VCRoomKey -> VCAccess) map.

    Indexes each row under its (scope, complex, court_no) key AND, when a
    designation is present, a (scope, complex, 'desg:<designation>') alias so
    the indexer can fall back to a judge-designation join when court_no misses.
    """

    def __init__(self, rooms: dict[VCRoomKey, VCAccess]):
        self._rooms = rooms

    def resolve(self, key: VCRoomKey) -> VCAccess | None:
        return self._rooms.get(key)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "CuratedMapProvider":
        rooms: dict[VCRoomKey, VCAccess] = {}
        compound: list[tuple[str, str, VCAccess, str]] = []  # (scope, complex, access, desg)
        # Pass 1: court_no + FULL designation keys (a full designation is the most
        # specific match and must win over any compound-role part indexed later).
        for row in rows:
            if not isinstance(row, dict):
                continue
            access = _access_from_row(row)
            if access is None or not row.get("scope") or not row.get("complex"):
                continue
            scope, complex_ = str(row["scope"]), str(row["complex"])
            if row.get("court_no") is not None:
                rooms[make_key(scope, complex_, str(row["court_no"]))] = access
            desg = row.get("designation")
            if desg:
                sl, cl = scope.strip().lower(), complex_.strip().lower()
                rooms[(sl, cl, "desg:" + normalize_designation(str(desg)))] = access
                if _CUM_RE.search(str(desg)):
                    compound.append((sl, cl, access, str(desg)))
        # Pass 2: index each "cum"-separated role of a compound designation, WITHOUT
        # overwriting any full-designation key from pass 1 (setdefault).
        for sl, cl, access, desg in compound:
            for part in _CUM_RE.split(desg):
                nk = normalize_designation(part)
                if nk:
                    rooms.setdefault((sl, cl, "desg:" + nk), access)
        return cls(rooms)

"""Canary drift CLI.

Usage:
    python -m ecourts_client.canary_check                  # check live vs snapshots
    python -m ecourts_client.canary_check --update <cnr>   # reseed snapshot from live
    python -m ecourts_client.canary_check --seed-fixtures  # reseed all snapshots from
                                                            # the recorded `.flow` fixtures
                                                            # under tests/canaries/fixtures
                                                            # (offline -- no network)
    python -m ecourts_client.canary_check --coverage       # report filled vs open
                                                            # coverage targets (offline)

Exit codes:
    0  all canaries OK (or seed succeeded, or coverage report rendered)
    1  drift detected on at least one canary
    2  unexpected error (parser raised, schema changed, network down, etc.)

`drift` here means: a non-mutable field of the parsed Case differs from the saved
snapshot. See tests/canaries/mutable_fields.py for the allow-list.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ecourts_client import Case, fetch_case
from ecourts_client.errors import ECourtsError
from ecourts_client.models import StateRef
from ecourts_client.parsers.case_history import parse_case_history


_PKG_ROOT = Path(__file__).resolve().parent
_CANARIES_PATH = _PKG_ROOT / "canary_cnrs.json"
_SNAPSHOTS_DIR = _PKG_ROOT / "snapshots"
_FIXTURES_DIR = _PKG_ROOT / "fixtures"


def _load_canaries() -> list[dict[str, Any]]:
    return json.loads(_CANARIES_PATH.read_text(encoding="utf-8"))["canaries"]


def _case_lookup_canaries() -> list[dict[str, Any]]:
    """Only entries with type=='case_lookup' have a harness today. Filter so
    ingress canaries (qr_decode, party_search, case_number_search) added to
    `canaries[]` don't break seed/check until their seeders exist.
    """
    return [c for c in _load_canaries() if c.get("type", "case_lookup") == "case_lookup"]


def _load_targets() -> list[dict[str, Any]]:
    """Coverage targets are optional; return [] for back-compat with v1 files."""
    data = json.loads(_CANARIES_PATH.read_text(encoding="utf-8"))
    return data.get("coverage_targets", [])


def _snapshot_path(cnr: str) -> Path:
    return _SNAPSHOTS_DIR / f"{cnr}.json"


def _fixture_path(cnr: str) -> Path:
    return _FIXTURES_DIR / f"{cnr}_resolve.json"


def _drop_mutable(case_dict: dict[str, Any]) -> dict[str, Any]:
    # Local copy of mutable_fields to avoid pulling tests/ into runtime imports.
    mutable = {"next_hearing_date", "stage", "history", "orders"}
    return {k: v for k, v in case_dict.items() if k not in mutable}


def _write_snapshot(cnr: str, case: Case) -> None:
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot_path(cnr).write_text(case.to_json(), encoding="utf-8")


def cmd_update(cnr: str) -> int:
    """Live-fetch one canary CNR and overwrite its snapshot."""
    try:
        case = fetch_case(cnr)
    except ECourtsError as e:
        print(f"ERROR: {cnr}: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    _write_snapshot(cnr, case)
    print(f"OK: snapshot updated for {cnr}")
    return 0


def cmd_seed_fixtures() -> int:
    """Re-seed every case_lookup canary snapshot by re-parsing its `_resolve.json` fixture.

    Offline: no network. Useful when the parser improves and the snapshots need to
    catch up to the new output shape, or for first-time seeding. Other canary types
    (qr_decode, party_search, case_number_search) are skipped here -- their seeders
    will live in dedicated commands as their fixtures land.
    """
    canaries = _case_lookup_canaries()
    failures: list[str] = []
    for c in canaries:
        cnr = c["cnr"]
        fix_path = _fixture_path(cnr)
        if not fix_path.exists():
            failures.append(f"{cnr}: missing fixture {fix_path}")
            continue
        fixture = json.loads(fix_path.read_text(encoding="utf-8"))
        case = parse_case_history(fixture["history"], cnr=cnr)
        _write_snapshot(cnr, case)
        print(f"OK: seeded {cnr} from {fix_path.name}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 2
    return 0


def cmd_check() -> int:
    """Live-fetch every case_lookup canary CNR and compare against snapshots
    (mutable fields excluded).

    Returns 0 if every canary's identity fields match its snapshot, 1 on drift,
    2 on transport / parser errors. Other canary types are not exercised here.
    """
    canaries = _case_lookup_canaries()
    drift: list[str] = []
    errors: list[str] = []

    for c in canaries:
        cnr = c["cnr"]
        snap_path = _snapshot_path(cnr)
        if not snap_path.exists():
            errors.append(f"{cnr}: no snapshot at {snap_path} (run --seed-fixtures or --update {cnr})")
            continue
        expected = _drop_mutable(json.loads(snap_path.read_text(encoding="utf-8")))
        try:
            actual_case = fetch_case(cnr)
        except ECourtsError as e:
            errors.append(f"{cnr}: {type(e).__name__}: {e}")
            continue
        actual = _drop_mutable(json.loads(actual_case.to_json()))
        if actual != expected:
            drift.append(cnr)
            for k in sorted(set(expected) | set(actual)):
                if expected.get(k) != actual.get(k):
                    print(f"DRIFT {cnr}.{k}:\n  expected: {expected.get(k)!r}\n  actual:   {actual.get(k)!r}", file=sys.stderr)
        else:
            print(f"OK:    {cnr}")

    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if errors:
        return 2
    if drift:
        print(f"\nDrift on {len(drift)} canary CNR(s): {', '.join(drift)}", file=sys.stderr)
        print("If the change is legitimate (e.g. parser fix, NIC clerical correction), reseed via:", file=sys.stderr)
        print("  python -m ecourts_client.canary_check --update <cnr>", file=sys.stderr)
        return 1
    return 0


def verify_dropdown_states_response(
    states: list[StateRef],
    checks: dict[str, Any],
) -> None:
    """Assert a list_states response satisfies a canary's checks block.

    Pure function -- takes already-fetched StateRefs and a `checks` dict (the
    `checks` key of a canary entry). Raises AssertionError on failure with a
    message that explains exactly what drifted, so the failing canary report
    is actionable without further investigation.

    Why this is a separate function: pulling the assertions out of the live
    pytest harness lets us unit-test the verifier on fixed fake data,
    catching typos / wrong defaults / changed key names at commit time
    instead of waiting for the 04:00 UTC scheduled canary run.

    Args:
        states: Parsed StateRef list from `HighCourtClient.list_states()`.
        checks: The canary's `checks` block, with optional keys:
            - min_state_count (int, default 20): minimum number of StateRefs.
              eCourts has ~36 states/UTs; <20 strongly suggests parser
              returned [] on a schema drift.
            - expected_national_codes (list[str], default ["DL","MH","BR"]):
              codes that MUST appear in the live response. Use direct CNR
              prefixes (not alias targets) -- the verifier does NOT apply
              the state_code_map alias map (keeps this module free of bot
              imports). To verify Telangana presence you'd put "HB" here,
              not "TG".

    Raises:
        AssertionError: with a diagnostic message naming the drifted check.
    """
    min_count = checks.get("min_state_count", 20)
    expected_codes = set(checks.get("expected_national_codes", ["DL", "MH", "BR"]))

    assert len(states) >= min_count, (
        f"list_states returned {len(states)} StateRef rows, expected >= {min_count}. "
        "This usually signals NIC schema drift -- parse_states caught a "
        "SchemaChanged and returned an empty list. Check the parser logs."
    )

    live_codes = {(s.national_code or "").upper() for s in states}
    missing = expected_codes - live_codes
    assert not missing, (
        f"list_states response missing expected national_codes: {sorted(missing)}. "
        f"Live codes observed: {sorted(live_codes)}. "
        "Either NIC removed/renamed a state, or the canary's "
        "expected_national_codes need updating."
    )


def _load_geographic_targets() -> dict[str, Any]:
    data = json.loads(_CANARIES_PATH.read_text(encoding="utf-8"))
    return data.get("geographic_targets", {})


def cmd_coverage() -> int:
    """Report which canary slots are filled and which targets remain open.

    Offline -- reads only `canary_cnrs.json`. Always exits 0; coverage is
    informational, not a gate. Use `cmd_check` (the default) to fail on drift.

    Groups output by canary type (ingress surface) and target category, and
    prints a separate geographic-coverage block so state-diversity gaps are
    surfaced alongside endpoint-diversity gaps.
    """
    canaries = _load_canaries()
    targets = _load_targets()
    geo = _load_geographic_targets()

    filled = len(canaries)
    total = filled + len(targets)
    print(f"Canary corpus: {filled} filled / {total} planned")
    print()

    # Filled canaries grouped by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for c in canaries:
        by_type.setdefault(c.get("type", "case_lookup"), []).append(c)
    print("Filled canaries:")
    for t in sorted(by_type):
        print(f"  [{t}]")
        for c in by_type[t]:
            dims = c.get("dimensions", {})
            dim_str = " ".join(f"{k}={v}" for k, v in dims.items())
            ident = c.get("cnr") or c.get("target_id") or "<unknown>"
            print(f"    [x] {ident:20} {c.get('scope', '?'):10} {dim_str}")
    print()

    # Targets grouped by category, then priority
    if targets:
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for tgt in targets:
            by_cat.setdefault(tgt.get("category", "uncategorized"), []).append(tgt)
        for cat in sorted(by_cat):
            print(f"Open coverage targets [{cat}]:")
            for tgt in sorted(by_cat[cat], key=lambda x: (x.get("priority", "P9"), x.get("target_id", ""))):
                prio = tgt.get("priority", "P?")
                tid = tgt.get("target_id", "<unnamed>")
                desc = tgt.get("description", "")
                scope = tgt.get("scope", "?")
                ttype = tgt.get("type", "?")
                print(f"  [ ] {prio}  {tid}  ({ttype}/{scope})")
                print(f"          {desc}")
            print()

    # Geographic coverage
    if geo:
        covered = geo.get("currently_covered", [])
        missing = geo.get("high_priority_missing", [])
        states_in_canaries = sorted({c.get("dimensions", {}).get("state") for c in canaries if c.get("dimensions", {}).get("state")})
        print("Geographic coverage:")
        print(f"  Covered now:           {' '.join(states_in_canaries) or '(none)'}")
        print(f"  Declared as covered:   {' '.join(covered)}")
        print(f"  High-priority missing: {' '.join(missing)}")
        rationale = geo.get("rationale")
        if rationale:
            print(f"  Rationale: {rationale}")
        print()

    print("To fill an open target:")
    print("  1. Capture the relevant fixture into tests/canaries/fixtures/")
    print("     - case_lookup:       <CNR>_resolve.json")
    print("     - qr_decode:         qr_<scope>_<state>_<id>.png")
    print("     - party_search:      party_search_<scope>_<state>_<id>.json")
    print("     - case_number_search: case_no_search_<scope>_<state>_<id>.json")
    print("  2. Append an entry to canary_cnrs.json `canaries` array with the right `type`/`scope` fields")
    print("  3. python -m ecourts_client.canary_check --seed-fixtures  (case_lookup) or run the type-specific seeder")
    print("  4. Commit fixture + snapshot + canary_cnrs.json change")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="canary_check", description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--update", metavar="CNR", help="Reseed one snapshot from a live fetch")
    g.add_argument("--seed-fixtures", action="store_true", help="Reseed all snapshots from recorded fixtures (offline)")
    g.add_argument("--coverage", action="store_true", help="Report filled vs open coverage targets (offline)")
    args = p.parse_args(argv)

    if args.update:
        return cmd_update(args.update)
    if args.seed_fixtures:
        return cmd_seed_fixtures()
    if args.coverage:
        return cmd_coverage()
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())

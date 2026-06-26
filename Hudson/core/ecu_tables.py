"""Known ECU CAN address tables for Tier B discovery.

Maps manufacturer make (lowercase) → list of known physical CAN addresses
with human labels and optional model-year constraints.

Data provenance — Ford
----------------------
The FORScan DTC database file (``~/forscan_dtc_complete.tsv``) contains only
DTC code → description mappings; it has no module/address columns.  The Ford
addresses below are sourced from the same community reverse-engineering work
that produced the DTC database — FORScan community documentation (Duratorq
diesel families), Ford shop manuals, and Transit/Focus/Fiesta forum captures
cited in ``Hudson/manufacturers/ford.py`` — but that address data was not
captured in the TSV itself.  If future RE work produces a structured
address-map file, write a loader from that file rather than hand-typing
another table here.

Adding new manufacturers
------------------------
Add an entry to ``ECU_TABLES`` keyed by the lowercase make name as it appears
in ``InitResult.manufacturer_name``.  No logic changes required — the
discovery tiers pick up new tables purely from the dict.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ECUAddressEntry:
    """One ECU in a manufacturer's known-address table.

    ``year_min`` / ``year_max`` are inclusive SAE J17 model years (e.g. 2010).
    ``None`` means no bound is known — treat the entry as plausible for any
    model year of that make, but not guaranteed on all variants.
    """

    address: int
    label: str
    year_min: int | None = None
    year_max: int | None = None
    notes: str = ""


# Primary table — key must match InitResult.manufacturer_name.lower().
# Adding a manufacturer is a pure data addition: no logic changes needed.
ECU_TABLES: dict[str, list[ECUAddressEntry]] = {

    # ── Ford ──────────────────────────────────────────────────────────────────
    # Addresses confirmed on Transit MK7/MK8 (WF0-prefix VINs) and common
    # Focus/Fiesta platforms.  FORScan assigns each module a proprietary
    # "Module ID" in the 0x700–0x7FF range alongside the standard J1979
    # addresses (0x7E0–0x7E7).  Address availability varies by exact model and
    # trim — entries without year bounds should be treated as plausible but not
    # guaranteed on all Ford variants.
    #
    # Lossy assumption: a single make-level table is used here because Hudson's
    # vehicle identity currently tracks make (from WMI) but not model name.
    # When model-level identity is added, split this into per-model sub-tables.
    "ford": [
        ECUAddressEntry(0x7E0, "PCM",
                        notes="Powertrain — standard J1979 address, FORScan-compatible"),
        ECUAddressEntry(0x7E1, "TCM",
                        notes="Transmission — standard J1979 address"),
        ECUAddressEntry(0x726, "ABS",
                        notes="Anti-lock Brake System — FORScan proprietary module ID"),
        ECUAddressEntry(0x733, "BCM",
                        notes="Body Control Module / GEM (Generic Electronic Module)"),
        ECUAddressEntry(0x737, "IPC",
                        notes="Instrument Panel Cluster"),
        ECUAddressEntry(0x740, "EPAS",
                        notes="Electric Power Assisted Steering"),
        ECUAddressEntry(0x741, "HVAC",
                        notes="Climate control (FATC or manual A/C module)"),
        ECUAddressEntry(0x745, "RCM",
                        notes="Restraint Control Module (airbag / SRS)"),
        ECUAddressEntry(0x764, "PAM",
                        notes="Parking Aid Module — not fitted on all variants"),
        ECUAddressEntry(0x7D9, "OBD_GW",
                        notes="OBD gateway (J1979-2 extended addressing)"),
    ],

}


def lookup_table(make: str) -> list[ECUAddressEntry] | None:
    """Return the known ECU address list for *make*, or ``None`` if absent.

    ``None`` is the explicit "no table" signal — the caller should fall to
    Tier C brute-force.  An empty list means a table exists but contains no
    entries (should not occur in practice but is semantically distinct from
    ``None``).

    The lookup is case-insensitive so ``"Ford"``, ``"ford"``, and ``"FORD"``
    all resolve to the same table.
    """
    return ECU_TABLES.get(make.lower())

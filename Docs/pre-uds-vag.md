# KWP2000 VAG DTC Enumeration — Reference

## Overview

On pre-UDS VAG vehicles, DTCs are stored per-ECU. There is no broadcast equivalent
of OBD2 mode 03 — each ECU address must be polled individually via KWP2000.

---

## Protocol: KWP2000 Fast Init (ISO 14230-2)

Before sending any service request to an ECU, a KWP2000 session must be established
via fast init. The ELM327 handles this with `AT KW` / `AT FI` but the sequence is:

1. **25ms bus low** (break signal)
2. **25ms bus high**
3. Send **StartCommunication request**: `81 [target] [source] C1 57 8F [checksum]`
4. ECU responds with **KB1/KB2** keyword bytes + sync byte
5. Send **StartDiagnosticSession**: `10 89` (or `10 85` for extended)
6. ECU ACKs with `50 89`

On ELM327, set protocol explicitly before attempting:
```
AT SP 4   # KWP2000 slow (ISO 14230, 5-baud init)
AT SP 5   # KWP2000 fast (ISO 14230, fast init) — prefer this for VAG
AT SH [target_addr]
AT KW     # keyword protocol negotiation
```

Reset adapter state fully between ECU address attempts — a timeout or NAK from one
address can leave the ELM327 in an undefined state.

---

## DTC Services

### Read DTCs — `18 00 FF 00`
Request all stored DTCs regardless of status.

```
Request:  18 00 FF 00
Response: 58 [count] [code_hi] [code_lo] [status] ... (3 bytes per DTC)
```

Status byte flags:
| Bit | Meaning |
|-----|---------|
| 0   | DTC active/current |
| 1   | DTC stored (occurred but not current) |
| 2   | Intermittent fault |
| 3   | Not used (VAG-specific varies) |

### Read Active DTCs only — `18 02 FF 00`
Same response format, filtered to current/active faults only.

```
Request:  18 02 FF 00
Response: 58 [count] [code_hi] [code_lo] [status] ...
```

### Clear DTCs — `14 FF 00`
Clears all DTCs for the addressed ECU. Equivalent of OBD2 mode 04 but per-ECU.

```
Request:  14 FF 00
Response: 54 (positive ACK)
```

---

## DTC Code Format

VAG KWP2000 DTCs are **2-byte internal fault numbers**, NOT standard SAE P/C/B/U codes.

Example: `0x01176` → VAG fault 4470 → maps to `P0322 — Engine Speed Sensor Signal`

A lookup table is required to translate VAG internal codes to human-readable descriptions.
Community sources:
- Ross-Tech Wiki (partial): https://wiki.ross-tech.com/wiki/index.php/VAG_Fault_Codes
- VCDS label files (.lbl) contain per-ECU fault descriptions
- OpenDiag / FreeSSM community tables

---

## ECU Address Map (VAG)

Priority addresses to scan first:

| Address | ECU |
|---------|-----|
| `0x01`  | Engine (ECM) |
| `0x02`  | Transmission (TCM) |
| `0x03`  | ABS / ESP |
| `0x08`  | HVAC / Climate |
| `0x09`  | Central electrics |
| `0x15`  | Airbag / SRS |
| `0x17`  | Instrument cluster |
| `0x19`  | Gateway (CAN gateway) |
| `0x25`  | Immobiliser |
| `0x37`  | Navigation |
| `0x46`  | Central convenience |
| `0x56`  | Radio |
| `0x76`  | Parking assist |

Full scan range: `0x01`–`0x7F`. Expect ~30s for a full scan over K-line.

---

## Enumeration Strategy

```python
PRIORITY_ADDRS = [0x01, 0x02, 0x03, 0x08, 0x09, 0x15, 0x17, 0x19, 0x25, 0x46]
FULL_SCAN_RANGE = range(0x01, 0x80)

for addr in PRIORITY_ADDRS + [a for a in FULL_SCAN_RANGE if a not in PRIORITY_ADDRS]:
    reset_elm327_state()
    if kwp2000_fast_init(addr):          # establish session
        dtcs = kwp2000_read_dtcs(addr)   # 18 00 FF 00
        if dtcs:
            log(addr, dtcs)
```

Run priority addresses synchronously first, then full scan as a background task.

---

## ELM327 State Hygiene

**Critical:** The ELM327 maintains state between commands. After any failed init,
timeout, or unexpected response:

1. Send `AT D` to restore defaults
2. Re-set protocol (`AT SP 5`)
3. Re-set header (`AT SH [addr]`)
4. Only then attempt the next ECU address

Failure to reset between addresses is the primary cause of cascading failures where
a non-responsive ECU poisons subsequent attempts.

---

## Relationship to UDS

On newer VAG vehicles (post ~2008 depending on model), ECUs speak UDS (ISO 14229)
over CAN rather than KWP2000 over K-line. The Hudson UDS discovery path handles these.

KWP2000 and UDS should be treated as separate, non-overlapping code paths.
Detection heuristic: attempt KWP2000 fast init on `0x01` — if it times out entirely,
fall back to assuming UDS/CAN and trigger the UDS discovery path instead.
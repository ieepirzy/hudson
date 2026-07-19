# hudson

Async OBD2 TUI scanner with manufacturer-extended PID support.

## Status

Pre-alpha. Tested against real hardware — ECU data reads are working. DTC
reading has not been confirmed against real hardware yet.

## Architecture

```text
Hudson/
├── core/                # ELM327, J1979 PIDs, ISO-TP, DTC encoding, UDS/KWP
├── manufacturers/       # VIN-dispatched extended PID + DTC tables
├── tui/                 # Textual UI (dashboard, DTC screen, widgets)
└── analysis/            # Future: anomaly detector, thermo analysis
```

The connection layer wraps the synchronous `python-obd` library in
`asyncio.to_thread()` so it integrates cleanly with Textual's async event loop.
A SocketCAN backend (`--vcan`) bypasses ELM327 entirely for direct CAN access.

## Supported manufacturers

VIN-based detection dispatches to manufacturer modules for extended Mode 22 PIDs
and manufacturer-specific DTC descriptions:

| Manufacturer | WMI examples | Strategy | Notes |
| --- | --- | --- | --- |
| VW / Audi / SEAT / Škoda / Porsche / Bentley | WVW, WAU, WV1, WP0, … | UDS | Post-2008 VAG |
| Toyota / Lexus | JT1–JT8, 4T1, 5TD, … | probe | 2007+ CAN |
| Volvo | YV1–YV4, 4V1–4V6 | KWP | ISO 14230 |
| Ford / Lincoln | WF0, 1FA–1FT, 2FA, 3FA, … | UDS | Incl. Transit Duratorq diesel |
| BMW / MINI | WBA, WBS, WBW, WBY, WMW | UDS | DTC lookup only; extended Mode 22 PIDs pending hardware capture |
| Mercedes-Benz / smart | WDB, WDC, WDD, WDF, WME, WMX, VSA | UDS | DTC lookup only; extended Mode 22 PIDs pending hardware capture |
| Generic fallback | any other VIN | probe | Mode 01 only |

Ford support includes a full Duratorq diesel PID set (DPF pressure / soot /
regen distances, EGT sensors, high-res MAP, lambda, EGR duty cycle) sourced
from FORScan community captures on the 2.0/2.2 TDCi family.

## Quick start

```bash
# Install:
pip install -e .[dev]

# ELM327 over Bluetooth — bind to serial first:
sudo rfcomm bind /dev/rfcomm0 <ELM327_MAC> 1
hudson --port /dev/rfcomm0

# ELM327 on USB:
hudson --port /dev/ttyUSB0

# Force a specific OBD2 protocol (e.g. ISO 15765-4 CAN 11-bit/500 kbaud):
hudson --port /dev/rfcomm0 --protocol 6

# Skip battery voltage check (some BT adapters report bad voltage):
hudson --port /dev/rfcomm0 --no-voltage-check

# SocketCAN directly (no ELM327, Linux only):
hudson --vcan vcan0

# Synthetic data — no hardware required:
hudson --mock

# Verbose logging to stderr:
hudson --debug
```

## Tests

```bash
pytest
```

Unit tests (`test_dtc.py`, `test_isotp.py`, `test_poller.py`, etc.) require no
hardware and run instantly.

Integration tests (`test_vcan_*.py`) require a `vcan0` interface. On Linux:

```bash
sudo modprobe vcan
sudo ip link add vcan0 type vcan
sudo ip link set vcan0 up
pytest tests/test_vcan_vag.py tests/test_vcan_ford.py -v
```

## License

MIT.

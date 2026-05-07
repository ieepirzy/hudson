# hudson

Async OBD2 TUI scanner with manufacturer-extended PID support.

Built originally for pre-purchase due diligence on a VW T5 van for [Muutto365](https://muutto365.fi).

## Status

Pre-alpha. Core protocol logic (DTC encode/decode, ISO-TP reassembly) is implemented and tested. TUI scaffold runs but has not been validated against real hardware yet.

## Architecture

```
hudson/
├── core/                # ELM327, J1979 PIDs, ISO-TP, DTC encoding
├── manufacturers/       # VIN-dispatched extended PID + DTC tables
├── tui/                 # Textual UI (dashboard, DTC screen, widgets)
└── analysis/            # Future: anomaly detector, thermo analysis
```

The connection layer wraps the synchronous `python-obd` library in `asyncio.to_thread()` so it integrates cleanly with Textual's async event loop.

For the protocol details and design choices, see the OBD2 protocol primer in the Obsidian vault.

## Quick start

```bash
# 1. Bind your BT ELM327 to a serial port (one-time per session):
sudo rfcomm bind /dev/rfcomm0 <ELM327_MAC> 1

# 2. Install:
pip install -e .[dev]

# 3. Run:
hudson --port /dev/rfcomm0
```

For development with the Textual devtools console:

```bash
textual console &
textual run --dev hudson.tui.app:HudsonApp
```

## Tests

```bash
pytest
```

The protocol-layer tests (`test_dtc.py`, `test_isotp.py`) require no hardware and run instantly.

## License

MIT.

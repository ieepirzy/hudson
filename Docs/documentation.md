# Hudson — Technical Documentation

Hudson is an async OBD2 diagnostic TUI (Terminal User Interface) for Linux. It connects to a vehicle via an ELM327-based adapter, runs a multi-step initialization sequence, then displays live sensor data, diagnostic trouble codes, and raw ECU discovery results in a terminal dashboard. It supports manufacturer-specific extended PIDs for VAG, Toyota, and Volvo vehicles, with a generic fallback for everything else.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Entry Points and Startup](#2-entry-points-and-startup)
3. [Core Modules](#3-core-modules)
4. [Manufacturer Modules](#4-manufacturer-modules)
5. [TUI Modules](#5-tui-modules)
6. [Test Suite and Fixtures](#6-test-suite-and-fixtures)
7. [Data Flow end-to-end](#7-data-flow-end-to-end)
8. [Configuration and Dependencies](#8-configuration-and-dependencies)

---

## 1. Architecture Overview

```
cli.py
  └─ HudsonApp (app.py)
       ├─ SplashScreen (screens/splash.py)
       │    └─ run_init() (core/init.py)
       │         ├─ ObdConnection (core/connection.py)
       │         ├─ resolve_vin_chain() (core/vin.py)
       │         ├─ select_decoder() (manufacturers/registry.py)
       │         │    └─ vw_audi / toyota / volvo / generic
       │         ├─ UdsDiscovery (core/uds.py)
       │         │    └─ EcuCache (core/ecu_cache.py)
       │         └─ KwpSession (core/kwp2000.py)
       └─ MainScreen (screens/main.py)
            ├─ DashboardPane (panes/dashboard.py)
            │    └─ Poller (core/poller.py)
            ├─ DtcPane (panes/dtcs.py)
            │    ├─ decode_dtc_list() (core/dtc.py)
            │    └─ TelemetryClient.record_dtcs()
            ├─ LogPane (panes/log.py)
            └─ VehiclePane (panes/vehicle.py)

TelemetryClient (core/telemetry.py)
  ├─ record_reading()  ← Poller on_reading callback
  └─ record_dtcs()    ← DtcPane after each scan
```

**Key design principles:**

- **Single async/thread boundary.** `python-obd` is a blocking library. All calls cross into a thread via `asyncio.to_thread()`. A single `asyncio.Lock` in `ObdConnection` serializes every serial I/O operation to prevent race conditions.
- **Non-fatal init steps.** Every step in the init sequence emits progress events. Failures are reported as error annotations, not raised exceptions, so the TUI can show exactly which step failed while continuing with the rest.
- **Manufacturer dispatch at runtime.** After VIN is known, the 3-character WMI prefix selects a manufacturer module. The module's constants and functions are used throughout the session for DTC lookup, KWP block definitions, and UDS identifier decoding.
- **Tiered polling.** Different PIDs are polled at different rates (RPM at 10 Hz, coolant at 1 Hz, fuel trim at 0.2 Hz) to stay within ELM327 UART bandwidth.
- **Discovery resumption.** UDS identifier sweeps are checkpointed to SQLite. An interrupted scan resumes from the last confirmed identifier rather than restarting from zero.

---

## 2. Entry Points and Startup

### `Hudson/cli.py`

The command-line entry point. Parses arguments, configures logging, creates the connection object, and runs the Textual app.

**Arguments:**

| Flag | Default | Description |
|---|---|---|
| `--port` | auto | Serial port, e.g. `/dev/rfcomm0` for Bluetooth |
| `--baudrate` | auto | Serial speed |
| `--protocol` | auto | ELM327 protocol ID (`6` = ISO 15765-4 CAN 11-bit/500k) |
| `--no-voltage-check` | off | Skip ELM327 voltage sanity check (useful for some BT adapters) |
| `--mock` | off | Use `FakeConnection` — no hardware required |
| `--debug` | off | Mirror log output to stderr in addition to the log file |
| `--telemetry` | off | POST telemetry to `api.muutto365.fi`. Requires `HUDSON_TELEMETRY_TOKEN` env var |

**Logging setup** (`_configure_logging`):

Always opens a `RotatingFileHandler` at `logs/hudson.log` (1 MB max, 3 backups). With `--debug`, a second `StreamHandler` on stderr is added. The root logger is set to `DEBUG` so all levels reach the file.

**`_amain()` flow:**

1. Build `FakeConnection` (mock) or `ObdConnection` (real).
2. If `--telemetry`, read `HUDSON_TELEMETRY_TOKEN` from environment; abort with an error if not set; construct `TelemetryClient`.
3. Instantiate and run `HudsonApp`.
4. In `finally`: call `telemetry.stop()` (flushes buffered readings, posts `session_end`), then `connection.close()`.

### `Hudson/__main__.py`

Allows `python -m Hudson` as an alternative to the `hudson` console script.

### `Hudson/tui/app.py` — `HudsonApp`

Minimal `App` subclass. `compose()` returns an empty iterator (no static widgets). `on_mount()` runs `_init_flow()` as an exclusive Textual worker.

**`_init_flow()`:**

1. `push_screen_wait(SplashScreen)` — blocks until init completes or fails.
2. If `TelemetryClient` is present, call `await telemetry.start(init_result)` — posts `session_start` and starts the background batch loop.
3. `push_screen(MainScreen)` — hands off the init result and telemetry client to the main interface.

---

## 3. Core Modules

### `Hudson/core/connection.py`

**`ConnectionConfig`** — plain dataclass for connection parameters.

**`ObdConnection`** — async wrapper around the blocking `obd.OBD` object.

All I/O methods acquire a single `asyncio.Lock` before entering the thread. This prevents two async coroutines from concurrently driving the serial port.

| Method | What it does |
|---|---|
| `connect()` | Open `obd.OBD` in a thread, log adapter state (ATDP + ATRV) |
| `close()` | Disconnect in a thread |
| `query(cmd, force)` | Standard J1979 query via python-obd |
| `query_uds(service, identifier)` | Encode and send a UDS 0x22 (ReadDataByIdentifier) request |
| `query_enhanced_local(local_id)` | Mode 0x21 — Toyota/Volvo proprietary local identifier |
| `query_kwp_service(service, payload)` | Raw KWP2000 service over K-line |
| `send_at(cmd)` | Raw ELM327 AT command; returns response string; logs warnings on failure |
| `send_tester_present()` | UDS 0x3E keepalive to suppress ECU session timeout |
| `_log_adapter_state()` | Reads ATDP and ATRV after connect, warns if voltage is unexpected |

`protocol_name` is a read-only property that returns the current protocol string or `None` if not connected.

---

### `Hudson/core/init.py`

The init sequence state machine. Called once from `SplashScreen` and runs to completion or failure.

**`InitStep`** enum — ordered steps:

```
CONNECT → PROTOCOL → VIN → MANUFACTURER → ECU_VERSION →
UDS_DISCOVERY → KWP_SESSION → SUPPORTED_PIDS → READY
```

**`InitEvent`** — progress message for a step. Fields: `step`, `detail` (display string), `error` (optional), `done` (bool), `progress` (0–100 for UDS_DISCOVERY).

**`InitResult`** — aggregate state available to the rest of the app:
- `vin: str | None`
- `manufacturer_name: str`
- `manufacturer_module` — the loaded Python module, or `None`
- `supported_commands: list[OBDCommand]`
- `protocol_name: str | None`
- `uds_discovery: UdsDiscovery | None`

**`run_init(connection, events)`** — async generator. Yields `InitEvent` objects, then returns `InitResult`. Each step is wrapped in its own try/except so one failure does not abort subsequent steps.

Step details:

1. **CONNECT** — calls `connection.connect()`.
2. **PROTOCOL** — reads `connection.protocol_name`.
3. **VIN** — calls `resolve_vin_chain()`. Reports "unavailable" if all protocols fail.
4. **MANUFACTURER** — calls `select_decoder(vin)`, imports the module, reads `name`.
5. **ECU_VERSION** — if DISCOVERY_STRATEGY is `"uds"`, probe UDS 0xF189 (Software Version); skip for `"probe"` strategy until confirmed responding.
6. **UDS_DISCOVERY** — run `UdsDiscovery.run_priority1(on_progress=...)` to probe standard identifier ranges; stream progress events.
7. **KWP_SESSION** — if manufacturer module has `kwp_blocks` AND protocol is K-line (not CAN), attempt a KWP2000 diagnostic session.
8. **SUPPORTED_PIDS** — query `obd.commands.PIDS_A/B/C/D` to enumerate what the ECU supports; fall back to all commands if ECU does not respond.
9. **READY** — emit final event.

**KWP CAN guard** — before starting a KWP session, the protocol name is checked for K-line keywords (`9141`, `14230`, `kwp`). If none match, the session is suppressed with a warning log and a `blocked` status event. This prevents forcing K-line framing on CAN-only vehicles.

---

### `Hudson/core/vin.py`

**`resolve_vin_chain(connection)`** — tries five VIN retrieval methods in order, returning on first success.

| Step | Protocol | ELM327 setup |
|---|---|---|
| 1 | OBD mode 09 PID 02 (broadcast) | Default — no AT changes |
| 2 | UDS 0x22 0xF190 to gateway (0x7D9) | ATSH 7D9 |
| 3 | KWP 0x1A param 0x90 to instrument cluster (0x17) | ATSP3, ATH1, ATSH 8017F1 |
| 4 | KWP 0x1A param 0x90 to gateway (0x19) | ATSP3, ATH1, ATSH 8019F1 |
| 5 | KWP 0x1A param 0x86 to cluster (0x17) — older VAG | ATSP3, ATH1, ATSH 8017F1 |

ELM327 hygiene: K-line steps always run `ATD + ATSP0` in a `finally` block to restore the adapter state so subsequent CAN queries work correctly. Each step also opens with `ATD` to clear any stale state from a prior failed step.

**`_parse_vin_value(raw)`** — accepts `bytes` or `str`, strips non-printable characters, rejects if not exactly 17 alphanumeric characters, returns the canonical uppercase string.

**`decode_model_year(vin)`** — decodes the 10th position per SAE J17. Returns `int | None`. Returns `None` for ambiguous letters (T, V, W, X, Y appear in both encoding cycles).

---

### `Hudson/core/dtc.py`

**`DTC`** — immutable named tuple: `code` (str), `raw_a` (int), `raw_b` (int).

Properties:
- `system` → `"Powertrain"` / `"Chassis"` / `"Body"` / `"Network"` based on top 2 bits of `raw_a`
- `is_manufacturer_specific` → True for P1xxx and P3xxx codes

**`decode_dtc(byte_a, byte_b)`** — converts two raw bytes into the standard SAE code string. Bit layout: `[SS GG TTTT TTTT TTTT]` where SS is the system type (00=P, 01=C, 10=B, 11=U), GG is the first digit, and T...T are the remaining 3 hex digits.

**`encode_dtc(code)`** — inverse of `decode_dtc`. Used in tests.

**`decode_dtc_list(payload)`** — parses mode 03, 07, or 0A response payloads (raw bytes after the service byte has been stripped). Skips zero-valued pairs (padding). Returns a list of `DTC` objects.

---

### `Hudson/core/dtc_lookup.py`

A module-level dict `GENERIC_DTC` mapping SAE standard code strings (P0001 through P3FFF, plus C/B/U codes) to human-readable descriptions. Sourced from python-obd and ISO 15031-6.

**`lookup_description(code, manufacturer_name)`** — returns a description string or `None`. Manufacturer modules can provide their own `lookup_dtc(code)` function which is checked first by the DTC pane, before falling back to this generic database.

---

### `Hudson/core/uds.py`

UDS service 0x22 (ReadDataByIdentifier) identifier discovery.

**`UdsDiscovery`** — manages two sweep phases and a cache.

Priority ranges probed in the foreground (priority 1):

| Range | Purpose |
|---|---|
| 0xF100–0xF1FF | Standard vehicle info (VIN, SW versions, part numbers) |
| 0xF400–0xF6FF | VAG extended (engine data, transmission, emissions) |
| 0x0600–0x06FF | Common across several manufacturers |

Priority 2 (background): full 0x0000–0xFFFF sweep.

**`read_ecu_version()`** — reads 0xF189 (ECU Software Version), decodes as ASCII. This doubles as the UDS reachability probe used by `init.py`. A response here gates the full UDS_DISCOVERY phase.

**`run_priority1(on_progress)`** — runs the priority ranges synchronously (from async context). Checks `EcuCache` for a cached result before probing. Calls `on_progress(current, total, identifier, responded)` on each probe. Saves responding identifiers to cache in batches. Sends `TesterPresent` every 4 seconds to keep the diagnostic session alive.

**`run_priority2_background(on_progress)`** — designed to be run as an `asyncio.create_task()`. Full 0x0000–0xFFFF sweep at ≤5 req/sec. Updates `MainScreen`'s `UdsScanStrip` via the progress callback.

**`_probe_identifier(identifier)`** — single 0x22 query with timeout. Returns `(responded: bool, raw_value: bytes | None)`. Non-timeout exceptions are logged at WARNING (not silently swallowed).

Mock mode: when `connection` is a `FakeConnection`, the discovery simulates a ~2 second sweep and returns a predetermined set of responding identifiers without touching the serial port.

---

### `Hudson/core/ecu_cache.py`

SQLite-backed persistence at `~/.hudson/ecu_cache.db`.

**Tables:**

- `ecu_versions` — keyed by (vin_prefix, version_string). Tracks whether priority1 and priority2 sweeps completed.
- `discovered_identifiers` — (ecu_id, identifier, responded, raw_value). One row per probed identifier per ECU.
- `discovery_progress` — (ecu_id, last_identifier, phase). Enables resuming an interrupted sweep.

**`save_identifiers_batch(ecu_id, results)`** — bulk INSERT for efficiency; called every N probes during a sweep.

**`get_discovered_identifiers(ecu_id)`** — retrieve the cached probe results for a given ECU fingerprint. Returns an empty dict if no cache entry exists.

**`mark_priority1_complete(ecu_id)`** / **`mark_priority2_complete(ecu_id)`** — set completion flags.

---

### `Hudson/core/kwp2000.py`

KWP2000 (ISO 14230-4) diagnostic session over K-line.

**`KwpField`** — named field definition: offset into the response payload, byte count, unit string, decode function.

**`KwpBlock`** — definition of a KWP local identifier block: `block_id` (service 0x21 local ID), `name`, tuple of `KwpField`.

**`KwpSession`** — lifecycle wrapper for a K-line diagnostic session.

| Method | Description |
|---|---|
| `start_diagnostic_session()` | Send ATSP3, ATSH for the target ECU, issue service 0x10 (StartDiagnosticSession) |
| `query_block(block_id)` | Issue service 0x21 (ReadDataByLocalIdentifier), return raw bytes |
| `parse_block(defn, data)` | Apply `KwpField` definitions to raw bytes, return dict of {field_name: value} |
| `close()` | Issue service 0x92 (StopDiagnosticSession); log warning on failure |

Design: read-only — only services 0x21 and 0x22 are used. No writes, routine control, or security access.

Mock mode: a `responses` dict maps block IDs to byte payloads. The session is flagged as `is_mock = True` and no AT commands are sent.

---

### `Hudson/core/isotp.py`

ISO 15765-2 (ISO-TP) CAN frame reassembly, for cases where ELM327's built-in auto-reassembly (ATCAF1) is disabled or raw CAN transport is used.

**`FrameType`** enum: `SINGLE` (0x0), `FIRST` (0x1), `CONSECUTIVE` (0x2), `FLOW_CONTROL` (0x3).

**`Reassembler`** — stateful.

- `feed(frame: bytes) -> bytes | None` — accepts an 8-byte CAN frame. Single frames return immediately. Multi-frame: FF records expected length and first chunk; CFs accumulate until the expected byte count is reached, then the complete payload is returned. Out-of-order or unexpected CFs raise `ValueError`.

---

### `Hudson/core/poller.py`

Tiered async PID poller.

**`PollSpec`** — `(command: OBDCommand, period_s: float)`. Example: `PollSpec(obd.commands.RPM, 0.1)` for 10 Hz.

**`Reading`** — `(command, response, received_at: float)`. Emitted into a queue consumed by the dashboard.

**`Poller`**:

- `start()` — spawns one `asyncio.Task` per spec.
- `stop()` — cancels all tasks, drains cancellations.
- `_run_one(spec)` — loop: query → emit Reading to queue → optionally call `on_reading` callback → sleep until next deadline. On query failure: log exception, backoff for `5 * period_s`, reset schedule. If a poll falls behind (elapsed > period), resets the deadline rather than spinning to catch up.
- `on_reading` — optional `Callable[[Reading], None]`, called synchronously after each reading is queued. Used by `TelemetryClient.record_reading()`.

---

### `Hudson/core/telemetry.py`

Optional async telemetry client. Only constructed if `--telemetry` is passed.

**`TelemetryClient`**:

- HTTP base URL: `https://api.muutto365.fi/api/telemetry`
- Auth: `Authorization: Bearer <HUDSON_TELEMETRY_TOKEN>`
- User-Agent: `Hudson/0.1 (OBD2 diagnostic)`
- Timeout: 10 seconds per request

**Event types:**

| Event | When | Payload |
|---|---|---|
| `session_start` | After init completes | session_id, vin, manufacturer, protocol, timestamp |
| `readings` | Every 5 seconds (batched) | session_id, list of {pid, value, ts} |
| `dtc_scan` | After each DTC pane scan | session_id, stored[], pending[], permanent[] |
| `session_end` | On clean shutdown | session_id, timestamp |

**`record_reading(reading)`** — synchronous, called from Poller callback. Extracts `magnitude` from the response value; skips if no numeric value. Uses `put_nowait` — readings are silently dropped if the queue (capacity 1000) is full.

**`record_dtcs(stored, pending, permanent)`** — async, creates a fire-and-forget task. DTC scans are infrequent enough that they are not batched.

All HTTP failures are caught and logged at WARNING. The TUI and OBD2 paths are never blocked by telemetry errors.

---

## 4. Manufacturer Modules

### `Hudson/manufacturers/registry.py`

Maps 3-character WMI (World Manufacturer Identifier — VIN positions 1–3) prefixes to decoder module paths. Entries are sorted most-specific first. `select_decoder(vin)` returns a module path string or falls back to `"Hudson.manufacturers.generic"` for unknown WMIs.

Coverage includes: VAG group (VW, Audi, SEAT, Škoda, Porsche, Bentley — WVW, WV1, WV2, WAU, VSS, VSSZZZ, TMB, WP0, SCF, SCA), BMW, Mercedes-Benz, Toyota, Volvo, and ~60 additional WMI entries.

---

### `Hudson/manufacturers/generic.py`

Fallback for unknown manufacturers.

- `name = "Generic"`
- `DISCOVERY_STRATEGY = "probe"` — init runs UDS probe (0xF189) before committing to full discovery
- `lookup_dtc(code)` → `None` — caller falls back to `dtc_lookup.py`

---

### `Hudson/manufacturers/vw_audi.py`

VAG group (Volkswagen, Audi, SEAT, Škoda).

- `name = "VW/Audi"`
- `DISCOVERY_STRATEGY = "uds"` — post-2008 VAG vehicles use UDS natively; the 0xF189 probe gate is skipped and full discovery starts immediately
- `DTC_DESCRIPTIONS` — 80+ P1xxx/P3xxx manufacturer-specific fault codes. Sources: Ross-Tech Wiki, VCDS label files.
- `lookup_dtc(code)` — returns from `DTC_DESCRIPTIONS` or `None`
- `UDS_DATA_IDENTIFIERS` — dict of 0x22 identifier decoders (currently a stub; populated as identifiers are confirmed)

---

### `Hudson/manufacturers/toyota.py`

Toyota and Lexus vehicles.

- `name = "Toyota"`
- `DISCOVERY_STRATEGY = "probe"`
- `ENHANCED_PIDS` — dict of `EnhancedPid` (UDS 0x22 identifier, name, unit, byte count, decode function) for 0x01xx range. Decoder functions: `_byte_minus40` (temperature), `_byte_pct` (load, throttle), `_rpm_16bit` (RPM /4), `_byte_kmh` (speed). Sources: TEMS TSBs, Techstream parameter IDs, community reverse-engineering of the 2AZ-FE engine.
- `ENHANCED_BLOCKS` — dict of `EnhancedBlock` for mode 0x21 multi-field responses (e.g., block 0x10 returns RPM + coolant + intake + throttle + load in one query).
- `read_enhanced_pid(connection, pid_def)` — query and decode a single enhanced PID.
- `MOCK_TOYOTA_UDS_RESPONSES` / `MOCK_TOYOTA_ENHANCED_LOCAL` — byte payloads for test fixtures.

---

### `Hudson/manufacturers/volvo.py`

Volvo P2 platform (S60/V70/S80/XC90 1999–2010, Bosch ME7 and Denso ECUs).

- `name = "Volvo"`
- `DISCOVERY_STRATEGY = "probe"`
- `VOLVO_BLOCKS` — dict of `KwpBlock` for KWP2000 service 0x21. Block 0x01: RPM, coolant temp, intake temp, throttle position, engine load. Sources: Vadis/VIDA workshop documentation, community reverse-engineering.
- `kwp_blocks = VOLVO_BLOCKS` — the presence of this attribute signals `init.py` that a KWP session attempt is warranted.
- `read_kwp_block(connection, session, block_id)` — query and parse a KWP block.
- `MOCK_VOLVO_KWP_RESPONSES` / `MOCK_KWP_RESPONSES` — byte payloads for test fixtures.

---

## 5. TUI Modules

### `Hudson/tui/screens/splash.py` — `SplashScreen`

Full-screen init display. `Screen[InitResult]` — dismissed with the `InitResult` on success.

Layout: centered vertical panel with title, 9 step rows (one per `InitStep`), and a footer. Each row displays: status icon (✓ done, ⏳ in-progress, ✗ error, · pending), step name, detail text.

`on_mount()` starts two concurrent coroutines:
1. `run_init()` — drives the init state machine and feeds events into a queue.
2. `_consume_events()` — reads from the queue and updates the label widgets.

`UDS_DISCOVERY` step: the label shows a progress bar rendered as Unicode block characters plus a `(current / total)` counter. All other steps: show detail text and optionally an error annotation in a muted color.

On failure: calls `self.app.exit()` with a non-zero code. On success: calls `self.dismiss(result)`.

---

### `Hudson/tui/screens/main.py` — `MainScreen`

The persistent main screen shown after init completes.

**Layout:**
```
┌─ HUDSON   VIN · Manufacturer · Protocol · PIDs ─── HH:MM ─┐
│ [ Dashboard ]  [ DTCs ]  [ Log ]  [ Vehicle ]               │
├─────────────────────────────────────────────────────────────┤
│  (active pane)                                              │
├─────────────────────────────────────────────────────────────┤
│  UDS scan  [████████░░░░░░░░]   52%   ETA 24s   17 found   │
└─────────────────────────────────────────────────────────────┘
```

**`TabBar`** — btop-style `[ Label ]` bracketed tabs. Active tab is bold + accented. `set_active(tab_id)` swaps CSS classes.

**`UdsScanStrip`** — single-line footer, hidden (`display: none`) until priority-2 sweep starts. `show_scanning()` renders a 16-character block-character progress bar + ETA. `show_complete()` shows the final count.

**`MainScreen`**:

- `on_mount()` — filter `GAUGE_CATALOG` against `supported_commands`, start `Poller` (with telemetry `on_reading` wired), kick off background UDS priority-2 task.
- `on_unmount()` — stop poller, cancel UDS task.
- `_on_uds_progress()` — called every 50 identifiers; updates `UdsScanStrip`. Wrapped in try/except because the strip may be gone if the screen dismounts mid-sweep.
- `_on_uds_done()` — done callback on the UDS task. If the task raised, logs at ERROR with the exception. If it completed normally, shows the final count.
- Tab navigation: `action_next_tab()` / `action_prev_tab()` via `←` / `→` bindings with `priority=True`.

---

### `Hudson/tui/panes/dashboard.py` — `DashboardPane`

Live PID gauge grid. `Widget` embedded in `MainScreen` via `ContentSwitcher`.

Reads from an `asyncio.Queue[Reading]` fed by `Poller`. An internal consumer coroutine (`_consume_readings()`) runs continuously: dequeues a `Reading`, finds the matching `Gauge` widget by PID name, extracts `response.value.magnitude`, and calls `gauge.value = ...`. The reactive `value` attribute on `Gauge` triggers a re-render.

On first `on_show()`, starts the consumer. Gauge widgets for PIDs not in `GAUGE_CATALOG` are not rendered.

---

### `Hudson/tui/panes/dtcs.py` — `DtcPane`

DTC scanner. `Widget` embedded in `MainScreen`.

**Custom OBD commands:**

```python
GET_PENDING_DTC  = OBDCommand("GET_PENDING_DTC",  ..., b"\x07", 0, _raw_dtc_decoder, ECU.ALL, False)
GET_PERMANENT_DTC = OBDCommand("GET_PERMANENT_DTC", ..., b"\x0A", 0, _raw_dtc_decoder, ECU.ALL, False)
```

`_raw_dtc_decoder` strips the response service byte (mode + 0x40) from the raw message, returning the DTC payload bytes for `decode_dtc_list()`.

**`ClearDtcConfirmScreen`** — modal dialog with warning text explaining that mode 04 resets readiness monitors, which can cause an emissions inspection failure.

**`DtcPane._do_scan()`**:

1. Query mode 03 via `obd.commands.GET_DTC`. python-obd returns `[(code, desc), ...]`.
2. Query mode 07 via `GET_PENDING_DTC`. Decode with `decode_dtc_list()`.
3. Query mode 0A via `GET_PERMANENT_DTC`. Decode with `decode_dtc_list()`.
4. Each step is independently try/excepted; failure of one does not skip the others.
5. After all three modes, call `telemetry.record_dtcs(stored, pending, permanent)` if telemetry is active.

**`_add_dtc_row()`** — resolves the DTC description via three-level lookup:
1. Manufacturer module's `lookup_dtc()` (P1xxx/P3xxx codes).
2. `dtc_lookup_description()` (SAE generic database).
3. python-obd's description string from mode 03.

DataTable columns: `Status` (Stored/Pending/Permanent), `Code`, `System`, `Type` (SAE or Manufacturer), `Description`.

Bindings: `r` = refresh (re-scan all modes), `c` = clear stored (pushes confirm modal, then sends mode 04).

Auto-scans on first `on_show()`.

---

### `Hudson/tui/panes/log.py` — `LogPane`

Scrolling log viewer. Installs a `_RichLogHandler` into the root Python logger on mount; removes it on unmount.

Log level → color mapping:
- `DEBUG` → dim
- `INFO` → default
- `WARNING` → yellow
- `ERROR` → red
- `CRITICAL` → bold red

Format: `HH:MM:SS  logger_name  LEVEL     message`

---

### `Hudson/tui/panes/vehicle.py` — `VehiclePane`

Static vehicle identity display. Populated from `InitResult` on mount. Shows VIN, manufacturer, protocol, and a sorted table of all `supported_commands` with their mode byte, PID byte, command name, and description.

---

### `Hudson/tui/widgets/gauge.py`

**`GaugeConfig`** — `(label, unit, max_val, color, interval_s)`.

**`GAUGE_CATALOG`** — 24 pre-configured entries: RPM (0–8000), SPEED (0–220 km/h), THROTTLE_POS (0–100%), COOLANT_TEMP (−40–215 °C), INTAKE_TEMP (−40–80 °C), ENGINE_LOAD (0–100%), MAF (0–655 g/s), SHORT_FUEL_TRIM_1, LONG_FUEL_TRIM_1, INTAKE_PRESSURE (0–255 kPa), BAROMETRIC_PRESSURE, TIMING_ADVANCE, OBD_COMPLIANCE, O2_S1_WR_VOLTAGE, FUEL_LEVEL, WARMUPS_SINCE_DTC_CLEAR, FUEL_INJECT_TIMING, FUEL_RAIL_PRESSURE_VAC, EGR_ERROR, EVAPORATIVE_PURGE, CATALYST_TEMP_B1S1, CONTROL_MODULE_VOLTAGE, AMBIENT_AIR_TEMP, ETHANOL_PERCENT.

**`Gauge`** — `Widget` with `reactive[float] value`.

Layout: header line (label + current value + unit), sparkline history graph (last N values height-scaled to peak), fill bar (0–100% scaled to `max_val`). Tracks and displays min/max across the session. The `.gauge--disabled` CSS class dims the widget when the PID is not supported.

---

### `Hudson/hudson_data/labels_loader.py`

JSON-backed UDS identifier label database. Per-manufacturer JSON files map 0x22 identifiers to label, unit, confidence score, notes, and an optional formula string.

**Formula DSL** supports type prefixes (`uint8`, `uint16_be`, `int16_be`, `uint32_be`, `ascii`) and arithmetic suffixes (`/ N`, `* N`, `- N`, `+ N`). Also handles Toyota-style named byte formulas like `(A*256+B)/1280`.

**`get_label_db(manufacturer)`** — lazy-loads and caches the JSON file for a manufacturer name.

---

## 6. Test Suite and Fixtures

### `tests/fixtures/fake_connection.py`

**`FakeConnection`** — implements the `ObdConnection` interface without hardware.

- VIN: `WV2ZZZ7HZ8H123456` (VAG van format)
- Supported commands: RPM, SPEED, THROTTLE_POS, COOLANT_TEMP, INTAKE_TEMP, ENGINE_LOAD, VIN, GET_DTC, CLEAR_DTC
- `_synthetic_value(cmd)` — generates waveforms (sine waves with different periods and offsets) for smooth dashboard animation in `--mock` mode
- DTC management: starts with a preset list of DTCs; `CLEAR_DTC` command empties the list; subsequent `GET_DTC` returns empty

**`FakeNoMode09VinConnection`** — mode 09 returns null response; UDS 0xF190 returns a valid VIN. Used to test VIN chain fallback.

**`FakeVolvoConnection`** — Volvo WMI prefix; UDS not supported; KWP session works; `MOCK_VOLVO_KWP_RESPONSES` injected.

**`FakeToyotaConnection`** — Toyota WMI; both UDS 0x22 and mode 0x21 responses injected from Toyota mock constants.

---

### `tests/test_dtc.py`

Round-trip encode/decode tests for all four DTC system types (P, C, B, U). Includes edge cases: manufacturer-specific codes (P1xxx, P3xxx), high nibble codes (P3FFF), padding stripping in `decode_dtc_list`.

### `tests/test_isotp.py`

Single-frame and multi-frame reassembly. Tests the actual VIN multi-frame sequence (FF + 2 CFs = 20 bytes). Validates that out-of-order consecutive frames raise `ValueError`.

### `tests/test_vin_chain.py`

VIN resolution chain tests using fake connections:
- Mode 09 succeeds immediately.
- Mode 09 null → UDS 0xF190 succeeds.
- All protocols fail → returns `None`.
- AT command history verified (ATD issued before each K-line attempt, ATSP0 restored in finally).

### `tests/test_kwp2000.py`

KWP session lifecycle: start, query, parse, close. Mock mode verified (`is_mock` property). `parse_block` tested with single and multi-field blocks at various byte offsets.

### `tests/test_toyota_enhanced.py`

Toyota enhanced PID and enhanced block decoding via `FakeToyotaConnection`.

### `tests/test_volvo_kwp.py`

Volvo KWP block structure verification and field decoding against `MOCK_VOLVO_KWP_DATA`.

### `tests/test_tui_smoke.py`

Smoke test for TUI lifecycle: mounts `HudsonApp` with a `FakeConnection`, verifies `SplashScreen` is the active screen, exits cleanly.

---

## 7. Data Flow End-to-End

### Startup

```
main() in cli.py
  → _configure_logging()         # logs/hudson.log always, stderr if --debug
  → ObdConnection(config)        # or FakeConnection if --mock
  → TelemetryClient(token)       # only if --telemetry + HUDSON_TELEMETRY_TOKEN set
  → HudsonApp.run_async()
      → _init_flow()
          → SplashScreen.on_mount()
              → run_init() [async generator]
                  → connection.connect()          # asyncio.to_thread
                  → resolve_vin_chain()
                  → select_decoder(vin) → import module
                  → UdsDiscovery.run_priority1()  # probes standard ranges
                  → KwpSession (if K-line + kwp_blocks)
                  → query PIDS_A/B/C/D
              → _consume_events() [streams InitEvents to label widgets]
          → dismiss(init_result)
      → telemetry.start(init_result)             # POST session_start
      → MainScreen(connection, init_result, telemetry)
          → on_mount()
              → Poller.start()                   # polls PIDs at tiered rates
              → asyncio.create_task(uds.run_priority2_background())
```

### Live Sensor Data

```
Poller._run_one(spec) → asyncio.to_thread(obd.query)
  → Reading(command, response, received_at) → out_queue.put()
  → on_reading(reading) → telemetry.record_reading()  [put_nowait]

DashboardPane._consume_readings()
  → queue.get() → Gauge.value = magnitude  [reactive → re-render]

TelemetryClient._batch_loop()  [every 5 seconds]
  → _flush() → POST /api/telemetry  {event: "readings", readings: [...]}
```

### DTC Scan

```
DtcPane.action_refresh()
  → _do_scan()
      → connection.query(GET_DTC)           # mode 03
      → connection.query(GET_PENDING_DTC)   # mode 07
      → connection.query(GET_PERMANENT_DTC) # mode 0A
      → for each code: _add_dtc_row()
          → manufacturer.lookup_dtc(code)
          → dtc_lookup_description(code)
          → DataTable.add_row(...)
      → telemetry.record_dtcs(stored, pending, permanent)
          → asyncio.create_task(_post({event: "dtc_scan", ...}))
```

### Shutdown

```
user presses q
  → HudsonApp.action_quit()
  → MainScreen.on_unmount()
      → Poller.stop()
      → uds_task.cancel()
  → _amain() finally block
      → telemetry.stop()
          → _batch_task.cancel()
          → _flush()       # drain remaining readings
          → POST session_end
          → httpx.AsyncClient.aclose()
      → connection.close()
```

---

## 8. Configuration and Dependencies

### `pyproject.toml`

- **Requires Python:** ≥ 3.11
- **Runtime dependencies:**

| Package | Version | Purpose |
|---|---|---|
| `obd` | ≥0.7.2 | ELM327 driver and J1979 PID table |
| `textual` | ≥0.80.0 | Async terminal UI framework |
| `textual-plotext` | ≥0.2.1 | Sparkline widgets |
| `pyserial` | ≥3.5 | Serial port access (explicit, for rfcomm paths) |
| `aiosqlite` | ≥0.19.0 | Async SQLite for ECU discovery cache |
| `httpx` | ≥0.27 | Async HTTP client for telemetry |

- **Dev dependencies:** `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `textual-dev`
- **Entry point:** `hudson = "Hudson.cli:main"`

### Log file

`logs/hudson.log` — rotating, 1 MB max, 3 backup files, UTF-8. The `logs/` directory is created on startup if it does not exist and is excluded from git via `.gitignore`.

### ECU cache

`~/.hudson/ecu_cache.db` — SQLite, created automatically on first UDS discovery run.

### Telemetry token

`HUDSON_TELEMETRY_TOKEN` environment variable. Never passed as a CLI argument. Only read when `--telemetry` is active. If the flag is set but the variable is absent, Hudson prints an error and exits before opening the connection.

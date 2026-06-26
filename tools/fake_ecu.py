#!/usr/bin/env python3
"""Software ECU for Hudson integration testing.

Listens on a vcan interface and responds to ISO-TP OBD/UDS frames according
to a YAML scenario file.  Does not depend on the isotp library — ISO-TP
framing is handled inline using the same logic as elm327_emu.py.

Supported services:
    Mode 09 PID 02  — VIN broadcast
    Mode 01         — current data PIDs
    Mode 03         — stored DTCs
    Mode 07         — pending DTCs
    Mode 0A         — permanent DTCs
    Mode 04         — clear DTCs (acks; no state change in this process)
    UDS 22          — ReadDataByIdentifier (ECM and gateway addresses)
    UDS 3E          — TesterPresent keepalive (acked silently)

Responds with NRC 0x11 (serviceNotSupported) for unknown services, and
NRC 0x31 (requestOutOfRange) for known services with unknown PIDs/IDs.

Usage:
    python3 tools/fake_ecu.py --scenario tools/scenarios/ford_transit_2010.yaml
    python3 tools/fake_ecu.py --scenario tools/scenarios/ford_transit_2010.yaml --degraded
    python3 tools/fake_ecu.py --help
"""

from __future__ import annotations

import argparse
import copy
import logging
import signal
import sys
import time
from pathlib import Path

import can
import yaml

log = logging.getLogger(__name__)


# ── DTC helpers ───────────────────────────────────────────────────────────────

def _encode_dtc(code: str) -> bytes:
    """Encode 'P0401' → b'\\x04\\x01' per SAE J2012."""
    system = {"P": 0, "C": 1, "B": 2, "U": 3}[code[0].upper()]
    d1 = int(code[1])
    d2 = int(code[2], 16)
    tail = int(code[3:5], 16)
    return bytes([(system << 6) | (d1 << 4) | d2, tail])


def _dtc_payload(response_service: int, codes: list[str]) -> bytes:
    """Build mode 03/07/0A payload: [service_response, dtc_pair, ...]."""
    payload = bytes([response_service])
    for code in codes:
        payload += _encode_dtc(code)
    return payload


# ── ISO-TP helpers ────────────────────────────────────────────────────────────

def _isotp_segments(payload: bytes) -> list[bytes]:
    """Encode payload into ISO-TP CAN frames (8 bytes each)."""
    n = len(payload)
    if n <= 7:
        return [(bytes([n]) + payload).ljust(8, b"\x00")]
    frames: list[bytes] = []
    frames.append(
        (bytes([0x10 | ((n >> 8) & 0x0F), n & 0xFF]) + payload[:6]).ljust(8, b"\x00")
    )
    sn, idx = 1, 6
    while idx < n:
        frames.append(
            (bytes([0x20 | (sn & 0x0F)]) + payload[idx : idx + 7]).ljust(8, b"\x00")
        )
        idx += 7
        sn += 1
    return frames


# ── Scenario loader ───────────────────────────────────────────────────────────

def _load_scenario(path: Path, degraded: bool) -> dict:
    """Load YAML scenario and optionally merge the degraded profile."""
    raw = yaml.safe_load(path.read_text())
    if not degraded or "degraded" not in raw:
        return raw

    scenario = copy.deepcopy(raw)
    overrides = scenario.pop("degraded")

    for key in ("mode01", "mode22"):
        if key in overrides:
            scenario.setdefault(key, {}).update(overrides[key])

    for key in ("dtcs_stored", "dtcs_pending", "dtcs_permanent"):
        if key in overrides:
            scenario[key] = overrides[key]

    log.info("Degraded profile active — applied overrides: %s", list(overrides.keys()))
    return scenario


# ── Fake ECU ─────────────────────────────────────────────────────────────────

class FakeEcu:
    """Responds to ISO-TP OBD/UDS requests on a vcan interface."""

    def __init__(self, scenario: dict, interface: str) -> None:
        self._s = scenario
        self._interface = interface
        self._bus: can.BusABC | None = None
        self._running = False

        self._ecm_rx = int(scenario.get("ecm_rx", 0x7E0))
        self._ecm_tx = int(scenario.get("ecm_tx", 0x7E8))
        self._gw_rx  = int(scenario.get("gw_rx",  0x7D9))
        self._gw_tx  = int(scenario.get("gw_tx",  0x7DA))

        # Partial multi-frame reassembly state keyed by rx CAN ID
        self._ff_buf: dict[int, dict] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._bus = can.Bus(interface="socketcan", channel=self._interface)
        self._running = True
        log.info(
            "FakeEcu started on %s  ECM=%03X→%03X  GW=%03X→%03X  VIN=%s",
            self._interface,
            self._ecm_rx, self._ecm_tx,
            self._gw_rx,  self._gw_tx,
            self._s.get("vin", "—"),
        )

    def stop(self) -> None:
        self._running = False
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
        log.info("FakeEcu stopped")

    def run(self) -> None:
        """Blocking event loop — call after start()."""
        assert self._bus is not None
        while self._running:
            msg = self._bus.recv(timeout=0.01)
            if msg is None:
                continue
            self._handle_frame(msg)

    # ── ISO-TP receive ────────────────────────────────────────────────────────

    def _handle_frame(self, msg: can.Message) -> None:
        arb_id = msg.arbitration_id
        data   = bytes(msg.data)
        pci    = (data[0] >> 4) & 0x0F

        if pci == 0:   # Single Frame
            length  = data[0] & 0x0F
            payload = data[1 : 1 + length]
            self._dispatch(arb_id, payload)

        elif pci == 1:  # First Frame — start reassembly, send FC
            length  = ((data[0] & 0x0F) << 8) | data[1]
            self._ff_buf[arb_id] = {"expected": length, "data": bytearray(data[2:])}
            # Flow Control: ContinueToSend, BlockSize=0, STmin=0
            tx_id = self._tx_for(arb_id)
            if tx_id is not None:
                fc = bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                self._send_raw(tx_id, fc)

        elif pci == 2:  # Consecutive Frame
            if arb_id not in self._ff_buf:
                return
            buf = self._ff_buf[arb_id]
            buf["data"].extend(data[1:])
            if len(buf["data"]) >= buf["expected"]:
                payload = bytes(buf["data"][: buf["expected"]])
                del self._ff_buf[arb_id]
                self._dispatch(arb_id, payload)

        # Frame type 3 (Flow Control from tester) — no action needed here

    def _tx_for(self, rx_id: int) -> int | None:
        if rx_id in (self._ecm_rx, 0x7DF):
            return self._ecm_tx
        if rx_id == self._gw_rx:
            return self._gw_tx
        return None

    # ── request dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, rx_id: int, payload: bytes) -> None:
        if not payload:
            return
        svc = payload[0]
        log.debug("RX %03X  %s", rx_id, payload.hex(" ").upper())

        if rx_id in (self._ecm_rx, 0x7DF):
            response = self._handle_ecm(svc, payload)
            tx_id    = self._ecm_tx
        elif rx_id == self._gw_rx:
            response = self._handle_gateway(svc, payload)
            tx_id    = self._gw_tx
        else:
            return

        if response is not None:
            log.debug("TX %03X  %s", tx_id, response.hex(" ").upper())
            self._send_isotp(tx_id, response)

    # ── ECM service handlers ──────────────────────────────────────────────────

    def _handle_ecm(self, svc: int, payload: bytes) -> bytes | None:
        if svc == 0x09:
            return self._mode09(payload)
        if svc == 0x01:
            return self._mode01(payload)
        if svc == 0x03:
            return self._mode03()
        if svc == 0x07:
            return self._mode07()
        if svc == 0x0A:
            return self._mode0A()
        if svc == 0x04:
            return bytes([0x44])   # clear DTCs ack
        if svc == 0x19:
            return self._uds19(payload)
        if svc == 0x22:
            return self._uds22(payload, ecm=True)
        if svc == 0x3E:
            return bytes([0x7E, 0x00])   # TesterPresent positive response
        return bytes([0x7F, svc, 0x11])  # NRC: serviceNotSupported

    def _handle_gateway(self, svc: int, payload: bytes) -> bytes | None:
        if svc == 0x22:
            return self._uds22(payload, ecm=False)
        return bytes([0x7F, svc, 0x11])

    # ── OBD service implementations ───────────────────────────────────────────

    def _mode09(self, payload: bytes) -> bytes | None:
        if len(payload) < 2:
            return bytes([0x7F, 0x09, 0x31])
        pid = payload[1]
        if pid == 0x00:
            # Supported mode-09 PIDs: PID 02 (VIN) = bit 30 = 0x40000000
            return bytes([0x49, 0x00, 0x40, 0x00, 0x00, 0x00])
        if pid == 0x02:
            vin = self._s.get("vin", "00000000000000000").encode("ascii")
            return bytes([0x49, 0x02, 0x01]) + vin
        return bytes([0x7F, 0x09, 0x31])   # NRC: requestOutOfRange

    def _mode01(self, payload: bytes) -> bytes | None:
        if len(payload) < 2:
            return bytes([0x7F, 0x01, 0x31])
        pid = payload[1]

        # Supported-PID bitmask meta-queries (0x00, 0x20, 0x40)
        supported = self._s.get("supported_pids", {})
        if pid in supported:
            bm = int(supported[pid]).to_bytes(4, "big")
            return bytes([0x41, pid]) + bm

        data = self._s.get("mode01", {}).get(pid)
        if data is None:
            return bytes([0x7F, 0x01, 0x31])
        return bytes([0x41, pid]) + bytes(data)

    def _mode03(self) -> bytes:
        codes = self._s.get("dtcs_stored", [])
        return _dtc_payload(0x43, codes)

    def _mode07(self) -> bytes:
        codes = self._s.get("dtcs_pending", [])
        return _dtc_payload(0x47, codes)

    def _mode0A(self) -> bytes:
        codes = self._s.get("dtcs_permanent", [])
        return _dtc_payload(0x4A, codes)

    def _uds19(self, payload: bytes) -> bytes:
        """UDS service 0x19 ReadDTCInformation.

        Sub-function 0x02 (reportDTCByStatusMask) and 0x0A (reportSupportedDTC)
        return stored DTCs. Each record is [hi, lo, 0x00, 0x8C] (status = confirmed
        + pending + MIL).  All other sub-functions return NRC 0x12.
        """
        if len(payload) < 2:
            return bytes([0x7F, 0x19, 0x13])   # NRC: incorrectMessageLength
        sub_fn = payload[1]
        if sub_fn not in (0x02, 0x0A):
            return bytes([0x7F, 0x19, 0x12])   # NRC: subFunctionNotSupported
        codes = self._s.get("dtcs_stored", [])
        records = b"".join(
            bytes([hi, lo, 0x00, 0x8C])
            for hi, lo in (_encode_dtc(c) for c in codes)
        )
        return bytes([0x59, sub_fn, 0xFF]) + records  # 0xFF = all status bits available

    def _uds22(self, payload: bytes, *, ecm: bool) -> bytes:
        if len(payload) < 3:
            return bytes([0x7F, 0x22, 0x13])   # NRC: incorrectMessageLength
        identifier = (payload[1] << 8) | payload[2]
        hi, lo = payload[1], payload[2]

        table = self._s.get("mode22" if ecm else "gw_mode22", {})
        raw = table.get(identifier)

        if raw is None:
            return bytes([0x7F, 0x22, 0x31])   # NRC: requestOutOfRange

        if isinstance(raw, str):
            data = raw.encode("ascii")
        else:
            data = bytes(raw)

        return bytes([0x62, hi, lo]) + data

    # ── ISO-TP transmit ───────────────────────────────────────────────────────

    def _send_isotp(self, tx_id: int, payload: bytes) -> None:
        frames = _isotp_segments(payload)
        self._send_raw(tx_id, frames[0])

        if len(frames) == 1:
            return

        # Multi-frame: wait for Flow Control from tester before sending CFs
        assert self._bus is not None
        fc_deadline = time.monotonic() + 1.0
        while time.monotonic() < fc_deadline:
            fc_msg = self._bus.recv(timeout=0.02)
            if fc_msg is not None and len(fc_msg.data) >= 1:
                pci = (fc_msg.data[0] >> 4) & 0x0F
                if pci == 3:   # Flow Control
                    break
        else:
            log.warning("TX %03X: no Flow Control received — aborting multi-frame send", tx_id)
            return

        for cf in frames[1:]:
            time.sleep(0.0005)   # 0.5 ms inter-frame gap
            self._send_raw(tx_id, cf)

    def _send_raw(self, tx_id: int, data: bytes) -> None:
        assert self._bus is not None
        self._bus.send(
            can.Message(arbitration_id=tx_id, data=data, is_extended_id=False)
        )


# ── entry point ───────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  fake_ecu  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Software ECU for Hudson vcan integration testing"
    )
    parser.add_argument(
        "--scenario",
        required=True,
        type=Path,
        metavar="YAML",
        help="Path to scenario YAML file",
    )
    parser.add_argument(
        "--interface",
        default="vcan0",
        metavar="IFACE",
        help="SocketCAN interface to bind (default: vcan0)",
    )
    parser.add_argument(
        "--degraded",
        action="store_true",
        help="Apply the degraded override profile from the scenario YAML",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log every CAN frame (DEBUG level)",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    scenario = _load_scenario(args.scenario, args.degraded)
    ecu = FakeEcu(scenario, args.interface)

    def _shutdown(sig, frame):  # noqa: ANN001
        log.info("Signal %d received — stopping", sig)
        ecu.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    ecu.start()
    try:
        ecu.run()
    except KeyboardInterrupt:
        pass
    finally:
        ecu.stop()


if __name__ == "__main__":
    main()

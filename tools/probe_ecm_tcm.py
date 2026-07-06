"""Directly probe ECM (0x7E0) and TCM (0x7E1) via TesterPresent and UDS 0x19."""

from __future__ import annotations

import sys
import obd
from obd import OBDCommand
from obd.protocols import ECU


def _passthrough(msgs):
    if not msgs:
        return None
    return bytes(msgs[0].data)


def send_at(iface, cmd: str) -> str:
    try:
        lines = iface._ELM327__send(cmd)
        if not lines:
            return ""
        parts = []
        for l in lines:
            parts.append(l.decode() if isinstance(l, bytes) else l)
        return "\n".join(parts)
    except Exception as e:
        return f"ERROR: {e}"


def probe_tester_present(conn, iface, addr: int) -> bool:
    """Send TesterPresent (0x3E 0x00) to addr; return True on 0x7E response."""
    send_at(iface, f"ATSH {addr:03X}")
    cmd = OBDCommand(f"TP_{addr:03X}", f"TesterPresent@{addr:03X}", bytes([0x3E, 0x00]), 0, _passthrough, ECU.ALL, False)
    resp = conn.query(cmd, force=True)
    if resp.is_null() or resp.value is None:
        return False
    raw = bytes(resp.value)
    print(f"  0x{addr:03X} TesterPresent raw: {raw.hex()}")
    return len(raw) >= 1 and raw[0] == 0x7E


def probe_uds19(conn, iface, addr: int) -> bytes | None:
    """Send UDS 0x19 0x02 0xFF (reportDTCByStatusMask) to addr."""
    send_at(iface, f"ATSH {addr:03X}")
    cmd = OBDCommand(f"DTC_{addr:03X}", f"UDS0x19@{addr:03X}", bytes([0x19, 0x02, 0xFF]), 0, _passthrough, ECU.ALL, False)
    resp = conn.query(cmd, force=True)
    if resp.is_null() or resp.value is None:
        return None
    return bytes(resp.value)


def probe_mode01(conn, iface, addr: int) -> bytes | None:
    """Send Mode 01 PID 0x00 (supported PIDs) to addr."""
    send_at(iface, f"ATSH {addr:03X}")
    cmd = OBDCommand(f"M01_{addr:03X}", f"Mode01@{addr:03X}", bytes([0x01, 0x00]), 0, _passthrough, ECU.ALL, False)
    resp = conn.query(cmd, force=True)
    if resp.is_null() or resp.value is None:
        return None
    return bytes(resp.value)


def main() -> None:
    port = "/dev/rfcomm0"
    print(f"Connecting to {port}...")
    conn = obd.OBD(portstr=port, fast=False, timeout=0.5, check_voltage=False)
    print(f"Status: {conn.status()}")
    print(f"Protocol: {conn.protocol_name()}")

    iface = getattr(conn, "interface", getattr(conn, "_interface", None))
    if iface is None:
        print("ERROR: no ELM327 interface found")
        sys.exit(1)

    print(f"\nAdapter: {send_at(iface, 'ATI').strip()}")
    print(f"Voltage: {send_at(iface, 'ATRV').strip()}")

    proto = conn.protocol_name().lower()
    is_can = "can" in proto
    is_kline = any(kw in proto for kw in ("9141", "14230", "kwp"))

    print(f"\nProtocol type: {'CAN' if is_can else 'K-line (ISO 9141-2 / KWP2000)' if is_kline else 'unknown'}")

    if is_can:
        # Disable adaptive timing for consistent probing
        send_at(iface, "ATAT 0")

        print("\n" + "="*60)
        print("CAN: PROBING ECM (0x7E0) and TCM (0x7E1)")
        print("="*60)

        for addr, label in [(0x7E0, "ECM/PCM"), (0x7E1, "TCM")]:
            print(f"\n--- {label} (0x{addr:03X}) ---")

            tp = probe_tester_present(conn, iface, addr)
            print(f"  TesterPresent: {'RESPONDED' if tp else 'NO RESPONSE'}")

            m01 = probe_mode01(conn, iface, addr)
            print(f"  Mode 01 PID 00: {m01.hex() if m01 else 'NO RESPONSE'}")

            dtc_raw = probe_uds19(conn, iface, addr)
            print(f"  UDS 0x19 0x02: {dtc_raw.hex() if dtc_raw else 'NO RESPONSE'}")

        # Functional broadcast
        print("\n" + "="*60)
        print("FUNCTIONAL BROADCAST (0x7DF) — raw response CAN IDs")
        print("="*60)
        send_at(iface, "ATSH 7DF")
        cmd = OBDCommand("M01_FNC", "Mode01 broadcast", bytes([0x01, 0x00]), 0, _passthrough, ECU.ALL, False)
        resp = conn.query(cmd, force=True)
        if not resp.is_null() and resp.value is not None:
            for msg in resp.messages:
                if msg.frames:
                    raw_frame = msg.frames[0].raw
                    print(f"  Raw frame: {raw_frame[:20]!r}")
        else:
            print("  No response to functional broadcast")

        send_at(iface, "ATAT 1")
        send_at(iface, "ATSH 7E0")

    else:
        print("\n" + "="*60)
        print("K-LINE: CAN addresses (0x7E0/0x7E1) don't exist on this bus.")
        print("Testing Mode 03 (stored DTCs) via K-line instead.")
        print("="*60)

        resp = conn.query(obd.commands.GET_DTC, force=True)
        if resp.is_null() or resp.value is None:
            print("  Mode 03 GET_DTC: NO RESPONSE")
        elif not resp.value:
            print("  Mode 03 GET_DTC: responded — 0 DTCs stored")
        else:
            print(f"  Mode 03 GET_DTC: {len(resp.value)} DTC(s) stored:")
            for code, desc in resp.value:
                print(f"    {code}: {desc or '(no description)'}")

        print("\n  Mode 01 PID 00 (supported PIDs via K-line):")
        m01_cmd = OBDCommand("M01_00", "Mode01 PID00", bytes([0x01, 0x00]), 0, _passthrough, ECU.ALL, False)
        resp2 = conn.query(m01_cmd, force=True)
        if resp2.is_null() or resp2.value is None:
            print("  NO RESPONSE")
        else:
            print(f"  Raw: {bytes(resp2.value).hex()}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()

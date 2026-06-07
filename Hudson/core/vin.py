"""VIN reading — speculative multi-protocol resolution chain.

Attempts are made in order:

  1. OBD2 mode 09 PID 02 (standard broadcast via python-obd)
  2. UDS 0x22 0xF190 addressed to gateway (KWP addr 0x19, CAN 0x7D9)
  3. KWP2000 0x1A 0x90 addressed to instrument cluster (0x17)
  4. KWP2000 0x1A 0x90 addressed to gateway (0x19)
  5. KWP2000 0x1A 0x86 addressed to instrument cluster (older VAG)

Each attempt resets ELM327 adapter state before sending (AT D, protocol
switch, headers) so a failed or corrupted previous step cannot poison the
next one.

ELM327 K-line → CAN hygiene:
  After any K-line (ATSP3) attempt, ATSP0 and AT D are issued to restore
  auto-detect so the rest of init (UDS, mode 01 PIDs) runs on CAN.
"""

from __future__ import annotations

import asyncio
import logging

import obd

# SAE J17 model year encoding — position 9 (0-indexed) of the VIN.
# Letters I, O, Q, U, Z are never used in VINs.
#
# The table runs in two 30-year cycles:
#   Cycle 1: A=1980 … S=1995, T=1996 … Y=2000, 1=2001 … 9=2009
#   Cycle 2: A=2010 … S=2025, T=2026 … (ongoing)
#
# Letters A–S are unambiguous for a diagnostic tool: the first-cycle years
# (1980–1995) predate OBD-II, so we can safely return the second-cycle year.
#
# Letters T, V, W, X, Y are deliberately absent — both OBD-II-era cycles are
# plausible (e.g. T = 1996 or 2026, V = 1997 or 2027). decode_model_year()
# returns None for these so callers fall back to a runtime ECU probe.
AMBIGUOUS_YEARS: frozenset[str] = frozenset("TVWXY")

_YEAR_CHARS: dict[str, int] = {
    # A–S: return the second-cycle year (2010–2025); first cycle predates OBD-II
    "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014,
    "F": 2015, "G": 2016, "H": 2017, "J": 2018, "K": 2019,
    "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024,
    "S": 2025,
    # T, V, W, X, Y intentionally omitted — both cycles are OBD-II era.
    # Digits — unambiguous (2001–2009):
    "1": 2001, "2": 2002, "3": 2003, "4": 2004, "5": 2005,
    "6": 2006, "7": 2007, "8": 2008, "9": 2009,
}


def decode_model_year(vin: str) -> int | None:
    """Return the model year encoded in VIN position 9 (0-indexed).

    Returns None if the VIN is too short, the character is invalid, or the
    year is genuinely ambiguous (T/V/W/X/Y — both OBD-II era cycles).
    Callers must not use None to skip ECU probing; probe directly instead.
    """
    if len(vin) < 10:
        return None
    return _YEAR_CHARS.get(vin[9].upper())


from Hudson.core.connection import ObdConnection  # noqa: E402

log = logging.getLogger(__name__)

# ISO 14230-2 W3/W4 minimum delay after protocol switch before first message.
_KLINE_SWITCH_DELAY = 0.05


class VinReadError(RuntimeError):
    """Could not read VIN from the vehicle."""


# ── VIN parsing ───────────────────────────────────────────────────────────────

def _parse_vin_value(raw: object) -> str | None:
    """Extract and validate a VIN string from a raw OBD/UDS response value.

    Accepts bytes, bytearray, or str. Returns None if the result is not a
    valid 17-character alphanumeric VIN.
    """
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("ascii", errors="replace").strip()
    else:
        text = str(raw).strip()

    # Strip non-printable and non-ASCII characters.
    text = "".join(c for c in text if c.isprintable())

    # VINs are exactly 17 alphanumeric characters; reject anything shorter.
    # We don't enforce the I/O/Q exclusion here — real ECUs sometimes emit
    # marginal data and we'd rather surface a near-VIN than silently drop it.
    if len(text) == 17 and text.isalnum():
        return text
    return None


# ── Individual protocol attempts ──────────────────────────────────────────────

async def _try_mode09(connection: ObdConnection) -> str | None:
    """Step 1: OBD2 mode 09 PID 02 (standard broadcast)."""
    log.info("VIN chain [1/5]: mode 09 PID 02")
    try:
        # AT D resets headers, CAN format, and other AT params to default.
        # It does NOT change the active protocol — we stay on whatever CAN
        # variant python-obd negotiated during connect.
        await connection.send_at("ATD")
        response = await connection.query(obd.commands.VIN)
        if response.is_null() or response.value is None:
            log.info("VIN chain [1/5]: mode 09 → null (ECU did not respond)")
            return None
        vin = _parse_vin_value(response.value)
        if vin:
            log.info("VIN chain [1/5]: mode 09 → %s", vin)
        else:
            log.warning(
                "VIN chain [1/5]: mode 09 response not a valid VIN: %r", response.value
            )
        return vin
    except Exception:
        log.exception("VIN chain [1/5]: mode 09 failed")
        return None


async def _try_uds_f190_gateway(connection: ObdConnection) -> str | None:
    """Step 2: UDS 0x22 0xF190 to gateway (KWP addr 0x19, CAN ID 0x7D9).

    VAG CAN convention: diagnostic address 0x19 → CAN ID 0x7C0 | 0x19 = 0x7D9.
    AT SH sets the CAN transmit header; the ELM327 will filter responses from
    the corresponding response ID (0x7D9 + 0x08 = 0x7E1 by default in ISO
    15765-4 addressing). If the gateway responds on a non-standard CAN ID this
    step will time out cleanly and the chain continues.
    """
    log.info("VIN chain [2/5]: UDS 22 F190 to gateway (0x19 / CAN 0x7D9)")
    try:
        await connection.send_at("ATD")       # clear prior AT SH / format settings
        await connection.send_at("ATSH7D9")   # physical address to gateway
        raw = await connection.query_uds(0x22, 0xF190)
        if raw is None:
            log.info("VIN chain [2/5]: UDS F190 → no response")
            return None
        vin = _parse_vin_value(raw)
        if vin:
            log.info("VIN chain [2/5]: UDS F190 → %s", vin)
        else:
            log.warning("VIN chain [2/5]: UDS F190 response not a valid VIN: %r", raw)
        return vin
    except Exception:
        log.exception("VIN chain [2/5]: UDS F190 failed")
        return None
    finally:
        # Remove custom header so subsequent CAN queries are not misdirected.
        await connection.send_at("ATD")


async def _try_kwp_1a(
    connection: ObdConnection,
    step_label: str,
    addr: int,
    param: int,
) -> str | None:
    """Steps 3–5: KWP2000 service 0x1A (ReadECUIdentification) over K-line.

    Sends a physical-addressed K-line frame to ``addr`` with parameter ``param``
    (0x90 = VIN, 0x86 = older VAG VIN identifier). The ELM327 header for
    physical K-line addressing is [0x80, addr, 0xF1] (tester source = 0xF1).

    ELM327 state management:
      - AT D: reset all AT params to default before switching protocol
      - ATSP3: ISO 14230-4 KWP fast init (K-line)
      - ATSH 80 XX F1: physical KWP header for target address XX
      - ATH1: include response headers (needed to verify target address)
      After the attempt (success or failure):
      - ATD + ATSP0: restore to auto-detect so subsequent CAN traffic works
    """
    log.info(
        "VIN chain [%s]: KWP2000 1A %02X to addr 0x%02X", step_label, param, addr
    )
    try:
        await connection.send_at("ATD")
        await connection.send_at("ATSP3")     # ISO 14230-4 KWP fast init (K-line)
        await asyncio.sleep(_KLINE_SWITCH_DELAY)
        await connection.send_at(f"ATSH80{addr:02X}F1")  # physical header: tgt=addr, src=0xF1
        await connection.send_at("ATH1")      # include response headers

        # query_kwp_service sends [service, payload...] and strips the positive-
        # response echo byte (service + 0x40). For 0x1A the positive response is
        # 0x5A, and what comes back is [param_echo, data...].
        raw = await connection.query_kwp_service(0x1A, bytes([param]))
        if raw is None:
            log.info(
                "VIN chain [%s]: KWP 1A %02X/0x%02X → no response",
                step_label, param, addr,
            )
            return None

        # Strip the parameter echo byte if present.
        data = raw[1:] if len(raw) > 1 and raw[0] == param else raw
        vin = _parse_vin_value(data)
        if vin:
            log.info("VIN chain [%s]: KWP 1A → %s", step_label, vin)
        else:
            log.warning(
                "VIN chain [%s]: KWP 1A response not a valid VIN: %r", step_label, raw
            )
        return vin
    except Exception:
        log.exception("VIN chain [%s]: KWP 1A failed", step_label)
        return None
    finally:
        # Always restore to CAN auto-detect after touching K-line.
        # python-obd's internal protocol cache will re-sync on the next query.
        await connection.send_at("ATD")
        await connection.send_at("ATSP0")


# ── Public API ────────────────────────────────────────────────────────────────

async def resolve_vin_chain(connection: ObdConnection) -> str | None:
    """Speculatively try every VIN protocol in sequence; return the first hit.

    Returns the VIN string (17 alphanumeric chars) on success, or None if all
    five attempts fail. Each attempt sets up its own ELM327 adapter state from
    scratch before sending, so a failed or corrupted prior attempt cannot
    poison the next one.
    """
    log.info("VIN resolution chain: starting")

    vin = await _try_mode09(connection)
    if vin:
        return vin

    vin = await _try_uds_f190_gateway(connection)
    if vin:
        return vin

    vin = await _try_kwp_1a(connection, "3/5", addr=0x17, param=0x90)
    if vin:
        return vin

    vin = await _try_kwp_1a(connection, "4/5", addr=0x19, param=0x90)
    if vin:
        return vin

    vin = await _try_kwp_1a(connection, "5/5", addr=0x17, param=0x86)
    if vin:
        return vin

    log.warning("VIN resolution chain: all protocols exhausted — no VIN found")
    return None


async def read_vin(connection: ObdConnection) -> str:
    """Read VIN via mode 09 only. Raises VinReadError on failure.

    Kept for backward compatibility. New code should call resolve_vin_chain().
    """
    response = await connection.query(obd.commands.VIN)
    if response.is_null():
        raise VinReadError("VIN response was null (no reply or unsupported)")

    raw = response.value
    if raw is None:
        raise VinReadError("VIN response value was None")

    if isinstance(raw, (bytes, bytearray)):
        vin = raw.decode("ascii", errors="replace").strip()
    else:
        vin = str(raw).strip()

    vin = "".join(c for c in vin if c.isprintable())

    if len(vin) != 17:
        raise VinReadError(f"VIN length {len(vin)} != 17, got {vin!r}")

    return vin

"""Tests for BMW/MINI and Mercedes-Benz/smart manufacturer modules."""

from __future__ import annotations

import pytest

from Hudson.manufacturers import bmw, mercedes
from Hudson.manufacturers.registry import select_decoder


class TestBmwModule:
    def test_module_attributes(self):
        assert bmw.name == "BMW/MINI"
        assert bmw.DISCOVERY_STRATEGY == "uds"
        assert isinstance(bmw.UDS_DATA_IDENTIFIERS, dict)
        assert isinstance(bmw.DTC_DESCRIPTIONS, dict)

    def test_lookup_known_dtc_vanos(self):
        result = bmw.lookup_dtc("P1523")
        assert result is not None
        assert "VANOS" in result

    def test_lookup_known_dtc_throttle(self):
        result = bmw.lookup_dtc("P1545")
        assert result is not None
        assert "throttle" in result.lower()

    def test_lookup_unknown_dtc_returns_none(self):
        assert bmw.lookup_dtc("P9999") is None

    def test_lookup_case_sensitive(self):
        assert bmw.lookup_dtc("p1523") is None

    def test_dtc_table_non_empty(self):
        assert len(bmw.DTC_DESCRIPTIONS) > 0

    @pytest.mark.parametrize("vin_prefix,expected", [
        ("WBA3A1234", "Hudson.manufacturers.bmw"),   # BMW AG passenger (WBA)
        ("WBS3R9120", "Hudson.manufacturers.bmw"),   # BMW M GmbH (WBS)
        ("WMWZC3C5X", "Hudson.manufacturers.bmw"),   # MINI (WMW)
        ("WBY12345G", "Hudson.manufacturers.bmw"),   # BMW i electric (WBY)
        ("WBW123456", "Hudson.manufacturers.bmw"),   # BMW alternate (WBW)
    ])
    def test_registry_resolves_to_bmw(self, vin_prefix: str, expected: str):
        vin = (vin_prefix + "0" * 17)[:17]
        assert select_decoder(vin) == expected


class TestMercedesModule:
    def test_module_attributes(self):
        assert mercedes.name == "Mercedes-Benz/smart"
        assert mercedes.DISCOVERY_STRATEGY == "uds"
        assert isinstance(mercedes.UDS_DATA_IDENTIFIERS, dict)
        assert isinstance(mercedes.DTC_DESCRIPTIONS, dict)

    def test_lookup_known_dtc_throttle(self):
        result = mercedes.lookup_dtc("P1120")
        assert result is not None
        assert "throttle" in result.lower()

    def test_lookup_known_dtc_egr(self):
        result = mercedes.lookup_dtc("P1402")
        assert result is not None
        assert "EGR" in result

    def test_lookup_unknown_dtc_returns_none(self):
        assert mercedes.lookup_dtc("P9999") is None

    def test_lookup_case_sensitive(self):
        assert mercedes.lookup_dtc("p1120") is None

    def test_dtc_table_non_empty(self):
        assert len(mercedes.DTC_DESCRIPTIONS) > 0

    @pytest.mark.parametrize("vin_prefix,expected", [
        ("WDB1230451", "Hudson.manufacturers.mercedes"),  # classic MB (WDB)
        ("WDD2050131", "Hudson.manufacturers.mercedes"),  # modern MB (WDD)
        ("WDC1660761", "Hudson.manufacturers.mercedes"),  # MB SUV (WDC)
        ("WME4503301", "Hudson.manufacturers.mercedes"),  # smart (WME)
        ("WDF6330021", "Hudson.manufacturers.mercedes"),  # MB commercial (WDF)
        ("WMX1234567", "Hudson.manufacturers.mercedes"),  # Mercedes-AMG (WMX)
    ])
    def test_registry_resolves_to_mercedes(self, vin_prefix: str, expected: str):
        vin = (vin_prefix + "0" * 17)[:17]
        assert select_decoder(vin) == expected

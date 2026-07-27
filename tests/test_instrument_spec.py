"""InstrumentSpec (wit/risk/instrument_spec.py): new tests, no MT5 equivalent
existed for this exact shape (SymbolSpec.round_volume's rounding behavior is
carried over into round_quantity, but everything else here — price rounding,
the Nautilus-instrument factory — is new).
"""
from __future__ import annotations

from types import SimpleNamespace

from wit.risk.instrument_spec import InstrumentSpec, spec_for

EURUSD = InstrumentSpec(
    instrument_id="EUR/USD.IDEALPRO", price_increment=0.00005,
    min_quantity=1.0, quantity_step=1.0, max_quantity=2_000_000.0,
)
NVDA = InstrumentSpec(
    instrument_id="NVDA.SMART", price_increment=0.01,
    min_quantity=1.0, quantity_step=1.0, max_quantity=None,
)


def test_round_price_snaps_to_the_increment():
    assert EURUSD.round_price(1.123456) == 1.12345  # nearest 0.00005 tick
    assert NVDA.round_price(184.006) == 184.01


def test_round_price_is_a_no_op_when_increment_is_zero():
    spec = InstrumentSpec(instrument_id="x", price_increment=0.0,
                          min_quantity=1.0, quantity_step=1.0)
    assert spec.round_price(1.23456789) == 1.23456789


def test_round_quantity_snaps_and_clamps():
    assert EURUSD.round_quantity(1234.6) == 1235.0
    assert EURUSD.round_quantity(0.4) == 1.0        # clamped up to the minimum
    assert EURUSD.round_quantity(5_000_000.0) == 2_000_000.0  # clamped down to the max


def test_round_quantity_with_no_configured_max_does_not_clamp_down():
    assert NVDA.round_quantity(50_000.0) == 50_000.0


def test_round_quantity_zero_step_still_clamps_to_min_and_max():
    spec = InstrumentSpec(instrument_id="x", price_increment=0.01,
                          min_quantity=10.0, quantity_step=0.0, max_quantity=100.0)
    assert spec.round_quantity(5.0) == 10.0
    assert spec.round_quantity(500.0) == 100.0
    assert spec.round_quantity(50.0) == 50.0


# ── spec_for: duck-typed Nautilus Instrument factory ───────────────────────

def _fake_instrument(**over):
    base = {
        "id": "EUR/USD.IDEALPRO", "price_increment": 0.00005, "size_increment": 1.0,
        "min_quantity": 1.0, "max_quantity": 2_000_000.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_spec_for_reads_the_expected_fields():
    spec = spec_for(_fake_instrument())
    assert spec.instrument_id == "EUR/USD.IDEALPRO"
    assert spec.price_increment == 0.00005
    assert spec.quantity_step == 1.0
    assert spec.min_quantity == 1.0
    assert spec.max_quantity == 2_000_000.0
    assert spec.value_per_unit == 1.0
    assert spec.min_stop_distance == 0.0


def test_spec_for_defaults_min_quantity_to_the_size_increment_when_unset():
    spec = spec_for(_fake_instrument(min_quantity=None))
    assert spec.min_quantity == 1.0  # falls back to size_increment


def test_spec_for_handles_no_configured_max():
    spec = spec_for(_fake_instrument(max_quantity=None))
    assert spec.max_quantity is None


def test_spec_for_passes_through_value_per_unit_and_min_stop_distance():
    spec = spec_for(_fake_instrument(), value_per_unit=0.85, min_stop_distance=0.001)
    assert spec.value_per_unit == 0.85
    assert spec.min_stop_distance == 0.001


def test_spec_for_coerces_nautilus_style_price_and_quantity_objects():
    """Nautilus's real Price/Quantity types aren't plain floats but support
    float() - simulate that with a minimal wrapper rather than requiring
    nautilus_trader to be installed for this test."""
    class _Decimalish:
        def __init__(self, v):
            self._v = v

        def __float__(self):
            return self._v

    spec = spec_for(_fake_instrument(
        price_increment=_Decimalish(0.01), size_increment=_Decimalish(1.0),
        min_quantity=_Decimalish(1.0), max_quantity=_Decimalish(10_000.0),
    ))
    assert spec.price_increment == 0.01
    assert spec.max_quantity == 10_000.0

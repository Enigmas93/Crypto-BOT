from datetime import date

import pytest

from aegis.macro.models import MacroObservation


def test_from_fred_payload_parses_a_valid_observation():
    obs = MacroObservation.from_fred_payload("FEDFUNDS", {"date": "2026-03-01", "value": "5.25"})
    assert obs.series_id == "FEDFUNDS"
    assert obs.date == date(2026, 3, 1)
    assert obs.value == 5.25


def test_from_fred_payload_returns_none_for_missing_marker():
    assert MacroObservation.from_fred_payload("FEDFUNDS", {"date": "2026-02-01", "value": "."}) is None


def test_from_fred_payload_returns_none_for_empty_or_missing_value_key():
    assert MacroObservation.from_fred_payload("FEDFUNDS", {"date": "2026-02-01", "value": ""}) is None
    assert MacroObservation.from_fred_payload("FEDFUNDS", {"date": "2026-02-01"}) is None


def test_from_bls_payload_parses_a_valid_monthly_observation():
    obs = MacroObservation.from_bls_payload(
        "LNS14000000", {"year": "2026", "period": "M08", "periodName": "August", "value": "4.1"}
    )
    assert obs.series_id == "LNS14000000"
    assert obs.date == date(2026, 8, 1)
    assert obs.value == 4.1
    assert obs.source == "bls"


def test_from_bls_payload_skips_the_m13_annual_average_row():
    assert MacroObservation.from_bls_payload(
        "LNS14000000", {"year": "2026", "period": "M13", "periodName": "Annual", "value": "4.0"}
    ) is None


def test_from_bls_payload_returns_none_for_non_monthly_periods():
    # quarterly ("Q01") / annual ("A01") series are out of scope for now
    assert MacroObservation.from_bls_payload("SOME_QUARTERLY", {"year": "2026", "period": "Q01", "value": "1.0"}) is None


def test_from_bls_payload_returns_none_for_missing_or_malformed_value():
    assert MacroObservation.from_bls_payload("LNS14000000", {"year": "2026", "period": "M08", "value": "-"}) is None
    assert MacroObservation.from_bls_payload("LNS14000000", {"year": "2026", "period": "M08"}) is None
    assert MacroObservation.from_bls_payload("LNS14000000", {"period": "M08", "value": "4.1"}) is None  # no year


def test_from_bea_payload_parses_a_quarterly_observation():
    obs = MacroObservation.from_bea_payload("A191RL", {"TimePeriod": "2026Q1", "DataValue": "3.4"})
    assert obs.series_id == "A191RL"
    assert obs.date == date(2026, 1, 1)  # Q1 -> January
    assert obs.value == 3.4
    assert obs.source == "bea"


def test_from_bea_payload_maps_every_quarter_to_its_first_month():
    for q, month in [("Q1", 1), ("Q2", 4), ("Q3", 7), ("Q4", 10)]:
        obs = MacroObservation.from_bea_payload("A191RL", {"TimePeriod": f"2026{q}", "DataValue": "1.0"})
        assert obs.date == date(2026, month, 1)


def test_from_bea_payload_strips_thousands_separator_commas():
    obs = MacroObservation.from_bea_payload("A191RL", {"TimePeriod": "2026Q1", "DataValue": "23,542.7"})
    assert obs.value == pytest.approx(23542.7)


def test_from_bea_payload_handles_monthly_and_annual_time_periods():
    monthly = MacroObservation.from_bea_payload("X", {"TimePeriod": "2026M03", "DataValue": "1.0"})
    assert monthly.date == date(2026, 3, 1)
    annual = MacroObservation.from_bea_payload("X", {"TimePeriod": "2026", "DataValue": "1.0"})
    assert annual.date == date(2026, 1, 1)


def test_from_bea_payload_returns_none_for_the_na_marker_and_missing_fields():
    assert MacroObservation.from_bea_payload("A191RL", {"TimePeriod": "2026Q1", "DataValue": "(NA)"}) is None
    assert MacroObservation.from_bea_payload("A191RL", {"TimePeriod": "2026Q1"}) is None
    assert MacroObservation.from_bea_payload("A191RL", {"DataValue": "3.4"}) is None

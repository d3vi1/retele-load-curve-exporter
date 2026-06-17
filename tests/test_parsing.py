from dso_retele_electrice.parsing import parse_load_curve_csv, split_atr_cer
from dso_retele_electrice.client import ReteleElectriceClient


def test_parse_load_curve_csv_ro_decimal_q_columns():
    text = 'Zi;Q1;Q2\n"2026.06.01";"0,306000";"1,250000"\n'
    rows = parse_load_curve_csv(text)
    assert rows[0][0].isoformat() == "2026-06-01T00:00:00+03:00"
    assert rows[0][2] == 0.306
    assert rows[1][0].isoformat() == "2026-06-01T00:15:00+03:00"
    assert rows[1][2] == 1.25


def test_split_atr_cer():
    assert split_atr_cer("17990499/12.01.2024") == ("17990499", "12.01.2024")


def test_reading_channel_mapping():
    client = ReteleElectriceClient("user", "pass")
    assert client._reading_channel("INDEX ENERGIE ACTIVĂ ZONA ORARĂ 1 (KWH)") == (
        "active_import_zone_1",
        "1.8.1",
        "kWh",
    )
    assert client._reading_channel("ENERGIE ACTIVĂ PRODUSĂ, SPECIFICĂ CLIENȚILOR PROSUMATORI") == (
        "active_export",
        "2.8.0",
        "kWh",
    )

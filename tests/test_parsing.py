import asyncio
from pathlib import Path

from dso_retele_electrice.parsing import parse_load_curve_csv, split_atr_cer
from dso_retele_electrice.client import ReteleElectriceClient


class _FakeLoadCurvePage:
    def __init__(self):
        self.wait_arg = None
        self.selected_value = ""
        self.role_names = []
        self.download_clicked = False

    async def goto(self, _url, wait_until=None):
        return None

    async def wait_for_function(self, _expression, *, arg=None):
        self.wait_arg = arg

    async def wait_for_timeout(self, _timeout):
        return None

    def locator(self, selector):
        assert selector == "select"
        return _FakeSelects(self)

    def get_by_role(self, role, name):
        assert role == "button"
        self.role_names.append(name)
        return _FakeButton(self, name)

    def expect_download(self, timeout):
        assert timeout == 20_000
        return _FakeDownloadContext()


class _FakeSelects:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 3

    def nth(self, index):
        assert index == 2
        return _FakeEnergySelect(self.page)


class _FakeEnergySelect:
    def __init__(self, page):
        self.page = page

    async def evaluate(self, _expression, target):
        assert target == "energie activa consumata"
        return "active-import-option"

    async def select_option(self, value):
        self.page.selected_value = value


class _FakeButton:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    async def wait_for(self, state):
        assert state == "visible"

    async def click(self):
        pattern = getattr(self.name, "pattern", str(self.name))
        if "granularitate" in pattern:
            self.page.download_clicked = True


class _FakeDownloadContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    @property
    def value(self):
        async def download():
            return _FakeDownload()

        return download()


class _FakeDownload:
    suggested_filename = "load-curve.csv"

    async def save_as(self, target):
        Path(target).write_text('Zi;Q1\n"2026.06.01";"0,306000"\n', encoding="utf-8-sig")


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


def test_download_load_curve_uses_current_portal_controls():
    async def run():
        client = ReteleElectriceClient("user", "pass")
        page = _FakeLoadCurvePage()
        client._page = page
        text = await client._download_current_load_curve("RO001EXXXXXXXXX", "active_import")
        return page, text

    page, text = asyncio.run(run())
    assert page.wait_arg == "RO001EXXXXXXXXX"
    assert page.selected_value == "active-import-option"
    assert any(getattr(name, "pattern", "") == "^CAUT[ĂA]$" for name in page.role_names)
    assert page.download_clicked
    assert text.startswith("Zi;Q1")

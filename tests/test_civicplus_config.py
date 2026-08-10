"""CivicPlus discovery persistence is recoverable and multi-process safe."""

import json

import pytest

import vendors.adapters.civicplus_adapter_async as civicplus_module
from exceptions import VendorHTTPError
from vendors.adapters.civicplus_adapter_async import AsyncCivicPlusAdapter


class _Response:
    status = 200

    async def text(self):
        return "<html><a>Agenda Center</a></html>"


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(civicplus_module.config, "DB_DIR", str(tmp_path))
    return tmp_path


def test_site_config_updates_keep_prior_slugs_and_leave_no_shared_temp(config_dir):
    first = AsyncCivicPlusAdapter("first-city")
    second = AsyncCivicPlusAdapter("second-city")

    first._update_site_config({"domain": "first.example.gov"})
    second._update_site_config({"failed": True})

    sites = json.loads((config_dir / "civicplus_sites.json").read_text())
    assert sites == {
        "first-city": {"domain": "first.example.gov"},
        "second-city": {"failed": True},
    }
    assert not list(config_dir.glob(".civicplus_sites.*.tmp"))


@pytest.mark.asyncio
async def test_manual_domain_overrides_failed_marker_and_clears_it_on_success(config_dir):
    (config_dir / "civicplus_sites.json").write_text(
        json.dumps({"repaired-city": {"failed": True, "domain": "repaired.example.gov"}})
    )
    adapter = AsyncCivicPlusAdapter("repaired-city")
    calls = []

    async def get(url):
        calls.append(url)
        return _Response()

    adapter._get = get
    assert await adapter._find_agenda_url() == "https://repaired.example.gov/AgendaCenter"
    assert calls == ["https://repaired.example.gov/AgendaCenter"]
    sites = json.loads((config_dir / "civicplus_sites.json").read_text())
    assert sites["repaired-city"]["failed"] is False
    assert sites["repaired-city"]["failed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "status_code"),
    [("timeout", None), ("rate limited", 429)],
)
async def test_transient_probe_failures_do_not_create_a_permanent_tombstone(
    config_dir, message, status_code
):
    adapter = AsyncCivicPlusAdapter("temporarily-offline")
    adapter._get_candidate_base_urls = lambda: ["https://offline.example.gov"]

    async def unavailable(url):
        raise VendorHTTPError(
            message,
            vendor="civicplus",
            status_code=status_code,
            city_slug="temporarily-offline",
        )

    adapter._get = unavailable
    assert await adapter._find_agenda_url() is None
    assert not (config_dir / "civicplus_sites.json").exists()

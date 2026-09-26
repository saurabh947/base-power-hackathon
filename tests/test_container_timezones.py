"""The slim container image has no system timezone database, so zoneinfo must come from
the `tzdata` package. Without it, ERCOT local times (America/Chicago, US/Central) crash."""
import importlib.util
from zoneinfo import ZoneInfo


def test_tzdata_package_is_installed():
    assert importlib.util.find_spec("tzdata") is not None


def test_central_time_resolves():
    assert ZoneInfo("America/Chicago").key == "America/Chicago"
    assert ZoneInfo("US/Central").key == "US/Central"

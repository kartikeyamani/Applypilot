"""Regression test for apply/prompt.py::_build_location_check.

Bug this guards against: the wizard never wrote a top-level `location:
accept_patterns:` block to searches.yaml, so this function always fell back
to just the profile's single city -- meaning a user who answered "anywhere
in the USA" at `applypilot init` still had every hybrid/onsite job outside
their literal home city rejected as not_eligible_location.
"""

from applypilot.apply.prompt import _build_location_check

PROFILE = {"personal": {"city": "Phoenix"}}


def test_location_check_uses_accept_patterns_when_present():
    search_config = {
        "location": {
            "accept_patterns": ["usa", "United States", "New York", "Austin"],
        }
    }

    check = _build_location_check(PROFILE, search_config)

    for pattern in ["usa", "United States", "New York", "Austin"]:
        assert pattern in check


def test_location_check_falls_back_to_profile_city_when_no_patterns_configured():
    search_config = {}

    check = _build_location_check(PROFILE, search_config)

    assert "Phoenix" in check

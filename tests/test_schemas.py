"""Schema validation tests: valid/invalid params, masking, limits.

Source of truth for patterns/limits: supplied SIM Swap v1 Swagger schemas
(PhoneNumber ^\\+[1-9][0-9]{4,14}$, CreateCheckSimSwap.maxAge 1..2400).
"""

import pytest
from pydantic import ValidationError

from app.schemas import (
    APP_DEFAULT_LOOKBACK_HOURS,
    CAMARA_MAX_MAX_AGE,
    CAMARA_MIN_MAX_AGE,
    SPEC_DEFAULT_MAX_AGE,
    CheckRequest,
    RetrieveDateRequest,
    mask_phone,
)


def test_check_request_defaults():
    req = CheckRequest(phone_number="+346661113334")
    assert req.lookback_hours == 72
    assert req.lookback_hours == APP_DEFAULT_LOOKBACK_HOURS


def test_spec_default_max_age_documented():
    # v1/v2 CreateCheckSimSwap default when maxAge is omitted.
    assert (CAMARA_MIN_MAX_AGE, CAMARA_MAX_MAX_AGE) == (1, 2400)
    assert SPEC_DEFAULT_MAX_AGE == 240


def test_check_request_accepts_custom_window():
    req = CheckRequest(phone_number="+12025550100", lookback_hours=24)
    assert req.lookback_hours == 24


@pytest.mark.parametrize("hours", [1, 240, 2400])
def test_check_request_accepts_boundary_windows(hours):
    assert CheckRequest(phone_number="+346661113334", lookback_hours=hours).lookback_hours == hours


@pytest.mark.parametrize(
    "bad",
    [
        "12345",
        "abc",
        "+34",  # too short: needs 5-15 digits after '+'
        "346661113334",  # missing '+'
        "+0ABCDE12345",
        "",
        "+1",  # single digit below minimum length
        "+12345678901234567890",  # above maximum length
        "+ 346661113334",
    ],
)
def test_check_request_rejects_malformed_numbers(bad):
    with pytest.raises(ValidationError):
        CheckRequest(phone_number=bad)


@pytest.mark.parametrize("good", ["+346661113334", "+12025550100", "+10000", "+999999999999999"])
def test_check_request_accepts_spec_pattern_numbers(good):
    assert CheckRequest(phone_number=good).phone_number == good


@pytest.mark.parametrize("hours", [0, -1, 2401, 9999])
def test_check_request_rejects_out_of_range_window(hours):
    with pytest.raises(ValidationError):
        CheckRequest(phone_number="+346661113334", lookback_hours=hours)


def test_check_request_allows_missing_phone_for_three_legged():
    req = CheckRequest(lookback_hours=72)
    assert req.phone_number is None


def test_retrieve_request_validation():
    assert RetrieveDateRequest(phone_number="+346661113334").phone_number is not None
    with pytest.raises(ValidationError):
        RetrieveDateRequest(phone_number="not-a-number")


def test_mask_phone_never_returns_full_number():
    masked = mask_phone("+346661113334")
    assert masked is not None
    assert "+346661113334" not in masked
    assert masked.endswith("3334")
    assert mask_phone(None) is None

"""Shipping zones: where a parcel goes decides which price tier applies."""

from __future__ import annotations

DOMESTIC = "domestic"
REMOTE = "remote"
NEAR = "near"
INTERNATIONAL = "international"

ZONES = (DOMESTIC, REMOTE, NEAR, INTERNATIONAL)

_COUNTRY_ZONES = {"US": DOMESTIC, "CA": NEAR, "MX": NEAR}

# ZIP prefixes of Hawaii and Alaska, which carriers price as remote.
_REMOTE_PREFIXES = ("967", "968", "995", "996", "997", "998", "999")

DELIVERY_DAYS = {DOMESTIC: 3, REMOTE: 6, NEAR: 7, INTERNATIONAL: 12}


def zone_for(country: str, postcode: str) -> str:
    """The zone of a destination; countries that are not in the table are international."""
    code = country.strip().upper()
    try:
        zone = _COUNTRY_ZONES[code]
    except KeyError:
        return INTERNATIONAL
    if zone == DOMESTIC and postcode.strip().startswith(_REMOTE_PREFIXES):
        return REMOTE
    return zone


def delivery_days(country: str, postcode: str) -> int:
    return DELIVERY_DAYS[zone_for(country, postcode)]

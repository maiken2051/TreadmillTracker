"""Unit conversions. Storage and parsing are SI; these are for display only."""

M_PER_MILE = 1609.344
M_PER_FOOT = 0.3048


def mps_to_mph(v: float) -> float:
    return v * 3600 / M_PER_MILE


def m_to_miles(d: float) -> float:
    return d / M_PER_MILE


def m_to_feet(d: float) -> float:
    return d / M_PER_FOOT


def mps_to_min_per_mile(v: float) -> float | None:
    """Pace; None when standing still."""
    if v <= 0:
        return None
    return M_PER_MILE / v / 60

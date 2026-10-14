"""Shared pen-speed profiles: segment timing and pen-tip interpolation must use the same curve."""
import numpy as np

_TAU = np.linspace(0.0, 1.0, 513)
_MINJERK = 10 * _TAU**3 - 15 * _TAU**4 + 6 * _TAU**5


def distance_fraction(time_fraction, profile='linear'):
    """Fraction of stroke length covered after this fraction of the stroke's time."""
    time_fraction = np.clip(time_fraction, 0.0, 1.0)
    if profile == 'minjerk':
        return np.interp(time_fraction, _TAU, _MINJERK)
    return time_fraction


def time_fraction(distance_fraction_value, profile='linear'):
    """Inverse of distance_fraction: when the tip reaches this fraction of the stroke length."""
    distance_fraction_value = np.clip(distance_fraction_value, 0.0, 1.0)
    if profile == 'minjerk':
        return np.interp(distance_fraction_value, _MINJERK, _TAU)
    return distance_fraction_value

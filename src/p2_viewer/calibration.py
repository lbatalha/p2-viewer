"""Emissivity correction for the camera's radiometric values.

The camera's own settings have no practical effect: changing the emissivity,
distance, reflected temperature, atmospheric temperature or transmittance
does not change the radiometric half of the video stream. The values it
reports are the apparent scene temperature, as if every surface were a black
body.

That is right for a high-emissivity surface such as skin or matte paint,
and wrong for shiny metal or glass, which reflect the surroundings instead
of radiating their own heat.

For a grey body in front of a background at ``T_ref`` the measured radiance
satisfies

    T_apparent^4 = e * T_true^4 + (1 - e) * T_ref^4

so the true temperature is

    T_true = ((T_apparent^4 - (1 - e) * T_ref^4) / e) ** 0.25

Applying this is optional: the phone app may or may not do the same thing,
and that has to be checked against the app rather than assumed.
"""

from __future__ import annotations

import numpy as np

#: Ambient/reflected temperature used when the scene background is unknown.
DEFAULT_REFLECTED_TEMPERATURE = 300.0


def correct_celsius(
    temperatures: np.ndarray,
    emissivity: float,
    reflected_temperature: float = DEFAULT_REFLECTED_TEMPERATURE,
) -> np.ndarray:
    """Correct apparent Celsius temperatures for a grey body of `emissivity`.

    Always returns a float64 array (0-d for scalar input). Emissivity of 1.0
    leaves the values untouched.
    """
    if emissivity <= 0:
        raise ValueError("emissivity must be positive")
    if emissivity >= 1:
        return temperatures

    scale = np.asarray(temperatures, dtype=np.float64)
    apparent = scale + 273.15
    ref = float(reflected_temperature)

    radicand = (apparent**4 - (1.0 - emissivity) * ref**4) / emissivity
    # A surface reflecting a warm background can measure below the point
    # where the inversion is meaningful; clamp rather than return NaN.
    true_kelvin = np.maximum(radicand, 0.0) ** 0.25
    corrected = true_kelvin - 273.15

    return np.maximum(corrected, -273.15)

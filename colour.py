"""
Colour science for the develop pipeline: transfer functions and working-space constants.

Pixels travel through the app as float32 in 0..1 *encoded* with the transfer
function of the photo's output space (``srgb``, ``adobergb`` or ``prophoto``).
Edits that mean "light" (white balance, exposure, shadows, highlights, ...) are
applied after decoding to linear light and re-encoded with the same curve, see
``render_kernels.tone_stage`` and docs/COLOUR-PIPELINE.md.

The curves are the ones the embedded ICC profiles (``exporter.icc_profile_for``)
describe, so a colour-managed viewer decodes exactly what was encoded:

* sRGB: the exact IEC 61966-2-1 piecewise function (not the BT.709 curve that
  LibRaw's default ``gamma=(2.222, 4.5)`` produces),
* Adobe RGB (1998): a pure power law, 563/256 = 2.19921875,
* ProPhoto RGB: a pure power law, 1.8.
"""
from __future__ import annotations

import functools

import numpy as np

try:
    import cv2
except ImportError:           # pragma: no cover - the NumPy paths cover it
    cv2 = None

F32 = np.float32

SPACES = ("srgb", "adobergb", "prophoto")
DEFAULT_SPACE = "srgb"
ADOBE_GAMMA = 563.0 / 256.0
PROPHOTO_GAMMA = 1.8

# sRGB piecewise constants.
_SRGB_A = 0.055
_SRGB_ENC_THRESHOLD = 0.0031308     # on the linear side
_SRGB_DEC_THRESHOLD = 0.04045       # on the encoded side


def normalise_space(space) -> str:
    space = str(space or DEFAULT_SPACE).lower()
    return space if space in SPACES else DEFAULT_SPACE


def _pow(x: np.ndarray, p: float) -> np.ndarray:
    """``x ** p`` for float32 ``x >= 0`` (OpenCV is ~1.5x faster than NumPy here)."""
    if cv2 is not None and x.dtype == np.float32 and x.flags.c_contiguous and x.ndim <= 3 and x.size:
        shape = x.shape
        return cv2.pow(x.reshape(-1), p).reshape(shape)
    return np.power(x, F32(p), dtype=np.float32) if x.dtype == np.float32 else np.power(x, p)


# ---------------------------------------------------------------------------
# Transfer functions
# ---------------------------------------------------------------------------

def to_linear(enc: np.ndarray, space: str = DEFAULT_SPACE) -> np.ndarray:
    """Encoded 0..1 -> linear light (a new array; negative input is treated as 0)."""
    space = normalise_space(space)
    x = np.maximum(np.asarray(enc), 0)
    if x.dtype not in (np.float32, np.float64):
        x = x.astype(np.float32)
    x = np.ascontiguousarray(x)
    if space == "srgb":
        hi = _pow((x + x.dtype.type(_SRGB_A)) * x.dtype.type(1.0 / (1.0 + _SRGB_A)), 2.4)
        low = x < _SRGB_DEC_THRESHOLD
        if low.any():
            hi[low] = x[low] * x.dtype.type(1.0 / 12.92)
        return hi
    return _pow(x, ADOBE_GAMMA if space == "adobergb" else PROPHOTO_GAMMA)


def to_encoded(lin: np.ndarray, space: str = DEFAULT_SPACE) -> np.ndarray:
    """Linear light -> encoded (a new array; input is clipped to 0..1 first)."""
    space = normalise_space(space)
    x = np.clip(np.asarray(lin), 0, 1)
    if x.dtype not in (np.float32, np.float64):
        x = x.astype(np.float32)
    x = np.ascontiguousarray(x)
    if space == "srgb":
        hi = _pow(x, 1.0 / 2.4)
        hi *= x.dtype.type(1.0 + _SRGB_A)
        hi -= x.dtype.type(_SRGB_A)
        low = x <= _SRGB_ENC_THRESHOLD
        if low.any():
            hi[low] = x[low] * x.dtype.type(12.92)
        return hi
    return _pow(x, 1.0 / (ADOBE_GAMMA if space == "adobergb" else PROPHOTO_GAMMA))


def srgb_encode_exact(lin):
    """Float64 reference sRGB OETF, for tests and tables."""
    lin = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(lin <= _SRGB_ENC_THRESHOLD, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)


def srgb_decode_exact(enc):
    enc = np.clip(np.asarray(enc, dtype=np.float64), 0.0, 1.0)
    return np.where(enc <= _SRGB_DEC_THRESHOLD, enc / 12.92, np.power((enc + 0.055) / 1.055, 2.4))


@functools.lru_cache(maxsize=8)
def encode_lut16(space: str = DEFAULT_SPACE) -> np.ndarray:
    """float32 table: encoded value of every 16-bit linear code (exact, no 8-bit step)."""
    lin = np.arange(65536, dtype=np.float64) / 65535.0
    space = normalise_space(space)
    if space == "srgb":
        out = srgb_encode_exact(lin)
    else:
        out = np.power(lin, 1.0 / (ADOBE_GAMMA if space == "adobergb" else PROPHOTO_GAMMA))
    out = out.astype(np.float32)
    out.flags.writeable = False
    return out


def linear16_to_encoded(lin16: np.ndarray, space: str = DEFAULT_SPACE) -> np.ndarray:
    """uint16 linear-light image -> float32 encoded 0..1, exactly (table lookup)."""
    return np.take(encode_lut16(normalise_space(space)), lin16)


def encoded_to_u8(enc: np.ndarray) -> np.ndarray:
    """float32 encoded 0..1 -> uint8, rounding once (the only quantisation step)."""
    return np.clip(np.asarray(enc, dtype=np.float32) * F32(255.0) + F32(0.5), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# The old curve (edit version 1)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _libraw_gamma_constants(power: float = 1 / 2.222, slope: float = 4.5) -> tuple[float, float, float]:
    """LibRaw's ``gamma_curve`` constants for ``gamma=(2.222, 4.5)``: (g0, g1, knee, g4).

    Edits made before ``edit_version`` 2 were tuned on data encoded with this
    curve (BT.709: 4.5x below ~0.018, 1.099*x^0.45 - 0.099 above).
    """
    lo, hi = 0.0, 1.0
    knee = 0.0
    for _ in range(48):
        knee = (lo + hi) / 2.0
        ok = (np.power(knee / slope, -power) - 1.0) / power - 1.0 / knee > -1.0
        if ok:
            hi = knee
        else:
            lo = knee
    return power, slope, knee, knee * (1.0 / power - 1.0)


def legacy_encode(lin: np.ndarray) -> np.ndarray:
    """Linear light -> the BT.709-style curve the app used before edit version 2."""
    power, slope, knee, g4 = _libraw_gamma_constants()
    x = np.ascontiguousarray(np.clip(lin, 0, 1).astype(np.float32, copy=False))
    hi = _pow(x, power)
    hi *= F32(1.0 + g4)
    hi -= F32(g4)
    low = x < F32(knee / slope)
    if low.any():
        hi[low] = x[low] * F32(slope)
    return hi


def to_legacy(enc: np.ndarray, space: str = DEFAULT_SPACE) -> np.ndarray:
    """Encoded (this space) -> what the old pipeline would have started from."""
    return legacy_encode(to_linear(enc, space))


# ---------------------------------------------------------------------------
# White balance: colour temperature and tint as linear-light channel gains
# ---------------------------------------------------------------------------

_XYZ_TO_SRGB = np.array([[3.2404542, -1.5371385, -0.4985314],
                         [-0.9692660, 1.8760108, 0.0415560],
                         [0.0556434, -0.2040259, 1.0572252]])
LUMA = np.array([0.2126, 0.7152, 0.0722])
TEMP_RANGE = (1700.0, 25000.0)


def illuminant_rgb(kelvin: float) -> np.ndarray:
    """Linear sRGB of an illuminant of the given correlated colour temperature, G = 1.

    CIE daylight locus from 4000 K up, Planckian locus (Kim et al. cubic spline)
    below, which is the pair of loci Camera Raw's temperature scale follows.
    """
    t = float(np.clip(kelvin, *TEMP_RANGE))
    if t >= 4000.0:
        if t <= 7000.0:
            x = 0.244063 + 0.09911e3 / t + 2.9678e6 / t ** 2 - 4.6070e9 / t ** 3
        else:
            x = 0.237040 + 0.24748e3 / t + 1.9018e6 / t ** 2 - 2.0064e9 / t ** 3
        y = -3.0 * x * x + 2.870 * x - 0.275
    else:
        x = -0.2661239e9 / t ** 3 - 0.2343589e6 / t ** 2 + 0.8776956e3 / t + 0.179910
        if t < 2222.0:
            y = -1.1063814 * x ** 3 - 1.34811020 * x ** 2 + 2.18555832 * x - 0.20219683
        else:
            y = -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
    xyz = np.array([x / y, 1.0, (1.0 - x - y) / y])
    rgb = _XYZ_TO_SRGB @ xyz
    return rgb / rgb[1]


def temperature_gains(kelvin: float, baseline_kelvin: float) -> np.ndarray:
    """
    Linear-light RGB gains that move the white balance from the camera's
    (``baseline_kelvin``) to ``kelvin``: raising the temperature warms the picture.
    Neutralising an illuminant divides by its colour, so the gain is the ratio of
    the two illuminants' inverses; G is 1 and the result keeps white's luminance.
    """
    rel = illuminant_rgb(baseline_kelvin) / illuminant_rgb(kelvin)
    return _keep_white_luminance(rel)


def tint_gains(tint: float) -> np.ndarray:
    """Positive tint is magenta (green down), negative green. +-150 is +-0.5 stop of green."""
    s = float(tint) / 150.0
    return _keep_white_luminance(np.array([2.0 ** (0.12 * s), 2.0 ** (-0.5 * s), 2.0 ** (0.12 * s)]))


def local_temperature_gains(temp: float) -> np.ndarray:
    """A mask's relative Temp slider (-100..+100): +-0.5 stop of red against blue."""
    s = float(temp) / 100.0
    return _keep_white_luminance(np.array([2.0 ** (0.5 * s), 1.0, 2.0 ** (-0.5 * s)]))


def _keep_white_luminance(g: np.ndarray) -> np.ndarray:
    return g / float(LUMA @ g)

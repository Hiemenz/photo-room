"""
Colour pipeline reference checks (docs/COLOUR-PIPELINE.md).

Exposure is a gain in linear light, the output curve is the exact one the embedded
ICC profile describes, 8-bit exports use all 256 levels, 16-bit exports keep their
precision through Straighten, and a neutral edit changes nothing.
"""
import io
import struct

import numpy as np
import pytest
from PIL import Image, ImageCms

import colour
import editor
import editversion
import exporter
import masks
import render_kernels
import tonecurve

BASELINE = 5500
RAW_BASELINE = 4125         # what the fake raw's camera white balance (R/B = 4/3) maps to
SPACES = ("srgb", "adobergb", "prophoto")


def render(img, adj=None, space="srgb"):
    adj = {"edit_version": 2, "_space": space, **(adj or {})}
    return editor.apply_adjustments(img.astype(np.float32), BASELINE, adj)


def lin(x, space="srgb"):
    return colour.to_linear(np.asarray(x, dtype=np.float32), space)


def flat(v, h=16, w=16):
    return np.full((h, w, 3), v, dtype=np.float32)


def luma(x):
    return 0.2126 * x[..., 0] + 0.7152 * x[..., 1] + 0.0722 * x[..., 2]


# ---------------------------------------------------------------------------
# Transfer functions
# ---------------------------------------------------------------------------

def test_srgb_round_trip_is_exact():
    x = np.linspace(0, 1, 100001)
    f64 = colour.srgb_decode_exact(colour.srgb_encode_exact(x))
    assert np.abs(f64 - x).max() < 1e-12
    x32 = x.astype(np.float32)
    back = colour.to_linear(colour.to_encoded(x32))
    assert np.abs(back - x32).max() < 2e-6                      # float32 arithmetic only


@pytest.mark.parametrize("space", SPACES)
def test_encode_decode_round_trip_every_space(space):
    x = np.linspace(0, 1, 4097, dtype=np.float32)
    assert np.abs(colour.to_encoded(colour.to_linear(x, space), space) - x).max() < 3e-6


def test_srgb_matches_the_piecewise_definition_at_known_points():
    # IEC 61966-2-1: 12.92 * linear below 0.0031308, 1.055 * linear^(1/2.4) - 0.055 above.
    assert float(colour.srgb_encode_exact(0.0031308 / 2)) == pytest.approx(12.92 * 0.0031308 / 2)
    assert float(colour.srgb_encode_exact(0.5)) == pytest.approx(0.7353569830524495)
    assert float(colour.srgb_encode_exact(0.18)) == pytest.approx(0.4614, abs=1e-4)
    assert colour.to_encoded(np.array([0.5], np.float32))[0] == pytest.approx(0.7353569830524495, abs=1e-6)
    assert colour.to_linear(np.array([0.5], np.float32))[0] == pytest.approx(0.21404114, abs=1e-6)


def test_srgb_is_not_the_bt709_curve_rawpy_defaults_to():
    lin_mid = np.array([0.18], np.float32)
    srgb, bt709 = colour.to_encoded(lin_mid)[0], colour.legacy_encode(lin_mid)[0]
    assert abs(srgb - bt709) * 255 > 4        # the ~14-level mid-tone error in the review, at its smallest here


def test_srgb_curve_agrees_with_the_embedded_icc_profile():
    """Neutral greys encoded by us have the lightness a colour-managed viewer derives from the profile."""
    srgb = ImageCms.createProfile("sRGB")
    lab = ImageCms.createProfile("LAB")
    t = ImageCms.buildTransform(srgb, lab, "RGB", "LAB", renderingIntent=ImageCms.Intent.ABSOLUTE_COLORIMETRIC)
    codes = np.arange(256, dtype=np.uint8)
    grey = Image.fromarray(np.stack([codes] * 3, axis=-1)[None])
    L = np.asarray(ImageCms.applyTransform(grey, t))[0, :, 0].astype(np.float64) * 100.0 / 255.0
    y = colour.srgb_decode_exact(codes / 255.0)
    f = np.where(y > 216 / 24389, np.cbrt(y), (24389 / 27 * y + 16) / 116)
    expected = 116 * f - 16
    assert np.abs(L - expected).max() < 0.5          # 8-bit Lab quantisation is 0.4
    # ...which the BT.709 curve it replaces does not (it is up to ~5 L* off).
    bt = colour.legacy_encode(colour.srgb_decode_exact(codes / 255.0).astype(np.float32))
    assert np.abs((np.round(bt * 255) - codes)).max() > 8


def _icc_gamma(icc: bytes) -> float:
    i = icc.index(b"rTRC")
    off, size = struct.unpack(">II", icc[i + 4:i + 12])
    assert icc[off:off + 4] == b"curv"
    count, = struct.unpack(">I", icc[off + 8:off + 12])
    assert count == 1
    return struct.unpack(">H", icc[off + 12:off + 14])[0] / 256.0


def test_adobe_and_prophoto_profiles_use_the_gamma_the_pipeline_encodes_with():
    assert _icc_gamma(exporter.icc_profile_for("adobergb")) == pytest.approx(colour.ADOBE_GAMMA, abs=1 / 256)
    assert _icc_gamma(exporter.icc_profile_for("prophoto")) == pytest.approx(colour.PROPHOTO_GAMMA, abs=1 / 256)
    assert colour.ADOBE_GAMMA == pytest.approx(2.2, abs=0.01)
    assert colour.to_encoded(np.array([0.25], np.float32), "prophoto")[0] == pytest.approx(0.25 ** (1 / 1.8), abs=1e-6)
    assert colour.to_encoded(np.array([0.25], np.float32), "adobergb")[0] == pytest.approx(0.25 ** (1 / colour.ADOBE_GAMMA), abs=1e-6)


def test_linear16_table_is_exact():
    u16 = np.array([[0, 1, 100, 20000, 65535]], dtype=np.uint16)
    out = colour.linear16_to_encoded(u16)
    assert np.allclose(out, colour.srgb_encode_exact(u16 / 65535.0), atol=1e-7)


# ---------------------------------------------------------------------------
# Exposure and the light controls
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("space", SPACES)
@pytest.mark.parametrize("code", [0.05, 0.2, 0.3, 0.5])
def test_one_ev_doubles_linear_light(space, code):
    src = flat(code)
    out = render(src, {"exposure": 1.0, "color_temp": BASELINE}, space)
    gain = lin(out, space)[0, 0, 0] / lin(src, space)[0, 0, 0]
    assert gain == pytest.approx(2.0, rel=2e-3)


@pytest.mark.parametrize("ev", [-3.0, -1.0, -0.5, 0.5, 1.5])
def test_exposure_is_two_to_the_ev_in_linear_light(ev):
    src = flat(0.3)
    out = render(src, {"exposure": ev})
    assert lin(out)[0, 0, 0] / lin(src)[0, 0, 0] == pytest.approx(2.0 ** ev, rel=2e-3)


def test_exposure_uses_light_not_code_values_the_old_bug():
    """The review's proof: +1 EV on code 0.5 used to give a 4.67x linear gain."""
    out = render(flat(0.5), {"exposure": 1.0}, "srgb")
    gain = lin(out)[0, 0, 0] / lin(flat(0.5))[0, 0, 0]
    assert gain < 2.05
    assert gain == pytest.approx(2.0, rel=1e-3) or lin(out)[0, 0, 0] >= 0.99      # clipping only at the top


def test_exposure_headroom_highlights_can_be_pulled_back():
    """Exposure no longer clips before Highlights runs: +2 EV then Highlights -100 keeps tone."""
    src = flat(0.6)
    bright = render(src, {"exposure": 1.5})
    pulled = render(src, {"exposure": 1.5, "highlights": -100})
    assert pulled.mean() < bright.mean() - 0.05
    assert pulled.mean() < 0.999


def test_neutral_edit_is_identity():
    rng = np.random.default_rng(1)
    img = rng.random((40, 60, 3), dtype=np.float32)
    for space in SPACES:
        out = render(img, {"color_temp": BASELINE}, space)
        assert np.array_equal(out, img)
    # also when every slider is present at its neutral value
    neutral = {c["key"]: c["default"] for c in editor.CONTROLS}
    neutral["color_temp"] = BASELINE
    assert np.abs(render(img, neutral) - img).max() == 0


def test_a_zero_slider_pass_through_linear_light_is_still_lossless():
    """Decode -> encode with no change in between must not move a pixel by more than float rounding."""
    img = np.random.default_rng(2).random((64, 64, 3), dtype=np.float32)
    lin_img = colour.to_linear(img)
    assert np.abs(colour.to_encoded(lin_img) - img).max() < 3e-6


def test_white_balance_warms_with_temperature_and_is_neutral_at_the_baseline():
    grey = flat(0.4)
    warm = render(grey, {"color_temp": 7500})
    cool = render(grey, {"color_temp": 3800})
    assert warm[0, 0, 0] > warm[0, 0, 2] and cool[0, 0, 0] < cool[0, 0, 2]
    # white keeps its luminance, so the picture does not change brightness
    assert luma(lin(warm))[0, 0] == pytest.approx(luma(lin(grey))[0, 0], rel=2e-3)
    assert np.array_equal(render(grey, {"color_temp": BASELINE}), grey)
    # green is the reference: it moves least
    assert abs(warm[0, 0, 1] - 0.4) < abs(warm[0, 0, 0] - 0.4)


def test_tint_positive_is_magenta_and_negative_is_green():
    grey = flat(0.4)
    assert render(grey, {"tint": 60})[0, 0, 1] < 0.4 < render(grey, {"tint": -60})[0, 0, 1]


def test_shadows_lift_darks_and_leave_highlights():
    ramp = np.tile(np.linspace(0.02, 0.98, 97, dtype=np.float32)[None, :, None], (4, 1, 3))
    out = render(ramp, {"shadows": 100})
    assert (out[0, :20, 0] > ramp[0, :20, 0] + 0.02).all()
    assert np.abs(out[0, 80:, 0] - ramp[0, 80:, 0]).max() < 1e-4


def test_highlights_negative_darkens_bright_and_leaves_shadows():
    ramp = np.tile(np.linspace(0.02, 0.98, 97, dtype=np.float32)[None, :, None], (4, 1, 3))
    out = render(ramp, {"highlights": -100})
    assert (out[0, 80:, 0] < ramp[0, 80:, 0] - 0.03).all()
    assert np.abs(out[0, :30, 0] - ramp[0, :30, 0]).max() < 1e-4


def test_whites_and_blacks_move_the_ends_of_the_range():
    ramp = np.tile(np.linspace(0.0, 1.0, 257, dtype=np.float32)[None, :, None], (2, 1, 3))
    whites = render(ramp, {"whites": 100})
    assert whites[0, -30:-5, 0].mean() > ramp[0, -30:-5, 0].mean()
    assert np.abs(whites[0, :100, 0] - ramp[0, :100, 0]).max() < 1e-4
    blacks_up = render(ramp, {"blacks": 100})
    assert blacks_up[0, 2, 0] > ramp[0, 2, 0] + 0.02
    assert np.abs(blacks_up[0, 160:, 0] - ramp[0, 160:, 0]).max() < 1e-4
    crushed = render(ramp, {"blacks": -100})
    assert crushed[0, 1, 0] <= 1e-6 and crushed[0, 120, 0] == pytest.approx(ramp[0, 120, 0], abs=2e-3)


def test_range_controls_keep_hue():
    px = np.zeros((4, 4, 3), np.float32)
    px[...] = (0.30, 0.15, 0.08)
    out = render(px, {"shadows": 80, "highlights": -40})
    before = lin(px)[0, 0]
    after = lin(out)[0, 0]
    assert (after / after[1]) == pytest.approx(before / before[1], rel=2e-3)


def test_contrast_is_an_s_curve_about_mid_grey_and_never_clips():
    ramp = np.tile(np.linspace(0, 1, 513, dtype=np.float32)[None, :, None], (2, 1, 3))
    up = render(ramp, {"contrast": 100})[0, :, 0]
    down = render(ramp, {"contrast": -100})[0, :, 0]
    assert up[256] == pytest.approx(0.5, abs=2e-3)
    assert up[0] == pytest.approx(0, abs=1e-6) and up[-1] == pytest.approx(1, abs=1e-6)
    assert up[128] < ramp[0, 128, 0] and up[384] > ramp[0, 384, 0]
    assert down[128] > ramp[0, 128, 0] and down[384] < ramp[0, 384, 0]
    assert (np.diff(up) > 0).all() and (np.diff(down) > 0).all()        # strictly monotonic: no clipped plateaus
    assert (np.diff(up)[:3] > 0).all()
    # the two directions undo each other
    both = render(np.tile(up[None, :, None], (2, 1, 3)), {"contrast": -100})[0, :, 0]
    assert np.abs(both - ramp[0, :, 0]).max() < 5e-3


def test_vibrance_and_saturation_still_behave():
    px = np.zeros((8, 8, 3), np.float32)
    px[...] = (0.55, 0.45, 0.4)
    sat = render(px, {"saturation": 50})
    assert (sat[0, 0, 0] - sat[0, 0, 2]) > (px[0, 0, 0] - px[0, 0, 2])
    grey = render(px, {"saturation": -100})
    assert np.ptp(grey[0, 0]) < 1e-4
    vib = render(px, {"vibrance": 60})
    assert (vib[0, 0, 0] - vib[0, 0, 2]) > (px[0, 0, 0] - px[0, 0, 2])
    saturated = np.zeros((8, 8, 3), np.float32)
    saturated[...] = (0.9, 0.2, 0.1)
    gain_low = (vib[0, 0, 0] - vib[0, 0, 2]) / (px[0, 0, 0] - px[0, 0, 2])
    vib_hi = render(saturated, {"vibrance": 60})
    gain_hi = (vib_hi[0, 0, 0] - vib_hi[0, 0, 2]) / (saturated[0, 0, 0] - saturated[0, 0, 2])
    assert gain_low > gain_hi             # vibrance favours the muted colours


def test_tone_curve_still_maps_points():
    curve = {"points": {"rgb": [[0, 0], [128, 160], [255, 255]]}}
    out = render(flat(128 / 255), {"tone_curve": curve})
    assert out[0, 0, 0] == pytest.approx(160 / 255, abs=2e-3)
    assert render(flat(0.0), {"tone_curve": curve})[0, 0, 0] == pytest.approx(0, abs=1e-5)
    assert render(flat(1.0), {"tone_curve": curve})[0, 0, 0] == pytest.approx(1, abs=1e-5)


def test_tone_curve_keeps_sixteen_bit_resolution():
    ramp = np.tile(np.linspace(0, 1, 6000, dtype=np.float32)[None, :, None], (1, 1, 3))
    curve = {"points": {"rgb": [[0, 0], [100, 80], [255, 255]]}}
    out = render(ramp, {"tone_curve": curve, "contrast": 30})
    assert len(np.unique(np.round(out[0, :, 0] * 65535))) > 5500


def test_hsl_still_shifts_only_its_band():
    img = np.zeros((8, 16, 3), np.float32)
    img[:, :8] = (0.8, 0.2, 0.2)       # red
    img[:, 8:] = (0.2, 0.3, 0.8)       # blue
    out = render(img, {"sat_red": -100})
    assert np.ptp(out[0, 0]) < np.ptp(img[0, 0]) * 0.5
    assert np.allclose(out[0, 12], img[0, 12], atol=2e-3)


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------

def _mask(adj, **kw):
    return {"type": "linear", "x": 0.5, "y": 0.5, "angle": 90, "feather": 0, "adj": adj, "enabled": True, **kw}


def test_local_exposure_is_a_stop_of_light_like_the_global_slider():
    src = flat(0.3, 32, 32)
    inside = {"exposure": 1.0}
    out = masks.apply_local(src, inside, space="srgb")
    assert lin(out)[0, 0, 0] / lin(src)[0, 0, 0] == pytest.approx(2.0, rel=2e-3)
    glob = render(src, {"exposure": 1.0})
    assert np.abs(out - glob).max() < 1e-5


def test_masks_blend_only_where_painted():
    src = np.tile(np.linspace(0.1, 0.6, 64, dtype=np.float32)[None, :, None], (64, 1, 3))
    m = {"type": "radial", "x": 0.25, "y": 0.5, "w": 0.2, "h": 0.4, "feather": 20, "invert": False,
         "enabled": True, "adj": {"exposure": 1.0}}
    out = render(src, {"masks": [m]})
    assert (out[:, 14:18] > src[:, 14:18] + 0.05).all() or out[32, 16, 0] > src[32, 16, 0] + 0.05
    assert np.abs(out[:, 60:] - src[:, 60:]).max() < 1e-5


def test_masks_keep_sixteen_bit_precision():
    ramp = np.tile(np.linspace(0, 1, 6000, dtype=np.float32)[None, :, None], (8, 1, 3))
    m = {"type": "radial", "x": 0.5, "y": 0.5, "w": 0.9, "h": 0.9, "feather": 50, "invert": False,
         "enabled": True, "adj": {"exposure": 0.5, "contrast": 20}}
    out = render(ramp, {"masks": [m]})
    assert len(np.unique(np.round(out[4, :, 0] * 65535))) > 5000


def test_legacy_masks_keep_the_old_gamma_space_exposure():
    src = flat(0.3, 8, 8)
    old = masks.apply_local(src, {"exposure": 1.0}, legacy=True)
    assert old[0, 0, 0] == pytest.approx(0.6, abs=1e-5)         # code value x2, the version 1 maths


# ---------------------------------------------------------------------------
# Precision: 8-bit levels, 16-bit through Straighten
# ---------------------------------------------------------------------------

def ramp_row(n=4096, h=8):
    return np.tile(np.linspace(0, 1, n, dtype=np.float32)[None, :, None], (h, 1, 3))


def test_straighten_keeps_16_bit_precision():
    """The review: a 2000-step ramp kept 256 levels at 1 degree. It must keep ~16 bits of accuracy."""
    ramp = np.tile(np.linspace(0, 1, 2000, dtype=np.float32)[None, :, None], (400, 1, 3))
    plain = render(ramp, {"color_temp": BASELINE})
    tilted = render(ramp, {"color_temp": BASELINE, "straighten": 1.0})
    u = lambda a: len(np.unique(np.round(a[:, :, 0] * 65535)))          # noqa: E731
    assert u(plain) == 2000
    assert u(tilted) > 6 * 256                       # was 256 when the rotation went through uint8
    # accuracy against a float64 reference rotation (Pillow's mode F), in 16-bit codes
    h, w = ramp.shape[:2]
    ref = np.asarray(Image.fromarray(ramp[:, :, 0].copy(), "F").rotate(1.0, resample=Image.BICUBIC))
    rotated = editor._rotate_float(ramp, 1.0)[:, :, 0]
    inner = (slice(20, h - 20), slice(40, w - 40))
    assert np.abs(rotated[inner] - ref[inner]).max() * 65535 < 2.5


def test_transform_and_geometry_stay_float():
    ramp = np.tile(np.linspace(0, 1, 3000, dtype=np.float32)[None, :, None], (300, 1, 3))
    out = render(ramp, {"straighten": 0.5, "crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9},
                        "xf_rotate": 0.4})
    assert len(np.unique(np.round(out[:, :, 0] * 65535))) > 6 * 256


def test_full_ramp_through_edits_quantises_to_all_256_levels():
    out = render(ramp_row(), {"exposure": 0.0, "color_temp": BASELINE})
    assert len(np.unique(render_kernels.float_to_u8(out)[0, :, 0])) == 256
    edited = render(ramp_row(), {"exposure": -0.5, "shadows": 40, "contrast": 20})
    codes = render_kernels.float_to_u8(edited)[0, :, 0]
    assert len(np.unique(codes)) == int(codes.max()) + 1          # every level up to the new white, no gaps
    assert (np.diff(edited[0, :, 0]) >= -1e-6).all()


# ---------------------------------------------------------------------------
# Export end to end (fake raw returning a smooth 16-bit linear ramp)
# ---------------------------------------------------------------------------

class _RampRaw:
    camera_whitebalance = [2.0, 1.0, 1.5, 1.0]

    def __init__(self, width=4096, height=16, scale=1.0):
        ramp = (np.linspace(0, 1, width) * scale * 65535).astype(np.uint16)
        self.rgb = np.tile(ramp[None, :, None], (height, 1, 3))
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self, **kw):
        self.calls.append(kw)
        assert tuple(kw["gamma"]) == (1, 1) and kw["output_bps"] == 16, "the decode must be linear 16-bit"
        return self.rgb.copy()


@pytest.fixture
def ramp_raw(tmp_path, monkeypatch, fake_exiftool):
    import rawpy
    raw = _RampRaw(height=600)          # tall enough that a 1 degree Straighten keeps a real picture
    monkeypatch.setattr(rawpy, "imread", lambda p: raw)
    path = tmp_path / "ramp.CR2"
    path.write_bytes(b"x")
    return path, raw


def _read(path):
    import cv2
    return cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[:, :, ::-1]


def test_8bit_export_of_a_linear_ramp_uses_all_256_levels(ramp_raw, tmp_path):
    path, raw = ramp_raw
    out = exporter.render(path, exporter.ExportOptions(), adjustments={"color_temp": RAW_BASELINE})[0]
    row = np.asarray(out)[0, :, 0]
    assert len(np.unique(row)) == 256
    # the codes are the exact sRGB encoding of the linear ramp
    expected = np.round(colour.srgb_encode_exact(np.linspace(0, 1, 4096)) * 255)
    assert np.abs(row.astype(int) - expected).max() <= 1


def test_8bit_export_with_exposure_and_shadows_keeps_most_levels(ramp_raw):
    """The review's example (exposure +2, shadows +50) kept 129 of 256 levels."""
    path, _ = ramp_raw
    out = np.asarray(exporter.render(path, exporter.ExportOptions(),
                                     adjustments={"exposure": 2.0, "shadows": 50, "color_temp": RAW_BASELINE})[0])
    n = len(np.unique(out[0, :, 1]))
    assert n >= 200
    assert (np.diff(out[0, :, 1].astype(int)) >= 0).all()


def test_16bit_png_export_keeps_precision_through_straighten(ramp_raw, tmp_path):
    path, _ = ramp_raw
    opts = exporter.ExportOptions(format="png", bit_depth=16).validate()
    arr16, icc = exporter._render_array(path, opts, adjustments={"straighten": 1.0, "color_temp": RAW_BASELINE})
    dest = tmp_path / "o.png"
    exporter.write_png16(dest, arr16, icc)
    arr = _read(dest)
    assert arr.dtype == np.uint16
    assert len(np.unique(arr[arr.shape[0] // 2, 20:-20, 0])) > 6 * 256        # an 8-bit rotate leaves 256


def test_export_embeds_the_profile_that_matches_the_curve(ramp_raw, tmp_path):
    path, raw = ramp_raw
    for space in SPACES:
        _, icc = exporter.render(path, exporter.ExportOptions(colorspace=space), adjustments={"color_temp": RAW_BASELINE})
        assert icc == exporter.icc_profile_for(space)
        out = np.asarray(exporter.render(path, exporter.ExportOptions(colorspace=space),
                                         adjustments={"color_temp": RAW_BASELINE})[0])[0, :, 0]
        expected = np.round(colour.to_encoded(np.linspace(0, 1, 4096, dtype=np.float32), space) * 255)
        assert np.abs(out.astype(int) - expected).max() <= 1
    assert raw.calls, "the raw decoder was used"


# ---------------------------------------------------------------------------
# Edit versions
# ---------------------------------------------------------------------------

def synthetic(h=240, w=360, seed=5):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([x / w, y / h, (x + y) / (w + h)], 2) * 0.8 + 0.05
    img += rng.normal(0, 0.02, (h, w, 3)).astype(np.float32)
    return np.clip(img, 0, 1).astype(np.float32)


def test_unversioned_edits_are_version_one_and_render_with_the_old_maths():
    img = synthetic()
    adj = {"exposure": 0.7, "contrast": 20, "shadows": 30, "color_temp": 6200}
    resolved = editor.resolve_edit_version(adj, BASELINE)
    assert resolved["edit_version"] == 1
    assert editor.resolve_edit_version({"color_temp": BASELINE}, BASELINE)["edit_version"] == editversion.CURRENT
    old = editor.apply_adjustments(img, BASELINE, resolved)
    new = editor.apply_adjustments(img, BASELINE, {**resolved, "edit_version": 2})
    assert np.abs(old - new).mean() * 255 > 8        # genuinely different maths...
    # ...and the old one is untouched: it is exactly the frozen original
    import legacy_pipeline as L
    full = editor.effective_adjustments(resolved)
    x = L.legacy_stage_tone(colour.to_legacy(img), BASELINE, full)
    x = L.legacy_stage_saturation(x, full)
    assert np.abs(old - x).max() <= 1 / 255


def test_legacy_decode_matches_rawpys_default_curve():
    """Version 1 renders start from linear -> BT.709, which must be what rawpy's default decode was."""
    import os
    from pathlib import Path
    sample = Path(os.environ.get("PHOTO_ROOM_RAW_SAMPLE", Path.home() / ".cache/photo-room-test-raw/IMG_9406.CR2"))
    if os.environ.get("PHOTO_ROOM_REAL_RAW") != "1" and not sample.exists():
        pytest.skip("real raw not available")
    if not sample.exists():
        pytest.skip("real raw not available")
    import rawpy
    import process
    with rawpy.imread(str(sample)) as raw:
        old = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8, half_size=True)
        lin16 = process.decode_linear(raw, half_size=True)
    new = np.rint(colour.to_legacy(colour.linear16_to_encoded(lin16)) * 255)
    assert np.abs(new - old.astype(float)).max() <= 2
    assert np.abs(new - old.astype(float)).mean() < 0.3


@pytest.mark.parametrize("adj", [
    {"exposure": 0.8},
    {"exposure": -0.6, "contrast": 25},
    {"exposure": 0.4, "shadows": 40, "highlights": -30, "contrast": 15},
    {"exposure": 0.5, "color_temp": 6600, "tint": 10, "saturation": 15},
])
def test_upgrade_keeps_the_look(adj):
    img = synthetic()
    old = {**adj, "color_temp": adj.get("color_temp", BASELINE), "edit_version": 1}
    upgraded, report = editversion.upgrade(old, BASELINE, img, lambda b, t, a: editor.apply_adjustments(b, t, a))
    assert upgraded["edit_version"] == 2 and report["upgraded"]
    before = editor.apply_adjustments(img, BASELINE, old)
    after = editor.apply_adjustments(img, BASELINE, upgraded)
    naive = editor.apply_adjustments(img, BASELINE, {**old, "edit_version": 2})
    err_after = np.abs(before - after).mean() * 255
    err_naive = np.abs(before - naive).mean() * 255
    assert err_after < err_naive * 0.7
    assert err_after < 10.0                             # documented: a few to ~10 of 255 levels
    assert report["mean_difference_after"] <= report["mean_difference_before"]
    assert report["mean_difference_after"] == pytest.approx(err_after, abs=0.6)


def test_upgrade_is_a_no_op_for_current_edits():
    adj = {"exposure": 0.5, "edit_version": 2}
    out, report = editversion.upgrade(adj, BASELINE, synthetic(), lambda b, t, a: b)
    assert out == adj and report["upgraded"] is False


def test_exposure_remap_matches_the_linear_gain_of_the_old_maths():
    mapped = editversion.map_parameters({"exposure": 1.0, "color_temp": BASELINE}, BASELINE)
    assert mapped["exposure"] == pytest.approx(2.2)
    assert mapped["edit_version"] == 2


def test_creator_tool_round_trip_and_inference():
    assert editversion.version_from_tool(editversion.tool_label(2)) == 2
    assert editversion.version_from_tool("photo-room (edit v1)") == 1
    assert editversion.version_from_tool("Adobe Lightroom") is None
    assert editversion.version_for_stored("Adobe Photoshop Lightroom Classic", True) == 2
    assert editversion.version_for_stored(None, True, has_crs_version=True) == 2
    assert editversion.version_for_stored(None, True) == 1                 # ours, before versions
    assert editversion.version_for_stored(None, False) == 2


def test_auto_exposure_is_measured_in_light():
    import process
    from pathlib import Path
    # a flat grey photo whose encoded median is 0.2: 0.40 is 2.0 stops of light brighter
    rgb = np.full((64, 64, 3), 51, np.uint8)
    out = process.compute_auto_adjustments(Path("x.cr2"), decoded=(rgb, 5500, np.array([2, 1, 1.5, 1.0])), iso=100)
    expected = np.log2(colour.srgb_decode_exact(0.40) / colour.srgb_decode_exact(51 / 255))
    assert out["exposure"] == pytest.approx(round(float(expected) * 4) / 4)

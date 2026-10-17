"""LUT parsing, interpolation accuracy, colour-space handling and the LUT library."""

import itertools

import numpy as np
import pytest

import colour
import lut as L


def cube_text(n, fn, title="t", extra=""):
    """A 3D .cube whose entry at (r, g, b) grid point is fn(r, g, b) in 0..1."""
    g = np.linspace(0, 1, n)
    rows = [f"{fn(r, gg, b)[0]:.8f} {fn(r, gg, b)[1]:.8f} {fn(r, gg, b)[2]:.8f}"
            for b in g for gg in g for r in g]       # red varies fastest
    return f'TITLE "{title}"\n{extra}LUT_3D_SIZE {n}\n' + "\n".join(rows) + "\n"


def rand_rgb(n=4000, seed=0):
    return np.random.default_rng(seed).random((n, 3)).astype(np.float32)


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

def test_parse_3d_and_red_varies_fastest():
    lut = L.parse_cube(cube_text(3, lambda r, g, b: (r, g * 0.5, b)))
    assert lut.kind == "3d" and lut.size == 3 and lut.title == "t"
    assert lut.table.shape == (3, 3, 3, 3)
    # index order is [blue][green][red]
    assert lut.table[2, 1, 0].tolist() == pytest.approx([0.0, 0.25, 1.0])


def test_parse_1d_with_domain_and_comments():
    text = "# comment\nTITLE \"x\"\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\nLUT_1D_SIZE 3\n0 0 0\n0.5 0.5 0.5 # mid\n1 1 1\n"
    lut = L.parse_cube(text)
    assert lut.kind == "1d" and lut.domain_max.tolist() == [2, 2, 2]
    assert lut.sample(np.array([[1.0, 1.0, 1.0]], np.float32))[0].tolist() == pytest.approx([0.5] * 3)


@pytest.mark.parametrize("text,fragment", [
    ("", "no LUT_1D_SIZE"),
    ("LUT_3D_SIZE 2\n0 0 0\n", "expected 8 data rows"),
    ("LUT_3D_SIZE 1\n0 0 0\n", "outside"),
    ("LUT_3D_SIZE 500\n", "outside"),
    ("LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n", "both"),
    ("LUT_1D_SIZE 2\nLUT_1D_SIZE 2\n", "twice"),
    ("LUT_1D_SIZE 2\n0 0 0\n1 1\n", "three numbers"),
    ("LUT_1D_SIZE 2\n0 0 0\n1 1 x\n", "not a number"),
    ("LUT_1D_SIZE 2\n0 0 0\n1 1 nan\n", "NaN|not finite|not a number"),
    ("LUT_1D_SIZE 2\n0 0 0\n1 1 inf\n", "NaN|infinite|not finite"),
    ("LUT_1D_SIZE two\n", "not a number"),
    ("LUT_1D_SIZE 2.5\n", "whole number"),
    ("LUT_1D_SIZE 2\nDOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n0 0 0\n1 1 1\n", "DOMAIN_MAX"),
    ("LUT_1D_SIZE 2\nBOGUS 3\n0 0 0\n1 1 1\n", "unknown keyword"),
])
def test_malformed_files_are_refused_with_a_reason(text, fragment):
    with pytest.raises(L.LutError, match=fragment):
        L.parse_cube(text)


def test_binary_and_oversized_input_is_refused():
    with pytest.raises(L.LutError, match="not text"):
        L.parse_cube(b"\xff\xfe\x00\x00\x80")
    with pytest.raises(L.LutError, match="64 MB"):
        L.parse_cube(b"#" * (L.MAX_BYTES + 1))


def test_cube_round_trips_through_text():
    lut = L.builtin("builtin-warm-negative")
    again = L.parse_cube(L.to_cube(lut))
    assert again.size == lut.size
    assert np.abs(again.table - lut.table).max() < 1e-6
    lin = L.parse_cube(L.to_cube(L.identity("1d", 5)), space="linear")
    assert L.parse_cube(L.to_cube(lin)).space == "linear"       # the space comment survives


# ---------------------------------------------------------------------------
# Interpolation accuracy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", L.METHODS)
@pytest.mark.parametrize("kind,size", [("3d", 2), ("3d", 17), ("3d", 33), ("1d", 2), ("1d", 64)])
def test_identity_lut_is_identity(method, kind, size):
    lut = L.identity(kind, size)
    x = rand_rgb()
    assert np.abs(lut.sample(x, method) - x).max() < 2e-6
    assert lut.is_identity()


@pytest.mark.parametrize("method", L.METHODS)
def test_grid_points_are_exact(method):
    n = 9
    fn = lambda r, g, b: (r ** 2, np.sqrt(g), 1 - b * r)    # noqa: E731 - arbitrary, non-affine
    lut = L.parse_cube(cube_text(n, fn))
    g = np.linspace(0, 1, n)
    pts = np.array(list(itertools.product(g, g, g)), np.float32)         # (r, g, b) combos
    want = np.array([fn(*p) for p in pts], np.float32)
    assert np.abs(lut.sample(pts, method) - want).max() < 2e-6


@pytest.mark.parametrize("method", L.METHODS)
def test_affine_luts_are_reproduced_exactly_between_grid_points(method):
    """Invert + channel mix is affine, and both schemes reproduce affine maps exactly."""
    fn = lambda r, g, b: (1 - r, 0.5 * g + 0.25 * r, 0.2 + 0.6 * b - 0.1 * g)    # noqa: E731
    lut = L.parse_cube(cube_text(5, fn))
    x = rand_rgb(3000, 1)
    want = np.stack([1 - x[:, 0], 0.5 * x[:, 1] + 0.25 * x[:, 0], 0.2 + 0.6 * x[:, 2] - 0.1 * x[:, 1]], axis=1)
    assert np.abs(lut.sample(x, method) - want).max() < 3e-6


def test_tetrahedral_matches_a_brute_force_reference():
    """Six-case textbook tetrahedral interpolation, written independently."""
    n = 6
    rng = np.random.default_rng(3)
    table = rng.random((n, n, n, 3)).astype(np.float32)
    lut = L.Lut("3d", n, table)
    x = rand_rgb(500, 4)

    def ref(p):
        s = p * (n - 1)
        i = np.minimum(s.astype(int), n - 2)
        fr, fg, fb = s - i
        c = lambda dr, dg, db: table[i[2] + db, i[1] + dg, i[0] + dr]    # noqa: E731
        if fr >= fg >= fb:
            return c(0, 0, 0) + fr * (c(1, 0, 0) - c(0, 0, 0)) + fg * (c(1, 1, 0) - c(1, 0, 0)) + fb * (c(1, 1, 1) - c(1, 1, 0))
        if fr >= fb > fg:
            return c(0, 0, 0) + fr * (c(1, 0, 0) - c(0, 0, 0)) + fb * (c(1, 0, 1) - c(1, 0, 0)) + fg * (c(1, 1, 1) - c(1, 0, 1))
        if fb > fr >= fg:
            return c(0, 0, 0) + fb * (c(0, 0, 1) - c(0, 0, 0)) + fr * (c(1, 0, 1) - c(0, 0, 1)) + fg * (c(1, 1, 1) - c(1, 0, 1))
        if fg > fr >= fb:
            return c(0, 0, 0) + fg * (c(0, 1, 0) - c(0, 0, 0)) + fr * (c(1, 1, 0) - c(0, 1, 0)) + fb * (c(1, 1, 1) - c(1, 1, 0))
        if fg >= fb > fr:
            return c(0, 0, 0) + fg * (c(0, 1, 0) - c(0, 0, 0)) + fb * (c(0, 1, 1) - c(0, 1, 0)) + fr * (c(1, 1, 1) - c(0, 1, 1))
        return c(0, 0, 0) + fb * (c(0, 0, 1) - c(0, 0, 0)) + fg * (c(0, 1, 1) - c(0, 0, 1)) + fr * (c(1, 1, 1) - c(0, 1, 1))

    want = np.array([ref(p) for p in x])
    assert np.abs(lut.sample(x, "tetrahedral") - want).max() < 1e-5


def test_smooth_lut_error_shrinks_quadratically_with_size():
    """A 33-point sampling of a smooth curve is within a fraction of an 8-bit level."""
    fn = lambda r, g, b: (r ** 2.2, (g + 0.1) ** 0.45, 0.5 * (r + b) ** 1.5 / 2 ** 0.5)    # noqa: E731
    x = rand_rgb(5000, 5)
    want = np.stack([x[:, 0] ** 2.2, (x[:, 1] + 0.1) ** 0.45, 0.5 * (x[:, 0] + x[:, 2]) ** 1.5 / 2 ** 0.5], axis=1)
    errs = {}
    for n in (9, 17, 33):
        errs[n] = np.abs(L.parse_cube(cube_text(n, fn)).sample(x, "tetrahedral") - want)
    assert errs[33].max() < 1 / 255 * 0.5
    assert errs[17].mean() < errs[9].mean() * 0.5 and errs[33].mean() < errs[17].mean() * 0.5


def test_tetrahedral_keeps_the_grey_axis_neutral():
    """A LUT that tints saturated colours but leaves neutrals alone must not tint greys."""
    n = 17

    def fn(r, g, b):
        sat = max(r, g, b) - min(r, g, b)
        return (min(1, r + 0.2 * sat), g, b)

    lut = L.parse_cube(cube_text(n, fn))
    grey = np.repeat(np.linspace(0, 1, 301, dtype=np.float32)[:, None], 3, axis=1)
    assert np.abs(lut.sample(grey, "tetrahedral") - grey).max() < 2e-6


def test_one_d_lut_is_linear_between_entries_and_clamps():
    lut = L.parse_cube("LUT_1D_SIZE 3\n0 0 0\n0.8 0.8 0.8\n1 1 1\n")
    out = lut.sample(np.array([[0.25, 0.5, 1.5], [-1.0, 0.75, 0.0]], np.float32))
    assert out[0].tolist() == pytest.approx([0.4, 0.8, 1.0])
    assert out[1].tolist() == pytest.approx([0.0, 0.9, 0.0])


def test_domain_scaling_maps_the_input_range():
    lut = L.parse_cube("DOMAIN_MIN 0 0 0\nDOMAIN_MAX 0.5 0.5 0.5\nLUT_1D_SIZE 2\n0 0 0\n1 1 1\n")
    out = lut.sample(np.array([[0.25, 0.5, 0.75]], np.float32))[0]
    assert out.tolist() == pytest.approx([0.5, 1.0, 1.0])


# ---------------------------------------------------------------------------
# Applying to an image: colour spaces, intensity
# ---------------------------------------------------------------------------

def test_identity_lut_leaves_an_image_alone_in_every_output_space():
    img = np.random.default_rng(6).random((32, 40, 3)).astype(np.float32) * 0.7 + 0.15     # in every gamut
    for space in colour.SPACES:
        for lut in (L.identity("3d", 17), L.identity("1d", 256)):
            out = L.apply(img, lut, space)
            assert np.abs(out - img).max() < 2e-3, (space, lut.kind)
        lin = L.identity("1d", 4096)
        lin.space = "linear"
        assert np.abs(L.apply(img, lin, space) - img).max() < 2e-3, space


def test_primaries_matrices_send_white_to_white_and_agree_with_known_values():
    for space in ("adobergb", "prophoto"):
        assert L.to_srgb_matrix(space) @ np.ones(3) == pytest.approx([1, 1, 1], abs=1e-4)
    # Adobe RGB pure green (0, 1, 0) is outside sRGB: negative red, a bit over-green (published matrix)
    assert L.to_srgb_matrix("adobergb") @ np.array([0, 1, 0]) == pytest.approx([-0.3976, 1.0, -0.0426], abs=2e-3)


def test_a_lut_looks_the_same_whatever_the_output_space():
    """A colour in sRGB and the same colour encoded in Adobe RGB give the same LUT result."""
    lut = L.builtin("builtin-teal-and-orange")
    srgb = np.random.default_rng(7).random((20, 20, 3)).astype(np.float32) * 0.6 + 0.2
    want = L.apply(srgb, lut, "srgb")
    m = L.to_srgb_matrix("adobergb")
    adobe_lin = colour.to_linear(srgb, "srgb").reshape(-1, 3) @ np.linalg.inv(m).T
    adobe = colour.to_encoded(adobe_lin.astype(np.float32).reshape(srgb.shape), "adobergb")
    got = L.apply(adobe, lut, "adobergb")
    back = colour.to_encoded((colour.to_linear(got, "adobergb").reshape(-1, 3) @ m.T)
                             .astype(np.float32).reshape(srgb.shape), "srgb")
    assert np.abs(back - want).max() < 3e-3


def test_intensity_blends_between_the_original_and_the_full_look():
    lut = L.builtin("builtin-faded-matte")
    img = np.random.default_rng(8).random((16, 16, 3)).astype(np.float32)
    full, none, half = L.apply(img, lut), L.apply(img, lut, amount=0), L.apply(img, lut, amount=0.5)
    assert np.array_equal(none, img)
    assert np.abs(half - (img + full) / 2).max() < 1e-6
    assert np.abs(full - img).max() > 0.05


def test_a_linear_1d_lut_acts_on_linear_light():
    """Halving linear light is one stop down: an sRGB mid-grey code 0.5 goes to code ~0.3865."""
    half = L.parse_cube("# photo-room: space=linear\nLUT_1D_SIZE 2\n0 0 0\n0.5 0.5 0.5\n")
    assert half.space == "linear"
    out = L.apply(np.full((2, 2, 3), 0.5, np.float32), half, "srgb")
    assert out[0, 0, 0] == pytest.approx(float(colour.srgb_encode_exact(0.5 * colour.srgb_decode_exact(0.5))), abs=2e-3)


def test_apply_does_not_modify_its_input_and_stays_in_range():
    lut = L.builtin("builtin-cool-chrome")
    img = np.random.default_rng(9).random((10, 10, 3)).astype(np.float32) * 1.4 - 0.2      # out of range
    before = img.copy()
    out = L.apply(img, lut)
    assert np.array_equal(img, before)
    assert out.min() >= 0 and out.max() <= 1 and out.dtype == np.float32


# ---------------------------------------------------------------------------
# Built-ins and the library
# ---------------------------------------------------------------------------

def test_builtin_looks_are_valid_distinct_and_not_identity():
    ids = list(L.builtin_ids())
    assert len(ids) >= 5
    tables = []
    for i in ids:
        lut = L.get(i)
        assert lut.size == L.BUILTIN_SIZE and np.isfinite(lut.table).all()
        assert lut.table.min() >= 0 and lut.table.max() <= 1
        assert not lut.is_identity(1e-2)
        tables.append(lut.table)
    assert len({t.tobytes() for t in tables}) == len(tables)


def test_silver_mono_is_black_and_white():
    lut = L.get("builtin-silver-mono")
    img = np.random.default_rng(10).random((8, 8, 3)).astype(np.float32)
    out = L.apply(img, lut)
    assert np.abs(out[..., 0] - out[..., 1]).max() < 0.03          # only a faint tone, no colour


def test_import_dedupes_renames_deletes_and_exports(tmp_path):
    text = cube_text(5, lambda r, g, b: (b, g, r), title="Swap")
    a = L.import_text(text)
    assert a["name"] == "Swap" and a["kind"] == "3d" and not a["builtin"]
    assert L.import_text(text, name="other name")["id"] == a["id"]            # same table, same LUT
    assert [x["id"] for x in L.list_luts() if not x["builtin"]] == [a["id"]]
    assert L.rename(a["id"], "  My   swap ")["name"] == "My swap"
    assert "My swap" in L.export_text(a["id"])
    assert L.parse_cube(L.export_text(a["id"])).size == 5
    assert L.delete(a["id"]) is True and L.delete(a["id"]) is False
    with pytest.raises(L.LutNotFound):
        L.get(a["id"])


def test_get_refuses_ids_that_are_not_ours():
    for bad in ("../../etc/passwd", "", "xyz", "builtin-nope", None):
        with pytest.raises(L.LutNotFound):
            L.get(bad)
    assert L.exists("builtin-warm-negative") and not L.exists("0123456789ab")


def test_a_bad_file_is_not_added_to_the_library():
    with pytest.raises(L.LutError):
        L.import_text("LUT_3D_SIZE 2\n1 2 3\n")
    assert not [x for x in L.list_luts() if not x["builtin"]]


def test_importing_with_a_declared_space_keeps_it():
    entry = L.import_text("LUT_1D_SIZE 2\n0 0 0\n1 1 1\n", name="lin", space="linear")
    assert entry["space"] == "linear" and L.get(entry["id"]).space == "linear"

# Colour pipeline

How a raw file becomes pixels, which maths each slider uses, and where that differs from
Lightroom / Camera Raw. This replaces the old behaviour in which every edit was applied to
gamma-coded 8-bit values (see "Edit versions" for what happens to photos edited that way).

## The pipeline

```
raw ──LibRaw──▶ linear light, 16-bit, camera white balance, camera→working matrix, no curve
      │            (process.decode_linear: gamma=(1, 1), no_auto_bright, output_bps=16)
      ▼
   float32 image ── crop to the camera's default area
      │
      ├─ geometry: spots/lens → transform (xf_*) → Straighten → Crop      (float32, cv2.warpAffine)
      ├─ camera profile (optional): HueSatMap → LookTable → tone curve    (profiles.py; cached with the geometry stage)
      ▼
   tone stage ─ in LINEAR light:  white balance · exposure · shadows · highlights · whites · blacks
      │          then encode with the output space's curve
      │          then in CODE VALUES: contrast S-curve · tone curve (one lookup)
      ▼
   saturation / vibrance → HSL · B&W mixer → colour grading → film style (LUT) → clarity/texture/dehaze/sharpen
      ▼
   masks (same light maths as the global sliders) → edit strength → monochrome
      ▼
   the ONE quantisation: float → 8-bit (round) for JPEG/8-bit PNG, float → 16-bit for 16-bit PNG
```

Nothing before the last line is rounded to an integer, so an 8-bit export uses all 256
levels (a smooth ramp keeps 256; the old 8-bit decode kept 129 on an Exposure +2 / Shadows +50 edit)
and a 16-bit export of a straightened photo keeps its 16-bit precision (the old rotation went
through `uint8`: 256 levels). Exceptions: the preview JPEG sent to the browser is 8-bit by nature;
the detail stage (clarity/texture/dehaze/sharpen) blurs through Pillow's 8-bit Gaussian as before;
the spot-healing patch (Poisson blend) is computed in 8-bit inside its small disc.

## Camera profiles

A profile is the base look that Lightroom's Profile browser chooses (Adobe Standard, Landscape,
Portrait ...). Here it is `profiles.py`, applied once per photo and profile, right after the
geometry stage and before white balance, exposure and every other edit. It is stored in the edit
as `camera_profile` (a profile id) and in the XMP sidecar as `crs:CameraProfile` (the name) with
`crs:CameraProfileDigest` (the MD5 of an imported file). A photo whose sidecar names a profile we
cannot resolve (for example Lightroom's "Adobe Standard") keeps that name in `camera_profile_foreign`
and writes it back unchanged until another profile is chosen; no profile means the camera-matrix
conversion only, as before.

* **Built-in profiles**: Standard, Neutral, Landscape, Portrait, Vivid, Flat, Monochrome. Each is
  a saturation matrix, a synthesised hue/saturation table (Landscape lifts greens, blues and
  oranges; Portrait calms reds and oranges) and an S-curve `y = x - k sin(2 pi x) / (2 pi)` (k < 0 flattens)
  applied to the working space's coded values, the same domain the Saturation slider uses. These are
  photo-room's own looks, not Adobe's: "Standard" is *not* "Adobe Standard".
* **DCP import** (`.dcp`, the TIFF-like DNG Camera Profile container, magic `IIRC`): the
  `ProfileHueSatMap` (the second table when two illuminants are present), `ProfileLookTable` and
  `ProfileToneCurve` are applied in that order, hue/saturation tables in HSV (linear, or sRGB-coded
  when `ProfileHueSatMapEncoding` says so) and the curve in linear light. `ColorMatrix1/2` and
  `ForwardMatrix1/2` are parsed and shown but **not applied**: they map camera-native RGB, and by
  this point LibRaw has already converted to the output space with the camera's own matrix. So a DCP
  gives its look (colour tweaks and base curve) but not its camera-specific colorimetry, and the
  two-illuminant tables are not blended by colour temperature.
* **ICC input profiles**: matrix/TRC RGB profiles only. The photo's coded values are re-interpreted as
  being in that profile's RGB space (its curves and primaries, Bradford-adapted through D50) and
  converted to the working space. LUT-based (A2B) and non-RGB profiles are refused with a message.
* **Storage**: `~/.photo-room/profiles` (`PHOTO_ROOM_PROFILE_DIR` redirects it), one file per
  profile named by the first 12 hex digits of its MD5, plus `index.json`. API: `/api/profiles`,
  `/api/profiles/import`, `/api/profiles/delete`, `/api/profiles/thumb`.
* **Cost and caching**: the profile result is part of the cached *geometry* stage, whose key
  (`stagecache.stage_keys`) contains `camera_profile` and `profiles.revision(id)` (changes if the
  imported file is replaced or deleted), so dragging any slider reuses it; only choosing a
  different profile redoes the geometry stage onward. Hue tables with one value division are
  baked to a 720 x 128 (hue x saturation) map so a pixel costs one lookup. A missing profile is
  skipped with a warning, never an error. Edit version 1 (legacy) renders ignore profiles.
* **Interactions**: Edit strength blends toward the *profiled* image (the profile is part of the base,
  not an edit). The Monochrome profile removes colour before the B&W mixer, so the mixer then has
  nothing to work on; use the Black & White toggle for a mixer-driven conversion.

## Working space and transfer functions

The working space is **linear light in the primaries of the chosen output space**: LibRaw's
camera matrix goes straight to sRGB, Adobe RGB (1998) or ProPhoto primaries, and the edits run
there. (Lightroom works internally in linear ProPhoto-primaries "Melissa RGB" and converts at
the end; the practical difference is that very saturated colours clip differently when the output
is sRGB. The preview is always sRGB.)

| Output | Curve (what is encoded, and what the embedded ICC profile says) |
|---|---|
| sRGB | the exact IEC 61966-2-1 piecewise function (12.92·x below 0.0031308, 1.055·x^(1/2.4) − 0.055 above). ICC: LittleCMS's sRGB profile, whose parametric curve is the same function |
| Adobe RGB (1998) | pure power law, 563/256 = 2.19921875 (the ICC `curv` value) |
| ProPhoto RGB | pure power law, 1.8 (the ICC `curv` value; the ROMM linear toe is not used, matching the profile we embed) |

Before this change the decode used LibRaw's default `gamma=(2.222, 4.5)` (the BT.709 curve) for
all three, so mid-tones were about 14 8-bit levels darker than a colour-managed viewer expects for
sRGB (mean 4 levels for ProPhoto). `tests/test_linear_light.py` pins the sRGB round trip, compares
neutral greys against lcms's Lab values for the embedded profile, and compares the Adobe/ProPhoto
gammas with the ICC bytes.

The film-style LUT is display-referred and runs on the encoded image (in sRGB primaries and curve, converted from and back to the output space); with black & white on it runs after the grey conversion. See [PRESETS-AND-LUTS.md](PRESETS-AND-LUTS.md).

## The controls

All numbers are the constants in `render_kernels.py` / `colour.py` / `tonecurve.py`.

| Control | Domain | Definition | Lightroom PV2012 |
|---|---|---|---|
| Exposure | linear | gain 2^EV on all channels | Same gain. LR also rolls highlights off softly above 1.0 and has headroom from the raw's unclipped data (LibRaw clips at white, so +EV on a clipped highlight stays white here) |
| Temp | linear | gains = illuminant(baseline)/illuminant(Temp) (CIE daylight locus ≥ 4000 K, Planckian below), normalised so white keeps its luminance. Raising Temp warms | Same direction and shape; LR derives the gains from the camera's own colour matrices, we use sRGB-primary illuminant colours. The "as shot" temperature is an estimate from the R/B multipliers, as before, so absolute Kelvin values do not match LR for the same file; relative moves are close |
| Tint | linear | green gain 2^(−0.5·t/150) with a small opposite red/blue gain, luminance-neutral; + is magenta | Same sign; the magnitude per unit is approximate |
| Shadows | linear, weighted by lightness | up to +1.5 stops·(s/100) on pixels whose lightness is in the lower half (mask (1 − L/0.55)²); hue preserved (one factor for all channels) | LR's shadows is local-contrast aware (it lifts shadows while protecting local detail); ours is a pure tone-range gain, so it is flatter and can look "grey" at +100 |
| Highlights | linear, weighted | up to ±1.5 stops·(h/100) above lightness 0.45 (mask squared ramp); can recover values pushed above 1.0 by Exposure | LR's recovery is also local-contrast aware and reconstructs more from the raw's highlight headroom |
| Whites | linear, weighted | ±0.75 stop·(w/100) above lightness 0.70 | LR moves the white point; ours moves the top of the range, close in effect |
| Blacks | linear, weighted | adds/removes up to 0.012 linear light (sRGB code ≈ 0.10) on the darkest tones (mask (1 − L/0.35)²); −100 crushes to 0, +100 lifts | LR moves the black point; ours is the same idea, constants are approximate |
| Contrast | code values | S-curve about 0.5: a logistic sigmoid with slope ≈ 1.75 at the centre at +100, its inverse for negative values; never clips | LR's contrast is also an S-curve in a display-referred space; shape differs in detail. (The old maths scaled values about 0.5 and clipped.) |
| Vibrance, Saturation | code values | saturation scales distance from the luma (0.2126/0.7152/0.0722 on code values); vibrance boosts low-chroma pixels more | LR's work in a perceptual space (Vibrance also protects skin hues; ours does not) |
| Tone curve | code values | exactly as before (65 537-entry lookup, nearest; ≤ 2 sixteen-bit codes of error) | Same idea |
| HSL, grading, clarity, texture, dehaze, sharpness | code values | unchanged | approximations of LR, as before |
| Masks | as global | temp/tint/exposure/shadows/highlights/contrast use the same kernel as the global sliders (a local +1 EV is a global +1 EV); the rest as before | LR's local sliders have more controls (whites/blacks, noise, ...) |

The shadows/highlights/whites/blacks masks are drawn on `sqrt(luminance)` (a gamma-2 lightness,
identical for every output space) rather than on the output curve, because it costs one pass.

What is **not** in this pipeline and so cannot match Lightroom pixel for pixel: Adobe's own camera
profiles and base tone curve ("Adobe Standard" with its default contrast curve; a DCP imported from
Adobe gives its look tables and curve but not its camera matrices, see above), its highlight
reconstruction, its demosaic and noise reduction, its lens profiles, and PV2012's local-contrast
tone mapping. The preview is, as `editor.apply_adjustments` has always said, a way to judge an edit,
not a byte-exact rendering of Camera Raw.

## Edit versions: what happens to photos edited under the old maths

The same slider numbers look different under the new maths (+1 EV used to be ≈ 2.2 stops of light),
so every edit records which maths it was made with:

* Saved edits carry `xmp:CreatorTool = photo-room (edit v2)` in the sidecar / DNG. Edits written
  by Lightroom or Camera Raw (they carry `crs:Version` or a CreatorTool naming Adobe software) are
  treated as version 2: their numbers were meant for a linear-light renderer.
* A settings file with edits and **no** version is version 1 (every photo edited before this
  change). Virtual copies, history entries and presets without an `edit_version` are the same.
  Photos with nothing in them are version 2.
* Version 1 edits keep rendering with the original maths (`render_kernels.tone_stage_legacy`,
  `masks._apply_light_legacy`, starting from the image re-coded with the old BT.709 curve), so
  they look **exactly** as they did, in the preview and in exports, until the user decides
  otherwise. Opening one shows a banner with an **Upgrade edit** button. Saving a legacy edit
  re-writes it as version 1; it is never relabelled.
* **Upgrade edit** (`POST /api/upgrade_edit`, `editversion.upgrade`) re-maps the sliders (Exposure
  × 2.2, Temp solved so red/blue gain matches, local masks' exposure and temp likewise), then
  refines Exposure, Contrast, Shadows, Highlights, Whites and Blacks by coordinate descent against
  the version 1 render of the photo itself, with a small cost for moving sliders the user never
  touched. It writes nothing: the result is a new unsaved state recorded as a History step ("Upgrade
  to linear-light colour"), so jumping back in History restores the old look. The status line
  reports the mean difference from the old look in 8-bit levels, before and after, and which
  sliders were re-fitted.
* How close can it get? The old and new neutral renders differ by a curve (BT.709 vs sRGB), not a
  gain, so sliders can only approximate it. Measured on the real CC0 CR2 at preview size with five
  typical old edits (exposure only; exposure + contrast; exposure + shadows + highlights + contrast;
  exposure + temperature + tint + saturation; contrast only): mean difference from the old look
  1.8-7.3 of 255 levels after the upgrade (99th percentile 13-26), against 10-39 for copying the
  numbers across unchanged. The re-fit can move sliders a lot: an exposure-only edit of +0.8
  became Exposure +1.13 with Contrast +68, because the old gamma-space gain clipped highlights
  hard and contrast is the closest new control. Heavy edits, hard clipping in the old maths and
  large temperature moves are the worst cases; the old look is one History step away.
* Presets saved before versions are re-mapped analytically (no render) when applied to a current
  photo, and refused with an "upgrade first" message for a photo still on the old maths. Batch apply
  to a virtual copy with an old edit re-maps the copy first. The style model's learned exposure
  corrections recorded before versions are scaled by 2.2 when loaded. Auto exposure now measures
  the shift in light (a median at code 0.20 asks for +2 EV where it asked for +1).
* Unedited photos have no version to preserve: their preview and exports now use the correct
  curve, so mid-tones are brighter than before (about 14 levels in sRGB). That is the M1 fix, not
  an edit change, and it is the one visible difference for photos you never touched.

## Precision notes and known limits

* Straighten uses `cv2.warpAffine` (bicubic) on float32. A 4096-pixel ramp straightened by 1°
  keeps a distinct 16-bit value in every column (4045 of 4045; the old `uint8` rotation left 256),
  and the values are within 2.5 sixteen-bit codes of a float64 reference
  (`test_straighten_keeps_16_bit_precision`).
* The exporter hands the 16-bit linear decode straight to the pipeline and resizes it in linear
  light (gamma-correct downscaling). Edits needing display-coded pixels (spots, lens corrections,
  edit version 1) get an encoded copy instead.
* Non-raw sources (JPEG/TIFF) are 8-bit and assumed sRGB-coded unless an output space is
  chosen; their edits run in the same linear maths, but they cannot gain precision.
* Not changed: the camera's default crop, detail-stage Gaussian blur quantisation, the
  spot-healing patch.

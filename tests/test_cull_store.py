"""The cull store: decision log, accuracy, personal pick/reject model, learned Auto-edit offsets."""

import json

import numpy as np
import pytest

import cull
import cull_store as cs


@pytest.fixture
def st(tmp_path):
    return cs.CullStore(tmp_path)


def vec(sharp_rel=0.8, eyes=0.0, face=0.0, expo=0.9):
    return [sharp_rel, 0.5, expo, 0.0, 0.0, 0.4, face, eyes]


def decide(st, n, make, batch="b"):
    st.log_decisions([dict(path=f"p{i}.cr2", group=i // 3, gsize=3, vec=v, suggested=s, final=f)
                      for i, (v, s, f) in enumerate(make(i) for i in range(n))], batch)


def test_logistic_regression_separates_a_linear_problem():
    rng = np.random.default_rng(0)
    X = rng.uniform(0, 1, (300, 3))
    y = (X[:, 0] - X[:, 1] > 0).astype(float)
    w = cs.fit_logistic(X, y)
    p = cs.sigmoid(np.hstack([X, np.ones((300, 1))]) @ w) > 0.5
    assert (p == (y > 0.5)).mean() > 0.95
    assert w[0] > 0 > w[1]


def test_model_has_no_say_until_it_has_enough_decisions(st):
    decide(st, cs.MIN_FIT - 1, lambda i: (vec(), "pick", "pick"))
    m = st.pick_model()
    assert m.confidence == 0 and m.blend(61.0, vec()) == 61.0 and not m.rejects(vec())


def test_model_learns_a_taste_the_heuristic_knows_nothing_about(st):
    """This user always keeps the punchier (higher-contrast) frame; the plain score ignores contrast."""
    rng = np.random.default_rng(3)

    def make(i):
        v = vec(sharp_rel=float(rng.uniform(0.6, 1.0)))
        v[5] = float(rng.uniform(0.1, 0.9))
        return v, "pick" if v[0] > 0.8 else "none", "pick" if v[5] > 0.5 else "none"
    decide(st, 150, make)
    m = st.pick_model()
    assert m.confidence > 0.5
    flat, punchy = vec(0.8), vec(0.8)
    flat[5], punchy[5] = 0.15, 0.85
    assert cull.heuristic_score(flat) == cull.heuristic_score(punchy)
    assert m.blend(cull.heuristic_score(punchy), punchy) > m.blend(cull.heuristic_score(flat), flat) + 5
    assert m.info()["weights"]["contrast"] > 0


def test_reject_model_needs_enough_examples_of_both_kinds(st):
    decide(st, 160, lambda i: (vec(sharp_rel=0.1 if i % 4 == 0 else 0.9), "none", "reject" if i % 4 == 0 else "none"))
    m = st.pick_model()
    assert m.reject_w is not None
    assert m.rejects(vec(sharp_rel=0.1)) and not m.rejects(vec(sharp_rel=0.95))


def test_accuracy_over_time_and_overrides(st):
    decide(st, 20, lambda i: (vec(), "pick", "pick" if i < 10 else "reject"))
    a = st.accuracy(bucket=10)
    assert a["decisions"] == 20 and a["overall"] == 0.5
    assert [s["rate"] for s in a["series"]] == [1.0, 0.0]
    assert a["overridden_suggestions"]["pick"] == 10
    assert cs.CullStore(st.path.parent).accuracy()["decisions"] == 20          # persisted


def test_undone_batch_does_not_train(st):
    decide(st, 6, lambda i: (vec(), "pick", "pick"), batch="x")
    st.drop_batch_decisions("x")
    assert st.accuracy()["decisions"] == 0


def test_feature_cache_is_keyed_by_file_identity(st):
    st.put_features("a.cr2", "k1", {"v": cull.FEATURE_VERSION, "hash": "0" * 16})
    assert "a.cr2" in st.get_features({"a.cr2": "k1"})
    assert st.get_features({"a.cr2": "k2"}) == {}                  # file changed
    st.put_features("old.cr2", "k", {"v": cull.FEATURE_VERSION - 1})
    assert st.get_features({"old.cr2": "k"}) == {}                 # older feature version


def test_export_reset_and_clear(st):
    decide(st, 12, lambda i: (vec(), "pick", "pick"))
    st.put_features("a", "k", {"v": cull.FEATURE_VERSION})
    ex = st.export()
    assert ex["format"] == "photo-room-cull-export" and len(ex["decisions"]) == 12
    json.dumps(ex)
    st.reset_learning()
    assert st.accuracy()["decisions"] == 0 and st.pick_model().n == 0
    assert st.get_features({"a": "k"})                              # cache kept by reset
    st.clear_all()
    assert st.get_features({"a": "k"}) == {}


def test_learned_offsets_from_edits_made_after_auto_edit(st):
    applied = {"exposure": 0.5, "contrast": 10, "color_temp": 5000}
    for i in range(6):
        st.record_autoedit(f"p{i}", "auto-step", applied, {"median": 0.4, "range": 0.6})
    state = {f"p{i}": ("user-step", {"exposure": 0.8, "contrast": 10, "color_temp": 5500}) for i in range(5)}
    state["p5"] = ("auto-step", applied)                           # untouched: teaches nothing
    changed = st.learn_offsets(lambda p: state[p])
    assert changed == 5
    m = st.offset_model()
    typ = m.info()["typical"]
    assert typ["exposure"]["samples"] == 5 and "contrast" not in typ       # zero offsets are not samples
    pred = m.predict({"median": 0.4})
    assert 0 < pred["exposure"] <= 0.3 and pred["temp_ratio"] > 0          # shrunk toward the user's +0.3 EV, warmer
    assert pred["exposure"] < 0.3 * 1.0001


def test_offsets_are_shrunk_bounded_and_applied_on_the_slider_grid():
    m = cs.OffsetModel.fit([({"exposure": 9.0, "contrast": 90}, None)] * 3)
    pred = m.predict()
    assert pred["exposure"] <= cs.OFFSET_LIMITS["exposure"] and pred["contrast"] <= cs.OFFSET_LIMITS["contrast"]
    out = cs.apply_offsets({"exposure": 0.33, "contrast": 3, "color_temp": 5000},
                           {"exposure": 0.21, "contrast": 4.4, "temp_ratio": 0.1})
    assert out["exposure"] == 0.55 and out["contrast"] == 7 and out["color_temp"] == 5500
    assert cs.apply_offsets({"exposure": 1.0}, {}) == {"exposure": 1.0}


def test_no_offsets_without_enough_samples():
    assert cs.OffsetModel.fit([({"exposure": 0.5}, None)]).predict() == {}

import numpy as np

from jarvis.audio.wake import WakeWord, parse_models

FRAME = np.zeros(1280, dtype=np.int16)


def test_parse_models():
    assert parse_models("hey_jarvis", 0.5) == [("hey_jarvis", 0.5)]
    assert parse_models("jarvis:0.06, hey_jarvis:0.5", 0.5) == [("jarvis", 0.06), ("hey_jarvis", 0.5)]
    assert parse_models("jarvis:0.06,hey_jarvis", 0.4) == [("jarvis", 0.06), ("hey_jarvis", 0.4)]


def test_either_model_fires_at_its_own_threshold():
    scores = iter([
        {"jarvis": 0.05, "hey_jarvis": 0.4},   # both below their thresholds
        {"jarvis": 0.07, "hey_jarvis": 0.1},   # the single-word model fires
        {"jarvis": 0.01, "hey_jarvis": 0.9},   # within the refractory period
    ])
    w = WakeWord("jarvis:0.06, hey_jarvis:0.5", refractory_s=2.0, scorer=lambda f: next(scores))
    w._thresholds = {"jarvis": 0.06, "hey_jarvis": 0.5}
    assert w.process(FRAME, now=0.0) is False
    assert w.process(FRAME, now=1.0) is True and w.last_score == 0.07
    assert w.process(FRAME, now=2.0) is False


def test_hey_jarvis_alone_fires_after_refractory():
    scores = iter([{"jarvis": 0.01, "hey_jarvis": 0.8}])
    w = WakeWord("jarvis:0.06, hey_jarvis:0.5", scorer=lambda f: next(scores))
    w._thresholds = {"jarvis": 0.06, "hey_jarvis": 0.5}
    assert w.process(FRAME, now=10.0) is True and w.last_score == 0.8


def test_float_scorer_still_supported():
    w = WakeWord("fake", threshold=0.5, scorer=lambda f: 0.6)
    assert w.process(FRAME, now=0.0) is True

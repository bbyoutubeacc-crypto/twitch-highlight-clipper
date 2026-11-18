import random

from clipper.chat import ChatMessage
from clipper.config import DetectionConfig
from clipper.detection import ChatActivitySignal, HighlightDetector, HypeMatcher, normalize_text


def make_cfg(**kw):
    cfg = DetectionConfig(
        threshold=3.0, window_seconds=5, baseline_seconds=120, warmup_seconds=30,
        min_baseline_rate=0.3, min_messages=6, cooldown_seconds=60, hype_multiplier=0.6,
        spam_window_seconds=30, max_messages_per_user=3,
        ignored_users=["nightbot", "streamelements"],
        hype_terms={"KEKW": 1.0, "LUL": 1.0, "POG": 1.0, "clip": 1.5, "!clip": 2.0, "LETS GO": 1.0, "W": 0.6},
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def simulate(rate_fn, seconds, cfg, start=1_000_000.0, text_fn=None, seed=1):
    """Erzeugt Chat mit rate_fn(t) Nachrichten/s und tickt den Detektor."""
    rng = random.Random(seed)
    sig = ChatActivitySignal(cfg)
    events = []
    det = HighlightDetector([sig], {"chat": 1.0}, cfg.threshold, cfg.cooldown_seconds, on_highlight=events.append)
    sig.mark_started(start)
    samples = []
    user_counter = 0
    for s in range(seconds):
        t0 = start + s
        n = rate_fn(s)
        whole = int(n) + (1 if rng.random() < n - int(n) else 0)
        for i in range(whole):
            user_counter += 1
            text = text_fn(s, rng) if text_fn else f"nachricht {rng.randint(0, 10_000)}"
            sig.add_message(ChatMessage(t0 + (i + 0.5) / max(whole, 1), f"user{user_counter % 500}", text))
        samples.append(det.tick(t0 + 1))
    return events, samples, sig, det


def test_normalize_text():
    assert normalize_text("KEKW KEKW  KEKW") == "KEKW"
    assert normalize_text("hallo\U000e0000 ") == "hallo"
    assert normalize_text("  a   b  ") == "a b"


def test_hype_matcher():
    m = HypeMatcher({"KEKW": 1.0, "LETS GO": 1.0, "!clip": 2.0, "W": 0.6})
    assert m.match("KEKW KEKW")[0] == 1.0
    assert m.match("lets go!!")[0] == 1.0
    assert m.match("!clip")[0] == 2.0
    assert m.match("w")[0] == 0.6
    assert m.match("wow nice")[0] == 0.0  # 'w' nur als ganzes Wort


def test_spike_detected_once_with_cooldown():
    cfg = make_cfg()
    # 1 msg/s normal, Spitze von 6 msg/s bei s=100..110 und erneut bei 130..140 (im Cooldown)
    rate = lambda s: 6 if 100 <= s < 110 or 130 <= s < 140 else 1
    events, samples, *_ = simulate(rate, 200, cfg)
    assert len(events) == 1
    assert 100 <= events[0].trigger_time - 1_000_000 <= 108


def test_no_trigger_during_warmup():
    cfg = make_cfg(warmup_seconds=60)
    rate = lambda s: 10 if s < 20 else 1
    events, *_ = simulate(rate, 80, cfg)
    assert events == []


def test_scale_independent_small_and_big_channel():
    for base in (0.5, 50):
        cfg = make_cfg()
        rate = lambda s, b=base: b * 5 if 150 <= s < 160 else b
        events, *_ = simulate(rate, 200, cfg)
        assert len(events) == 1, f"Basis {base}"


def test_steady_chat_no_trigger():
    cfg = make_cfg()
    events, samples, *_ = simulate(lambda s: 20, 300, cfg)
    assert events == []
    assert max(x.score for x in samples[60:]) < 2.0


def test_spam_same_user_counts_once():
    cfg = make_cfg()
    sig = ChatActivitySignal(cfg)
    for i in range(20):
        sig.add_message(ChatMessage(1000 + i * 0.1, "spammer", "KEKW KEKW"))
    sig.add_message(ChatMessage(1001.5, "spammer", "KEKW\U000e0000"))
    sample = sig.update(1003.0)
    assert sample.details["window_count"] == 1
    assert sig.ignored_spam == 20


def test_max_messages_per_user():
    cfg = make_cfg(max_messages_per_user=3)
    sig = ChatActivitySignal(cfg)
    for i in range(10):
        sig.add_message(ChatMessage(1000 + i * 0.2, "fastuser", f"nachricht {i}"))
    assert sig.update(1003.0).details["window_count"] == 3


def test_bots_ignored():
    cfg = make_cfg()
    sig = ChatActivitySignal(cfg)
    for i in range(10):
        sig.add_message(ChatMessage(1000 + i * 0.1, "Nightbot", f"!command {i}"))
    assert sig.update(1003.0).details["window_count"] == 0
    assert sig.ignored_bot == 10


def test_hype_terms_boost_score():
    cfg = make_cfg()
    rate = lambda s: 3 if 150 <= s < 160 else 1
    plain, s_plain, *_ = simulate(rate, 170, cfg)
    hype, s_hype, *_ = simulate(rate, 170, cfg, text_fn=lambda s, rng: f"KEKW {rng.randint(0, 99999)}" if s >= 150 else f"x {rng.randint(0, 99999)}")
    assert max(x.score for x in s_hype) > max(x.score for x in s_plain) * 1.3
    assert len(hype) >= len(plain)


def test_summary_top_messages_and_emotes():
    cfg = make_cfg()
    sig = ChatActivitySignal(cfg)
    texts = ["KEKW"] * 8 + ["LETS GO"] * 4 + ["was war das"] * 3
    for i, t in enumerate(texts):
        sig.add_message(ChatMessage(1000 + i * 0.1, f"u{i}", t))
    summ = sig.summary(999, 1010)
    assert summ["top_messages"][0] == {"text": "KEKW", "users": 8}
    assert summ["top_emotes"][0]["text"] == "KEKW"
    assert summ["chatters"] == len(texts)


def test_detector_combines_weighted_signals():
    from clipper.detection import Signal, SignalSample

    class Fixed(Signal):
        def __init__(self, name, score):
            self.name, self._s = name, score

        def update(self, now):
            return SignalSample(time=now, score=self._s)

    events = []
    det = HighlightDetector([Fixed("chat", 2.0), Fixed("audio", 5.0)], {"chat": 1.0, "audio": 1.0}, 3.0, 60, events.append)
    assert det.tick(1.0).score == 3.5
    assert len(events) == 1

import datetime as dt

from clipper.config import AppConfig, SubtitleConfig
from clipper.subtitles import Word, build_cues, wrap_lines, write_ass, write_srt
from clipper.youtube import build_metadata, write_metadata_files


def words(text, start=0.0, step=0.3):
    return [Word(start + i * step, start + i * step + 0.25, w) for i, w in enumerate(text.split())]


def test_wrap_lines_balanced():
    lines = wrap_lines("das ist ein ziemlich langer satz für zwei zeilen", 30)
    assert len(lines) == 2
    assert all(len(l) <= 30 for l in lines)


def test_build_cues_respects_limits():
    ws = words("Hallo zusammen und willkommen im Stream. Heute spielen wir etwas ganz Besonderes mit euch allen zusammen.")
    cues = build_cues(ws, max_chars_per_line=22)
    assert cues
    for c in cues:
        assert all(len(l) <= 22 for l in c.text.split("\n"))
        assert len(c.text.split("\n")) <= 2
        assert c.end > c.start
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start


def test_gap_splits_cues():
    ws = words("eins zwei") + words("drei vier", start=5.0)
    cues = build_cues(ws, 40)
    assert len(cues) == 2


def test_srt_and_ass(tmp_path):
    cues = build_cues(words("Hallo Welt {test} \\ backslash"), 40)
    srt = write_srt(cues, tmp_path / "a.srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,000 --> ")
    ass = write_ass(cues, tmp_path / "a.ass", SubtitleConfig(), vertical=True).read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "{test}" not in ass.split("[Events]")[1]


def test_metadata(tmp_path):
    cfg = AppConfig()
    summary = {
        "top_emotes": [{"text": "KEKW", "users": 30}, {"text": "LUL", "users": 10}],
        "top_messages": [{"text": "KEKW", "users": 25}, {"text": "der hat das echt gemacht", "users": 4}],
    }
    when = dt.datetime(2026, 5, 1, 20, 15)
    meta = build_metadata(cfg, "kanal", "Kanal", when, summary, "Ich kann das nicht glauben. Ok.")
    assert "😂" in meta.title
    assert "https://www.twitch.tv/kanal" in meta.description
    assert "Kanal" in meta.tags and "kanal highlights" in meta.tags
    assert len(meta.title) <= 100
    shorts = build_metadata(cfg, "kanal", "Kanal", when, summary, shorts=True)
    assert shorts.title.endswith("#shorts")
    p = write_metadata_files(meta, tmp_path / "x_youtube.txt")
    assert "TITEL" in p.read_text(encoding="utf-8")
    assert (tmp_path / "x_youtube.json").exists()

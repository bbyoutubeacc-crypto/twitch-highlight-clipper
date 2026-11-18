import shutil
import time
from pathlib import Path

import pytest

from clipper.config import ClipConfig, RecordingConfig
from clipper.recorder import RecordingRun, Segment, SegmentBuffer, StreamRecorder

ffmpeg_missing = shutil.which("ffmpeg") is None


def _seg(tmp: Path, run: RecordingRun, i: int, length=10.0) -> Segment:
    p = tmp / f"run_{i}.ts"
    p.write_bytes(b"x")
    return Segment(path=p, run=run, pts_start=i * length, pts_end=(i + 1) * length)


def test_anchor_median_and_segments_for(tmp_path):
    run = RecordingRun(run_id=1, started_wall=1000.0)
    for est in (1000.0, 1001.0, 1000.5, 1030.0):  # Ausreißer wird ignoriert
        run.add_anchor_estimate(est)
    assert run.anchor == pytest.approx(1000.75)
    buf = SegmentBuffer()
    for i in range(6):
        buf.add(_seg(tmp_path, run, i))
    segs = buf.segments_for(1000.75 + 15, 1000.75 + 35)
    assert [round(s.pts_start) for s in segs] == [10, 20, 30]


def test_cleanup_respects_protection(tmp_path):
    run = RecordingRun(run_id=1, started_wall=0.0)
    run.add_anchor_estimate(0.0)
    buf = SegmentBuffer()
    for i in range(10):
        buf.add(_seg(tmp_path, run, i))
    token = buf.protect(25.0)
    buf.cleanup(now=100.0, keep_seconds=30)  # ohne Schutz: alles < 70 löschen
    assert [s.pts_start for s in buf.all()][0] == 20.0
    buf.release(token)
    buf.cleanup(now=100.0, keep_seconds=30)
    assert [s.pts_start for s in buf.all()][0] == 60.0  # endet genau bei 70 -> bleibt
    assert not (tmp_path / "run_0.ts").exists()


def test_segments_for_prefers_run_with_most_overlap(tmp_path):
    r1, r2 = RecordingRun(1, 0.0), RecordingRun(2, 0.0)
    r1.add_anchor_estimate(0.0)
    r2.add_anchor_estimate(100.0)
    buf = SegmentBuffer()
    buf.add(_seg(tmp_path, r1, 9))   # 90-100
    for i in range(3):
        buf.add(_seg(tmp_path, r2, i))  # 100-130
    segs = buf.segments_for(95, 125)
    assert {s.run.run_id for s in segs} == {2}


@pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg nicht installiert")
def test_recorder_with_fake_stream_and_cut(tmp_path):
    from clipper.clip_maker import cut_from_segments

    cfg = RecordingConfig(segment_seconds=2, buffer_seconds=60)
    src = [
        "ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine", "-t", "9", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
        "-c:a", "aac", "-f", "mpegts", "-",
    ]
    rec = StreamRecorder("test://", cfg, tmp_path / "buf", input_command=src)
    rec.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and rec.buffer.buffered_seconds() < 6:
            time.sleep(0.5)
        segs = rec.buffer.all()
        assert len(segs) >= 3
        start = segs[0].wall_start + 0.5
        end = segs[-1].wall_end - 0.5
        out = tmp_path / "clip.mp4"
        real = cut_from_segments(rec.ffmpeg, rec.buffer.segments_for(start, end), start, end, out, ClipConfig(video_preset="ultrafast"))
        assert out.exists() and out.stat().st_size > 1000
        assert real[1] - real[0] == pytest.approx(end - start, abs=0.01)
    finally:
        rec.stop()

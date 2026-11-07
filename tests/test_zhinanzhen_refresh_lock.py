"""指南针开着时独占 day.vdat：不当成没有数据，退出后再读（全部打桩，不启动指南针）。"""

import modules.zhinanzhen_refresh as zr


def _stub(monkeypatch, reads, mtime=10_000.0):
    calls = {"closed": 0, "reads": 0}
    seq = list(reads)

    def read():
        calls["reads"] += 1
        return seq.pop(0) if seq else seq_last[0]

    seq_last = [reads[-1]]
    clock = {"t": 1000.0}
    monkeypatch.setattr(zr, "read_amv_date", read)
    monkeypatch.setattr(zr, "latest_amv_date", lambda: "2026-11-03")
    monkeypatch.setattr(zr, "amv_file_mtime", lambda: mtime)
    monkeypatch.setattr(zr, "_wavmain_pids", lambda: [])
    monkeypatch.setattr(zr, "wavmain_exe", lambda: zr.Path(__file__))
    monkeypatch.setattr(zr.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 4242})())
    monkeypatch.setattr(zr, "_launch", lambda exe: 4242)
    monkeypatch.setattr(zr, "open_amv_page", lambda pid, timeout=60: True)

    def close(pid, timeout=60):
        calls["closed"] += 1
        return True

    monkeypatch.setattr(zr, "_close_gracefully", close)
    monkeypatch.setattr(zr.time, "time", lambda: clock["t"])
    monkeypatch.setattr(zr.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return calls


def test_opens_queries_waits_briefly_then_reads_after_exit(monkeypatch):
    # 指南针开着时文件被独占；打开 0AMV 等 15 秒就退出，退出后读到 11-04
    calls = _stub(monkeypatch, [("2026-11-04", False)])
    result = zr.refresh_amv("2026-11-04")
    assert result.updated and result.after == "2026-11-04" and not result.error
    assert result.waited_seconds <= zr.STARTUP_SETTLE + zr.AMV_PAGE_WAIT + 5
    assert calls["closed"] == 1


def test_retries_once_with_longer_wait_then_reports(monkeypatch):
    calls = _stub(monkeypatch, [("2026-11-03", False)])
    result = zr.refresh_amv("2026-11-04")
    assert not result.updated and "2026-11-03" in result.error and "开了 2 次" in result.error
    assert calls["closed"] == 2


def test_second_round_can_succeed(monkeypatch):
    calls = _stub(monkeypatch, [("2026-11-03", False), ("2026-11-04", False)])
    result = zr.refresh_amv("2026-11-04")
    assert result.updated and calls["closed"] == 2

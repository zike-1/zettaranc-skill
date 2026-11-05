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
    monkeypatch.setattr(zr, "open_amv_page", lambda pid, timeout=60: True)

    def close(pid, timeout=60):
        calls["closed"] += 1
        return True

    monkeypatch.setattr(zr, "_close_gracefully", close)
    monkeypatch.setattr(zr.time, "time", lambda: clock["t"])
    monkeypatch.setattr(zr.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return calls


def test_locked_file_written_after_page_opens_is_read_after_exit(monkeypatch):
    # 指南针开着：一直读不了（占用），但文件在打开页面后被写过；退出后读到 11-04
    calls = _stub(monkeypatch, [("", True), ("", True), ("2026-11-04", False)], mtime=99_999.0)
    result = zr.refresh_amv("2026-11-04", wait_seconds=180)
    assert result.updated and result.after == "2026-11-04" and not result.error
    assert result.waited_seconds < 30  # 不再空等 180 秒
    assert calls["closed"] == 1


def test_locked_but_not_written_still_reports_after_exit(monkeypatch):
    # 文件一直没被写过：等到超时，退出后读到的还是旧日期，如实报错
    calls = _stub(monkeypatch, [("", True)] * 100 + [("2026-11-03", False)], mtime=1.0)
    result = zr.refresh_amv("2026-11-04", wait_seconds=30)
    assert not result.updated and "2026-11-03" in result.error or "无数据" in result.error
    assert calls["closed"] == 1

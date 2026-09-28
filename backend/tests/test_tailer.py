"""Asynchronous file-following tests for LogTailer."""
import asyncio

import pytest

from backend.app.ingest.tailer import LogTailer


POLL_INTERVAL_S = 0.01


async def _next_line(lines):
    return await asyncio.wait_for(anext(lines), timeout=2.0)


async def _close_tailer(tailer: LogTailer, lines) -> None:
    tailer.stop()
    await lines.aclose()


@pytest.mark.asyncio
async def test_reads_lines_appended_after_start(tmp_path):
    path = tmp_path / "hospital.log"
    path.write_bytes(b"")
    tailer = LogTailer(path, poll_interval_s=POLL_INTERVAL_S, from_beginning=True)
    lines = tailer.lines()
    first_read = asyncio.create_task(_next_line(lines))

    await asyncio.sleep(0.03)
    with path.open("ab") as log_file:
        log_file.write(b"first appended line\nsecond appended line\n")

    try:
        assert await first_read == "first appended line"
        assert await _next_line(lines) == "second appended line"
    finally:
        await _close_tailer(tailer, lines)


@pytest.mark.asyncio
async def test_partial_line_waits_for_newline(tmp_path):
    path = tmp_path / "hospital.log"
    path.write_bytes(b"")
    tailer = LogTailer(path, poll_interval_s=POLL_INTERVAL_S, from_beginning=True)
    lines = tailer.lines()
    read_task = asyncio.create_task(_next_line(lines))

    await asyncio.sleep(0.03)
    with path.open("ab") as log_file:
        log_file.write(b"partial record")
    await asyncio.sleep(0.05)
    assert not read_task.done()

    with path.open("ab") as log_file:
        log_file.write(b" completed\n")
    try:
        assert await read_task == "partial record completed"
    finally:
        await _close_tailer(tailer, lines)


@pytest.mark.asyncio
async def test_truncation_restarts_from_new_file_start(tmp_path):
    path = tmp_path / "hospital.log"
    path.write_bytes(b"previous record longer than new record\n")
    tailer = LogTailer(path, poll_interval_s=POLL_INTERVAL_S, from_beginning=True)
    lines = tailer.lines()

    try:
        assert await _next_line(lines) == "previous record longer than new record"
        path.write_bytes(b"new\n")
        assert await _next_line(lines) == "new"
    finally:
        await _close_tailer(tailer, lines)


@pytest.mark.asyncio
async def test_rotation_follows_replacement_file(tmp_path):
    path = tmp_path / "hospital.log"
    rotated = tmp_path / "hospital.log.1"
    path.write_bytes(b"before rotation\n")
    tailer = LogTailer(path, poll_interval_s=POLL_INTERVAL_S, from_beginning=True)
    lines = tailer.lines()

    try:
        assert await _next_line(lines) == "before rotation"
        path.replace(rotated)
        path.write_bytes(b"after rotation\n")
        assert await _next_line(lines) == "after rotation"
    finally:
        await _close_tailer(tailer, lines)


@pytest.mark.asyncio
async def test_resumes_from_saved_byte_offset(tmp_path):
    path = tmp_path / "hospital.log"
    state_path = tmp_path / "tailer-state.json"
    path.write_bytes(b"first line\nsecond line\n")
    tailer = LogTailer(
        path,
        state_path=state_path,
        poll_interval_s=POLL_INTERVAL_S,
        from_beginning=True,
        state_save_interval_s=0.0,
    )
    lines = tailer.lines()

    try:
        assert await _next_line(lines) == "first line"
    finally:
        await _close_tailer(tailer, lines)

    resumed = LogTailer(path, state_path=state_path, poll_interval_s=POLL_INTERVAL_S)
    resumed_lines = resumed.lines()
    try:
        assert await _next_line(resumed_lines) == "second line"
    finally:
        await _close_tailer(resumed, resumed_lines)
import asyncio
from dataclasses import replace

import pytest
from aiohttp import web
from conftest import module
from test_adapter import message
from test_connection_alerts import chat_event
from test_stickers import deliver, find, send_event, send_sticker


class LibraryAPI:
    def __init__(self, native):
        self.native = native
        self.jobs = {}
        self.states = {}
        self.faults = {}
        self.sync_finished = False
        self.sync_immediate = False
        self.submitted = asyncio.Event()

    def receipt(self, ident, *, initial=False):
        pack_id = self.jobs[ident]
        if not pack_id and self.sync_immediate:
            initial = False
        state = "running" if initial else self.states.get(pack_id, "available")
        payload = {"serverAcceptance": "accepted", "profileWriteback": "complete"}
        if pack_id:
            payload.update(packId=pack_id, membership="added", contentReadiness="available")
        if state == "available" and not initial and pack_id not in self.faults:
            if pack_id:
                for pack in self.native.packs:
                    if pack["id"] == pack_id:
                        pack["contentReadiness"] = "available"
                self.native.pack_items[0]["original"] = dict(
                    self.native.asset, id="pack-original", messageId=None
                )
            else:
                self.sync_finished = True
        return {
            "id": ident,
            "accountId": "a",
            "kind": "sticker_acquire" if pack_id else "sticker_pack",
            "state": state,
            "phase": "completed" if state == "available" else "downloading",
            "result": payload,
            **self.faults.get(pack_id, {}),
        }

    async def handle(self, req):
        if req.method == "POST" and req.path.endswith(("/sticker-downloads", "/sticker-syncs")):
            body = await req.json()
            self.native.calls.append(
                (req.method, req.path, body, req.headers.get("Idempotency-Key"))
            )
            pack_id = body.get("packId", "")
            if pack_id:
                assert self.sync_finished
            ident = "download-" + pack_id if pack_id else "sync-job"
            self.jobs[ident] = pack_id
            self.submitted.set()
            return web.json_response(self.receipt(ident, initial=True), status=202)
        if "/jobs/" in req.path:
            self.native.calls.append((req.method, req.path, None, None))
            return web.json_response(self.receipt(req.path.rsplit("/", 1)[-1]))
        return await self.native.handle(req)


async def setup(serve, tmp_path, monkeypatch):
    native, adapter, event = await send_event(serve, tmp_path)
    event.role = "admin"
    native.packs[0]["contentReadiness"] = "unavailable"
    native.pack_items[0]["original"] = None
    api = LibraryAPI(native)
    adapter.client.api_url = await serve(api.handle)
    native.calls.clear()
    monkeypatch.setattr(module("sticker_commands"), "POLL_INTERVAL_SECONDS", 0.001, raising=False)
    return native, adapter, event, api


async def command(event):
    return await module("sticker_commands").run_sticker_command(event)


async def next_event(native, adapter, ident="m2"):
    native.raw = message(id=ident, serverId=ident)
    event = await deliver(adapter, native.raw)
    event.role = "admin"
    return event


async def test_only_sync_command_is_registered_and_sends_start_then_summary(
    serve, tmp_path, monkeypatch
):
    from astrbot.core.star import command_management

    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)

        async def no_overrides():
            return []

        monkeypatch.setattr(command_management.db_helper, "get_command_configs", no_overrides)
        registered = {
            c["effective_command"]: c
            for c in await command_management.list_commands()
            if c["module_path"] == "wechat_plugin.main"
        }
        assert "表情包同步" in registered
        assert "表情包" not in registered
        replies = [r async for r in plugin.sticker_library(event)]
        assert "下载成功 1" in replies[-1].get_plain_text()
        texts = [c[2].get("text", "") for c in native.calls if c[0] == "POST" and c[2]]
        assert any("开始" in t and "自动" in t for t in texts)
        assert event.is_stopped()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("source", ["member", "group", "foreign", "disabled", "unowned"])
async def test_sync_rejects_unauthorized_events_before_http(serve, tmp_path, monkeypatch, source):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    if source == "member":
        event.role = "member"
    elif source == "group":
        native.raw = message(id="g1", threadId="room@chatroom")
        native.raw["mentions"]["self"] = True
        event = await deliver(adapter, native.raw)
        event.role = "admin"
    elif source == "foreign":
        event = chat_event()
    elif source == "disabled":
        adapter.native_config = replace(adapter.native_config, enable_stickers=False)
    else:
        adapter._events.clear()
    native.calls.clear()
    try:
        text = await command(event)
        assert "无法" in text or "仅允许" in text or "开启" in text
        assert not native.calls
    finally:
        await adapter.terminate()


async def test_sync_waits_for_jobs_and_enables_existing_find_and_send(serve, tmp_path, monkeypatch):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    try:
        text = await command(event)
        assert "同步完成" in text and "下载成功 1" in text
        posts = [c for c in native.calls if c[0] == "POST"]
        assert [c[2] for c in posts] == [{"scope": "all"}, {"packId": "pack1"}]
        assert posts[0][1].endswith("/accounts/a/sticker-syncs")
        assert len({c[3] for c in posts}) == 2
        assert adapter.state.sticker_operation_by_job("a", "sync-job")["state"] == "success"
        assert adapter.state.sticker_operation_by_job("a", "download-pack1")["state"] == "success"
        await command(event)
        assert len([c for c in native.calls if c[0] == "POST"]) == 2
        chat = await next_event(native, adapter)
        choice = (await find(chat, "晚安"))["items"][0]
        assert choice["source"] == "pack"
        assert (await send_sticker(chat, choice["selection_id"]))["status"] == "success"
    finally:
        await adapter.terminate()


async def test_sync_downloads_all_unready_added_packs_and_continues_after_failure(
    serve, tmp_path, monkeypatch
):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    native.packs += [
        {"id": "ready", "name": "已就绪", "membership": "added", "contentReadiness": "available"},
        {"id": "broken", "name": "失败包", "membership": "added"},
        {"id": "last", "name": "最后一包", "membership": "added", "contentReadiness": "partial"},
        {"id": "unadded", "membership": "not_added"},
    ]
    api.states["broken"] = "failed"
    try:
        text = await command(event)
        assert "下载成功 2" in text and "已就绪 1" in text and "失败 1" in text
        assert "broken" in text and "download-broken" in text
        assert "全部完成" not in text
        posts = [c for c in native.calls if c[0] == "POST"]
        assert [c[2] for c in posts] == [
            {"scope": "all"},
            {"packId": "pack1"},
            {"packId": "broken"},
            {"packId": "last"},
        ]
        assert len({c[3] for c in posts}) == 4
    finally:
        await adapter.terminate()


async def test_sync_failure_never_starts_downloads(serve, tmp_path, monkeypatch):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    api.states[""] = "failed"
    try:
        assert "同步任务失败" in await command(event)
        assert [c[2] for c in native.calls if c[0] == "POST"] == [{"scope": "all"}]
    finally:
        await adapter.terminate()


async def test_sync_reports_unfinished_download_without_resubmitting(serve, tmp_path, monkeypatch):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    api.sync_immediate = True
    monkeypatch.setattr(module("sticker_commands"), "JOB_WAIT_SECONDS", 0, raising=False)
    api.states["pack1"] = "running"
    try:
        text = await command(event)
        assert "未完成 1" in text and "download-pack1" in text and "全部完成" not in text
        await command(await next_event(native, adapter))
        assert len([c for c in native.calls if c[0] == "POST" and c[2].get("packId")]) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "fault",
    [
        {"accountId": "foreign"},
        {"id": "foreign-job"},
        {"kind": "sticker_send"},
        {
            "result": {
                "packId": "other",
                "serverAcceptance": "accepted",
                "profileWriteback": "complete",
            }
        },
        {
            "result": {
                "packId": "pack1",
                "serverAcceptance": "accepted",
                "profileWriteback": "pending",
            }
        },
    ],
)
async def test_unverified_receipt_never_reports_success_or_reposts(
    serve, tmp_path, monkeypatch, fault
):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    api.faults["pack1"] = fault
    try:
        assert "待核对" in await command(event)
        assert "待核对" in await command(await next_event(native, adapter))
        assert len([c for c in native.calls if c[0] == "POST" and c[2].get("packId")]) == 1
    finally:
        await adapter.terminate()


async def test_sync_honors_legacy_unknown_download_without_post(serve, tmp_path, monkeypatch):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    row, _ = adapter.stickers.claim(native.raw, "library:download", "pack1", {"packId": "pack1"})
    adapter.state.finish_sticker(row["idempotency_key"], "needs_review")
    try:
        text = await command(await next_event(native, adapter))
        assert "未完成 1" in text and "待核对" in text
        assert [c[2] for c in native.calls if c[0] == "POST"] == [{"scope": "all"}]
    finally:
        await adapter.terminate()


async def test_legacy_download_recovers_after_restart_without_post(serve, tmp_path, monkeypatch):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    row, _ = adapter.stickers.claim(native.raw, "library:download", "pack1", {"packId": "pack1"})
    adapter.state.finish_sticker(row["idempotency_key"], "pending", "download-pack1")
    api.jobs["download-pack1"] = "pack1"
    try:
        config, api_url = dict(adapter.config), adapter.client.api_url
        await adapter.terminate()
        adapter = module("adapter").WeChatNativeAdapter(
            config, {}, asyncio.Queue(), state_dir=tmp_path
        )
        adapter.bot_self_id = "bot"
        await adapter.client.open()
        adapter.client.api_url = api_url
        await adapter.stickers.recover()
        assert adapter.state.sticker_operation_by_job("a", "download-pack1")["state"] == "success"
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_concurrent_sync_gets_one_pipeline_and_cancellation_stops_downloads(
    serve, tmp_path, monkeypatch
):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    api.states[""] = "running"
    task = asyncio.create_task(command(event))
    try:
        await asyncio.wait_for(api.submitted.wait(), 2)
        text = await command(await next_event(native, adapter))
        assert "正在" in text and "自动" in text
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task not in adapter._send_tasks
        assert [c[2] for c in native.calls if c[0] == "POST"] == [{"scope": "all"}]
        api.states[""] = "available"
        assert "下载成功 1" in await command(await next_event(native, adapter, "m3"))
        assert len([c for c in native.calls if c[0] == "POST"]) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.terminate()


async def test_cancelled_unknown_admission_is_not_reposted(serve, tmp_path, monkeypatch):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    entered = asyncio.Event()
    original = adapter.client._request

    async def blocked(method, path, **kwargs):
        if method == "POST":
            entered.set()
            await asyncio.Future()
        return await original(method, path, **kwargs)

    adapter.client._request = blocked
    task = asyncio.create_task(command(event))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task not in adapter._send_tasks
        text = await command(await next_event(native, adapter))
        assert "待核对" in text
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.terminate()


async def test_sync_rechecks_permissions_after_polling_before_downloading(
    serve, tmp_path, monkeypatch
):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    original = adapter.client.job

    async def revoke(ident):
        job = await original(ident)
        adapter.native_config = replace(adapter.native_config, enable_stickers=False)
        return job

    adapter.client.job = revoke
    try:
        assert "无法" in await command(event)
        assert [c[2] for c in native.calls if c[0] == "POST"] == [{"scope": "all"}]
    finally:
        await adapter.terminate()


def test_library_claim_deduplicates_new_and_legacy_operations_across_connections(tmp_path):
    State = module("state").StateStore
    first, second = State(tmp_path / "state.sqlite3"), State(tmp_path / "state.sqlite3")
    try:
        one, won = first.claim_sticker("a", "p1", "m1", "library:download", "pack1", "k1", "h", 1)
        assert won
        first.finish_sticker("k1", "needs_review")
        two, won = second.claim_sticker(
            "a", "p2", "m2", "library:download:pack1", "pack1", "k2", "h", 2
        )
        assert not won and two["idempotency_key"] == one["idempotency_key"]
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("empty", [False, True])
async def test_sync_skips_ready_packs_and_handles_empty_collection(
    serve, tmp_path, monkeypatch, empty
):
    native, adapter, event, _ = await setup(serve, tmp_path, monkeypatch)
    if empty:
        native.packs = []
    else:
        native.packs[0]["contentReadiness"] = "available"
    try:
        text = await command(event)
        if empty:
            assert "没有已添加" in text
        else:
            assert "已就绪 1" in text and "下载成功 0" in text
        assert [c[2] for c in native.calls if c[0] == "POST"] == [{"scope": "all"}]
    finally:
        await adapter.terminate()


async def test_shutdown_cancels_polling_after_real_start_acknowledgement(
    serve, tmp_path, monkeypatch
):
    native, adapter, event, api = await setup(serve, tmp_path, monkeypatch)
    api.states[""] = "running"
    plugin = object.__new__(module("main").WeChatNativePlugin)

    async def consume():
        return [r async for r in plugin.sticker_library(event)]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(api.submitted.wait(), 2)
        assert task in adapter._send_tasks
        await adapter.terminate()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert event.is_stopped()
        assert not adapter._sticker_sync_running
        assert [c[2] for c in native.calls if c[1].endswith("/sticker-downloads")] == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.terminate()

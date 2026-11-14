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
        self.state = "available"
        self.fault = {}
        self.status = 202
        self.kind = "sticker_acquire"

    def job(self):
        original = dict(self.native.asset, id="pack-original", messageId=None)
        payload = {"serverAcceptance": "reconciled_noop", "profileWriteback": "complete"}
        if self.kind == "sticker_acquire":
            payload.update(
                packId="pack1",
                serverAcceptance="accepted",
                membership="added",
                contentReadiness="available",
                items=[original],
            )
            if self.state == "available":
                self.native.pack_items[0]["original"] = original
        return {
            "id": "library-job",
            "accountId": "a",
            "kind": self.kind,
            "state": self.state,
            "phase": "completed" if self.state == "available" else "downloading",
            "result": payload,
            **self.fault,
        }

    async def handle(self, req):
        if req.method == "POST" and req.path.endswith(("/sticker-downloads", "/sticker-syncs")):
            self.native.calls.append(
                (req.method, req.path, await req.json(), req.headers.get("Idempotency-Key"))
            )
            self.kind = "sticker_pack" if req.path.endswith("/sticker-syncs") else "sticker_acquire"
            if self.status != 202:
                return web.json_response({"error": {"code": "request_failed"}}, status=self.status)
            return web.json_response(self.job(), status=202)
        if req.path.endswith("/jobs/library-job"):
            self.native.calls.append((req.method, req.path, None, None))
            return web.json_response(self.job())
        return await self.native.handle(req)


async def setup(serve, tmp_path):
    native, adapter, event = await send_event(serve, tmp_path)
    event.role = "admin"
    native.packs[0]["contentReadiness"] = "unavailable"
    native.pack_items[0]["original"] = None
    api = LibraryAPI(native)
    adapter.client.api_url = await serve(api.handle)
    native.calls.clear()
    return native, adapter, event, api


async def command(event, action="帮助", target=""):
    return await module("sticker_commands").run_sticker_command(event, action, target)


async def next_event(native, adapter, ident="m2"):
    native.raw = message(id=ident, serverId=ident)
    event = await deliver(adapter, native.raw)
    event.role = "admin"
    return event


async def test_private_command_help_is_registered_and_stops_model_dispatch(
    serve, tmp_path, monkeypatch
):
    from astrbot.core.star import command_management

    native, adapter, event, _ = await setup(serve, tmp_path)
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
        assert "stickers" in registered["表情包"]["aliases"]
        replies = [r async for r in plugin.sticker_library(event)]
        assert "表情包 下载" in replies[0].get_plain_text()
        assert event.is_stopped()
        assert not native.calls
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("source", ["member", "group", "foreign", "disabled", "unowned"])
async def test_management_rejects_unauthorized_events_before_http(serve, tmp_path, source):
    native, adapter, event, _ = await setup(serve, tmp_path)
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
        text = await command(event, "同步")
        assert "无法" in text or "仅允许" in text or "开启" in text
        assert not native.calls
    finally:
        await adapter.terminate()


async def test_list_is_read_only_and_reports_added_pack_id_and_readiness(serve, tmp_path):
    native, adapter, event, _ = await setup(serve, tmp_path)
    try:
        text = await command(event, "列表")
        assert "pack1" in text and "小猫" in text and "未下载" in text
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_sync_uses_account_api_and_retains_receipt(serve, tmp_path):
    native, adapter, event, _ = await setup(serve, tmp_path)
    try:
        text = await command(event, "同步")
        assert "同步完成" in text and "library-job" in text
        posts = [c for c in native.calls if c[0] == "POST"]
        assert len(posts) == 1 and posts[0][1].endswith("/accounts/a/sticker-syncs")
        assert posts[0][2] == {"scope": "all"} and posts[0][3]
        await command(event, "同步")
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_whole_pack_download_enables_existing_find_and_native_send(serve, tmp_path):
    native, adapter, event, _ = await setup(serve, tmp_path)
    try:
        text = await command(event, "下载", "pack1")
        assert "下载完成" in text and "library-job" in text
        posts = [c for c in native.calls if c[0] == "POST"]
        assert len(posts) == 1 and posts[0][2] == {"packId": "pack1"} and posts[0][3]
        chat = await next_event(native, adapter)
        choice = (await find(chat, "晚安"))["items"][0]
        assert choice["source"] == "pack"
        assert (await send_sticker(chat, choice["selection_id"]))["status"] == "success"
        assert not any(c[1].endswith("/custom-stickers") and c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("target", ["", "not-added", "bad\npack", "x" * 1025])
async def test_download_rejects_invalid_or_unadded_pack_without_post(serve, tmp_path, target):
    native, adapter, event, _ = await setup(serve, tmp_path)
    try:
        text = await command(event, "下载", target)
        assert "下载完成" not in text
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_running_download_status_and_concurrent_commands_only_submit_once(serve, tmp_path):
    native, adapter, event, api = await setup(serve, tmp_path)
    api.state = "running"
    other = await next_event(native, adapter)
    try:
        texts = await asyncio.gather(
            command(event, "下载", "pack1"), command(other, "下载", "pack1")
        )
        assert all("处理中" in t for t in texts)
        assert any("library-job" in t for t in texts)
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
        assert "处理中" in await command(other, "状态", "library-job")
        api.state = "available"
        assert "下载完成" in await command(other, "状态", "library-job")
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "fault",
    [
        {"accountId": "foreign"},
        {"id": ""},
        {"kind": "sticker_send"},
        {"result": {"packId": "other", "serverAcceptance": "accepted"}},
        {
            "result": {
                "packId": "pack1",
                "serverAcceptance": "accepted",
                "profileWriteback": "pending",
            }
        },
    ],
)
async def test_unverified_download_receipt_never_reports_success_or_reposts(serve, tmp_path, fault):
    native, adapter, event, api = await setup(serve, tmp_path)
    api.fault = fault
    try:
        assert "待核对" in await command(event, "下载", "pack1")
        later = await next_event(native, adapter)
        assert "待核对" in await command(later, "下载", "pack1")
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_unknown_admission_is_not_reposted_after_restart(serve, tmp_path):
    native, adapter, event, api = await setup(serve, tmp_path)
    api.status = 503
    try:
        assert "待核对" in await command(event, "下载", "pack1")
        config, api_url = dict(adapter.config), adapter.client.api_url
        await adapter.terminate()
        adapter = module("adapter").WeChatNativeAdapter(
            config, {}, asyncio.Queue(), state_dir=tmp_path
        )
        adapter.bot_self_id = "bot"
        await adapter.client.open()
        adapter.client.api_url = api_url
        await adapter.stickers.recover()
        later = await next_event(native, adapter)
        assert "待核对" in await command(later, "下载", "pack1")
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_status_cannot_read_unretained_job(serve, tmp_path):
    native, adapter, event, _ = await setup(serve, tmp_path)
    try:
        assert "找不到" in await command(event, "状态", "foreign-job")
        assert not native.calls
    finally:
        await adapter.terminate()


async def test_known_download_job_recovers_after_restart_with_reads_only(serve, tmp_path):
    native, adapter, event, api = await setup(serve, tmp_path)
    api.state = "running"
    try:
        assert "处理中" in await command(event, "下载", "pack1")
        config, api_url = dict(adapter.config), adapter.client.api_url
        await adapter.terminate()
        adapter = module("adapter").WeChatNativeAdapter(
            config, {}, asyncio.Queue(), state_dir=tmp_path
        )
        adapter.bot_self_id = "bot"
        await adapter.client.open()
        adapter.client.api_url = api_url
        api.state = "available"
        await adapter.stickers.recover()
        row = adapter.state.sticker_operation_by_job("a", "library-job")
        assert row["state"] == "success"
        assert len([c for c in native.calls if c[0] == "POST"]) == 1
    finally:
        await adapter.terminate()


async def test_cancellation_preserves_unknown_intent_and_removes_active_task(serve, tmp_path):
    native, adapter, event, api = await setup(serve, tmp_path)
    entered = asyncio.Event()
    original_request = adapter.client._request

    async def blocked(method, path, **kwargs):
        if method == "POST":
            entered.set()
            await asyncio.Future()
        return await original_request(method, path, **kwargs)

    adapter.client._request = blocked
    try:
        task = asyncio.create_task(command(event, "下载", "pack1"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task not in adapter._send_tasks
        row = adapter.state.sticker_operation("a", "peer", "m1", "library:download")
        assert row["state"] == "needs_review"
        assert "待核对" in await command(await next_event(native, adapter), "下载", "pack1")
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


async def test_download_rechecks_feature_after_loading_membership(serve, tmp_path):
    native, adapter, event, _ = await setup(serve, tmp_path)
    original = adapter.client.sticker_packs

    async def revoked():
        data = await original()
        adapter.native_config = replace(adapter.native_config, enable_stickers=False)
        return data

    adapter.client.sticker_packs = revoked
    try:
        assert "无法" in await command(event, "下载", "pack1")
        assert not any(c[0] == "POST" for c in native.calls)
    finally:
        await adapter.terminate()


def test_library_claim_is_shared_across_database_connections(tmp_path):
    State = module("state").StateStore
    first, second = State(tmp_path / "state.sqlite3"), State(tmp_path / "state.sqlite3")
    try:
        one, won = first.claim_sticker("a", "p1", "m1", "library:download", "pack1", "k1", "h", 1)
        assert won
        first.finish_sticker("k1", "needs_review")
        two, won = second.claim_sticker("a", "p2", "m2", "library:download", "pack1", "k2", "h", 2)
        assert not won and two["idempotency_key"] == one["idempotency_key"]
    finally:
        first.close()
        second.close()

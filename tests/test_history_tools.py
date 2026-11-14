import json

import pytest
from aiohttp import web
from conftest import module
from test_adapter import make_adapter, message, native_event


async def tool_event(serve, tmp_path, **options):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, **options)
    await adapter.process_native_event(native_event(msg))
    return adapter, queue.get_nowait(), msg


async def test_recent_history_is_scoped_and_disabled_by_default(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["status"] == "not_available"
        assert adapter.state.cursor == "1"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("unique", [True, False])
async def test_recent_page_hides_recall_and_binds_cursor(serve, tmp_path, unique):
    adapter, event, msg = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        unique_session=unique,
    )
    calls = []

    async def page(thread, *, limit, cursor="", query=""):
        calls.append((thread, limit, cursor, query))
        return {
            "items": [dict(msg, body="secret", recall={}), dict(msg, id="m2", body="a" * 900)],
            "nextCursor": "native-next",
        }

    adapter.client.messages = page
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["ok"]
        assert result["items"][0]["text"] == "[已撤回]"
        assert len(result["items"][1]["text"]) == 800
        assert "secret" not in json.dumps(result)
        await plugin.wechat_get_recent_messages(event, cursor=result["nextCursor"])
        assert calls[-1] == ("peer", 20, "native-next", "")
        bad = json.loads(await plugin.wechat_get_recent_messages(event, 10, result["nextCursor"]))
        assert bad["status"] == "invalid_cursor"
    finally:
        await adapter.terminate()


async def test_recent_uses_native_http_and_preserves_checkpoint(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    requests = []

    async def handler(req):
        requests.append((req.method, req.path, dict(req.query)))
        return web.json_response({"items": [msg], "nextCursor": None})

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_get_recent_messages(event))
        assert data["items"][0]["text"] == "你好"
        assert requests == [("GET", "/accounts/a/threads/peer/messages", {"limit": "20"})]
        assert adapter.state.cursor == "1"
        for invalid in (True, 1.5, 0, 51, "20"):
            data = json.loads(await plugin.wechat_get_recent_messages(event, invalid))
            assert data["status"] == "invalid_request"
        assert len(requests) == 1
    finally:
        await adapter.terminate()


async def test_foreign_and_tampered_events_cannot_read(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    calls = []

    async def page(*args, **kwargs):
        calls.append(True)
        return {"items": [dict(msg, threadId="other")], "nextCursor": None}

    adapter.client.messages = page
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        assert (
            json.loads(await plugin.wechat_get_recent_messages(object()))["status"]
            == "not_available"
        )
        assert (
            json.loads(await plugin.wechat_get_recent_messages(event))["status"] == "not_available"
        )
        assert len(calls) == 1
        event.message_obj.raw_message["threadId"] = "other"
        assert (
            json.loads(await plugin.wechat_get_recent_messages(event))["status"]
            == "permission_denied"
        )
        assert len(calls) == 1
    finally:
        await adapter.terminate()


async def test_search_encodes_chinese_and_only_queries_current_thread(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    requests = []

    async def handler(req):
        requests.append((req.path, dict(req.query)))
        return web.json_response({"items": [], "nextCursor": None})

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_search_messages(event, "  中文 Docker &  "))
        assert data["ok"] and data["count"] == 0
        assert data["coverage"] == "local_history_may_be_partial"
        assert requests == [
            ("/accounts/a/threads/peer/messages", {"limit": "20", "query": "中文 Docker &"})
        ]
        for query in (" ", "中" * 86, None):
            data = json.loads(await plugin.wechat_search_messages(event, query))
            assert data["status"] == "invalid_request"
        assert len(requests) == 1
    finally:
        await adapter.terminate()


async def test_group_info_preserves_unknowns_and_rejects_private(serve, tmp_path):
    adapter, event, _ = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
    )

    async def handler(req):
        assert req.path.endswith("/groups/room@chatroom")
        return web.json_response(
            {
                "id": "room@chatroom",
                "roomId": "room@chatroom",
                "displayName": "开发群",
                "ownerId": None,
                "announcement": "x" * 900,
            }
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_get_group_info(event))
        assert data["ok"]
        assert data["group"]["ownerId"] is None
        assert len(data["group"]["announcement"]) == 800
        assert data["truncated"]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("query", ["  alice dev  ", "中文 &"])
async def test_member_search_uses_native_search_on_every_page(serve, tmp_path, query):
    adapter, event, _ = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
    )
    requests = []

    async def handler(req):
        requests.append((req.path, dict(req.query)))
        if req.query.get("search") != query.strip():
            return web.json_response(
                {
                    "items": [{"id": "u2", "username": "u2", "displayName": "Bob"}],
                    "nextCursor": "unfiltered-next",
                }
            )
        return web.json_response(
            {
                "items": [
                    {
                        "id": "u1",
                        "username": "u1",
                        "displayName": "备注",
                        "roomDisplayName": "群昵称",
                        "nickname": "Alice 中文",
                        "alias": "dev&id",
                        "mentionName": "群昵称",
                        "version": "v1",
                        "privateRemark": "secret",
                    },
                ],
                "nextCursor": None if req.query.get("cursor") else "members-next",
            }
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_list_group_members(event, query))
        assert data["ok"] and data["count"] == 1
        assert data["items"][0]["id"] == "u1"
        assert data["items"][0]["nickname"] == "Alice 中文"
        assert data["items"][0]["alias"] == "dev&id"
        assert data["coverage"] == "saved_members_may_be_partial"
        assert data["hasMore"]
        assert "secret" not in json.dumps(data)
        assert requests == [
            ("/accounts/a/groups/room@chatroom/members", {"limit": "20", "search": query.strip()})
        ]
        bad = json.loads(
            await plugin.wechat_list_group_members(event, "other", cursor=data["nextCursor"])
        )
        assert bad["status"] == "invalid_cursor"
        assert len(requests) == 1
        second = json.loads(
            await plugin.wechat_list_group_members(event, query, cursor=data["nextCursor"])
        )
        assert second["count"] == 1 and not second["hasMore"]
        assert second["nextCursor"] is None
        assert requests[-1] == (
            "/accounts/a/groups/room@chatroom/members",
            {"limit": "20", "cursor": "members-next", "search": query.strip()},
        )
    finally:
        await adapter.terminate()


async def test_member_list_without_search_accepts_older_member_fields(serve, tmp_path):
    adapter, event, _ = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
    )
    requests = []

    async def handler(req):
        requests.append(dict(req.query))
        return web.json_response(
            {
                "items": [{"id": "u1", "username": "u1", "displayName": "Alice"}],
                "nextCursor": None,
            }
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_list_group_members(event, "  "))
        assert data["ok"] and data["count"] == 1
        assert data["items"][0]["nickname"] is None
        assert data["items"][0]["alias"] is None
        assert requests == [{"limit": "20"}]
        for query in (None, "中" * 86):
            bad = json.loads(await plugin.wechat_list_group_members(event, query))
            assert bad["status"] == "invalid_request"
        assert len(requests) == 1
    finally:
        await adapter.terminate()


async def test_group_info_in_private_chat_is_rejected(serve, tmp_path):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_get_group_info(event))
        assert data["status"] == "not_in_group_chat"
    finally:
        await adapter.terminate()


async def test_native_expired_cursor_is_invalid_cursor(serve, tmp_path):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)

    async def handler(req):
        if req.query.get("cursor"):
            return web.json_response({"error": {"code": "invalid_cursor"}}, status=400)
        return web.json_response({"items": [], "nextCursor": "native-old"})

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        first = json.loads(await plugin.wechat_get_recent_messages(event))
        data = json.loads(
            await plugin.wechat_get_recent_messages(event, cursor=first["nextCursor"])
        )
        assert data["status"] == "invalid_cursor"
    finally:
        await adapter.terminate()

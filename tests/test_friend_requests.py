import asyncio
import json
import logging
import time

import pytest
from aiohttp import web
from conftest import module
from test_adapter import message
from test_connection_alerts import AlertContext, chat_event
from test_connection_recovery import MemoryKV
from test_history_tools import tool_event


def contact_operation(action, **outcome):
    return {
        "id": "op-1",
        "accountId": "a",
        "kind": "contact_mutation",
        "state": "succeeded",
        "result": {"action": action, "remoteOutcome": "accepted", **outcome},
    }


async def test_search_and_send_checks_scope_and_recovers_lost_admission(serve, tmp_path):
    token = tmp_path / "admin-token"
    token.write_text("synthetic-admin")
    adapter, event, _ = await tool_event(
        serve, tmp_path, enable_friend_requests=True, admin_token_file=str(token)
    )
    event.role = "admin"
    plugin = object.__new__(module("main").WeChatNativePlugin)
    posts = []

    async def native(req):
        assert req.path.startswith("/v1/accounts/a/")
        if req.path.endswith("/search/contacts"):
            assert req.headers["Authorization"] == "Bearer fake"
            assert await req.json() == {"query": "alice_wechat"}
            return web.json_response(
                {
                    "displayName": "Alice",
                    "contactId": None,
                    "recipientRef": "native-ref",
                    "expiresAt": int(time.time()) + 60,
                }
            )
        assert req.headers["Authorization"] == "Bearer synthetic-admin"
        if req.method == "POST":
            posts.append((req.headers["Idempotency-Key"], await req.json()))
            return web.json_response({}, status=503)
        assert req.path.endswith("/contact-mutations/by-idempotency-key")
        assert req.query["key"] == posts[0][0]
        return web.json_response(contact_operation("send_request"))

    try:
        url = await serve(native)
        adapter.client.api_url = adapter.admin_client.api_url = url + "/v1"
        assert not json.loads(await plugin.wechat_send_friend_request(event, "guessed"))["ok"]
        found = json.loads(await plugin.wechat_search_contact(event, "alice_wechat"))
        ref = found["recipientRef"]
        for _ in range(2):
            sent = json.loads(await plugin.wechat_send_friend_request(event, ref, "你好"))
            assert sent["ok"] and sent["operationId"] == "op-1"
        assert len(posts) == 1 and posts[0][0]
        assert posts[0][1] == {"recipientRef": ref, "message": "你好"}
    finally:
        await adapter.terminate()


def pending(request_id, **updates):
    return {
        "id": request_id,
        "direction": "incoming",
        "actionable": True,
        "isContact": False,
        "displayName": "Alice",
        "message": "你好",
        "updatedAt": 1760123123,
        "version": "new-version",
        **updates,
    }


async def test_list_followup_page_and_accept_current_version(serve, tmp_path):
    token = tmp_path / "admin-token"
    token.write_text("synthetic-admin")
    adapter, event, _ = await tool_event(
        serve, tmp_path, enable_friend_requests=True, admin_token_file=str(token)
    )
    event.role = "admin"
    plugin = object.__new__(module("main").WeChatNativePlugin)
    pages, posts = [], []
    version = "new-version"
    confirmed = False

    async def native(req):
        if req.method == "POST":
            assert req.headers["Authorization"] == "Bearer synthetic-admin"
            assert req.path.endswith("/contact-requests/fr_target/accept")
            posts.append((req.headers["Idempotency-Key"], (await req.json())["expectedVersion"]))
            if version == "new-version":
                return web.json_response({"error": {"code": "stale_contact_request"}}, status=409)
            return web.json_response(
                contact_operation("accept_request", requestId="fr_target"), status=202
            )
        if req.path.endswith("/operations/op-1"):
            return web.json_response(contact_operation("accept_request", requestId="fr_target"))
        assert req.headers["Authorization"] == "Bearer fake"
        if req.path.endswith("/contact-requests"):
            assert req.query["actionable"] == "true" and req.query["direction"] == "incoming"
            assert req.query["search"] == "Alice"
            pages.append(req.query.get("cursor"))
            return web.json_response(
                {"items": [pending("fr_target", version="old")], "nextCursor": None}
                if pages[-1]
                else {"items": [pending("fr_other", displayName="Bob")], "nextCursor": "page-2"}
            )
        return web.json_response(pending("fr_target", version=version, isContact=confirmed))

    try:
        url = await serve(native)
        adapter.client.api_url = adapter.admin_client.api_url = url + "/v1"
        listing = json.loads(await plugin.wechat_list_friend_requests(event, "Alice"))
        assert pages == [None, "page-2"]
        assert listing["items"] == [
            {
                "requestId": "fr_target",
                "displayName": "Alice",
                "message": "你好",
                "updatedAt": 1760123123,
            }
        ]
        stale = json.loads(await plugin.wechat_accept_friend_request(event, "fr_target"))
        assert stale["reason"] == "stale_contact_request"
        version = "next-version"
        accepted = json.loads(await plugin.wechat_accept_friend_request(event, "fr_target"))
        assert accepted["ok"] and "已提交" in accepted["message"]
        confirmed = True
        accepted = json.loads(await plugin.wechat_accept_friend_request(event, "fr_target"))
        assert accepted["message"] == "已成功添加好友。"
        assert [v for _, v in posts] == ["new-version", "next-version"]
        assert len({key for key, _ in posts}) == 2
    finally:
        await adapter.terminate()


async def subscription_setup(serve, tmp_path, monkeypatch):
    calls = []

    async def native(req):
        calls.append(req.path)
        if req.path == "/v1/accounts/a":
            return web.json_response({"id": "a", "authenticated": True, "connection": "online"})
        if req.path == "/v1/status":
            return web.json_response({"serverId": "server-a"})
        if req.path.endswith("/contact-requests"):
            return web.json_response(
                {"items": [pending("fr_baseline")], "asOfEventCursor": "4", "nextCursor": None}
            )
        if "/contact-requests/" in req.path:
            return web.json_response(pending(req.path.rsplit("/", 1)[1]))
        return web.json_response({"error": {"code": "event_cursor_expired"}}, status=410)

    sdk_context, kv = AlertContext(), MemoryKV()
    url = await serve(native)
    sdk_context.robots[0].update(server_url=url, enable_friend_requests=True)

    async def start():
        adapter = module("adapter").WeChatNativeAdapter(
            sdk_context.robots[0], {}, asyncio.Queue(), state_dir=tmp_path
        )
        await adapter.client.open()
        return adapter

    platform = sdk_context.get_platform_inst
    adapter = await start()
    monkeypatch.setattr(
        sdk_context,
        "get_platform_inst",
        lambda bot_id: adapter if bot_id == "native-1" else platform(bot_id),
    )
    plugin = object.__new__(module("main").WeChatNativePlugin)
    plugin.alert_bindings = module("alert_binding").AlertBindingService(sdk_context, kv)
    from astrbot.core.star.star import star_map

    monkeypatch.setattr(star_map[type(plugin).__module__], "star_cls", plugin)
    return adapter, plugin, sdk_context, kv, calls, start


def updated(cursor, request_id="fr_new", **source):
    return {
        "cursor": str(cursor),
        "kind": "friend_request.updated",
        "data": {"requestId": request_id, **(source or {"source": "remote_sync"})},
    }


async def reply(command, event, *args):
    responses = [response async for response in command(event, *args)]
    assert event.is_stopped()
    return responses[0].get_plain_text()


async def test_friend_events_use_existing_dispatcher_and_skip_local_baseline_and_duplicates(
    serve, tmp_path, monkeypatch
):
    adapter, plugin, sdk_context, _, calls, _ = await subscription_setup(
        serve, tmp_path, monkeypatch
    )
    try:
        assert "已订阅" in await reply(plugin.friend_subscribe, chat_event())
        assert adapter.state.cursor == "0"
        await adapter.process_native_event(updated(3, "fr_replayed"))
        await adapter.process_native_event(updated(5, "fr_baseline"))
        await adapter.process_native_event(updated(6, "fr_local", action="send_request"))
        await adapter.process_native_event(updated(7))
        await adapter.process_native_event(updated(8))
        assert [path.rsplit("/", 1)[-1] for path in calls] == ["contact-requests", "fr_new"]
        assert len(sdk_context.sent) == 1 and "Alice" in sdk_context.sent[0][1]
        assert adapter.state.cursor == "8"
        with pytest.raises(module("native_client").NativeAPIError, match="event_cursor_expired"):
            await adapter.run()
        assert calls.count("/v1/accounts/a/events/stream") == 1
        assert "游标已过期" in sdk_context.sent[-1][1]
        assert adapter.state.cursor == "8"
    finally:
        await adapter.terminate()


async def test_friend_commands_require_admin_private_and_keep_connection_binding(
    serve, tmp_path, monkeypatch
):
    adapter, plugin, _, kv, calls, _ = await subscription_setup(serve, tmp_path, monkeypatch)
    try:
        for command in (plugin.friend_status, plugin.friend_subscribe, plugin.friend_unsubscribe):
            assert "管理员私聊" in await reply(command, chat_event(admin=False))
            assert "管理员私聊" in await reply(command, chat_event(private=False))
        assert not calls
        assert "已绑定" in await reply(plugin.bind_alert, chat_event())
        assert "已订阅" in await reply(plugin.friend_subscribe, chat_event(), "native-1")
        assert "telegram" in await reply(plugin.friend_status, chat_event())
        assert "已取消订阅" in await reply(plugin.friend_unsubscribe, chat_event())
        assert "未订阅" in await reply(plugin.friend_status, chat_event())
        assert set(kv.data) == {"connection_alert_binding:native-1"}
    finally:
        await adapter.terminate()


async def test_friend_binding_and_failed_attempt_survive_restart(serve, tmp_path, monkeypatch):
    adapter, plugin, sdk_context, kv, _, start = await subscription_setup(
        serve, tmp_path, monkeypatch
    )
    try:
        assert "已订阅" in await reply(plugin.friend_subscribe, chat_event())
        sdk_context.send_result = False
        await adapter.process_native_event(updated(5))
        assert len(sdk_context.sent) == 1
    finally:
        await adapter.terminate()
    adapter = await start()
    try:
        plugin.alert_bindings = module("alert_binding").AlertBindingService(sdk_context, kv)
        sdk_context.send_result = True
        await adapter.process_native_event(updated(6))
        await adapter.process_native_event(updated(7, "fr_another"))
        assert len(sdk_context.sent) == 2
        assert adapter.state.cursor == "7"
    finally:
        await adapter.terminate()


async def test_friend_tool_filter_changes_only_the_current_provider_request(serve, tmp_path):
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.register import llm_tools

    adapter, event, _ = await tool_event(serve, tmp_path, enable_friend_requests=True)
    group_adapter, group_event, _ = await tool_event(
        serve,
        tmp_path,
        enable_friend_requests=True,
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
    )
    group_event.role = "admin"
    plugin = object.__new__(module("main").WeChatNativePlugin)
    tools = ToolSet(list(llm_tools.func_list))
    names = set(tools.names())
    friend_names = module("friend_requests").FRIEND_TOOLS
    try:
        for source in (event, group_event, chat_event()):
            req = ProviderRequest(func_tool=tools)
            await plugin.scope_friend_tools(source, req)
            assert set(req.func_tool.names()) == names - friend_names
            assert set(tools.names()) == names
            assert not json.loads(await plugin.wechat_list_friend_requests(source))["ok"]
        event.role = "admin"
        req = ProviderRequest(func_tool=tools)
        await plugin.scope_friend_tools(event, req)
        assert req.func_tool is tools and friend_names <= names
        output = json.loads(await plugin.wechat_send_friend_request(event, "ref"))
        assert output["status"] == "configuration_error"
    finally:
        await group_adapter.terminate()
        await adapter.terminate()


def test_friend_log_filter_omits_sdk_tool_payloads():
    log_filter = module("friend_requests").FriendToolLogFilter()
    for text in (
        "使用工具：wechat_send_friend_request，参数：private verification",
        "Tool `wechat_list_friend_requests` Result: private verification",
    ):
        record = logging.LogRecord(
            "astrbot", logging.INFO, "tool_loop_agent_runner.py", 1, text, (), None
        )
        assert not log_filter.filter(record)
    other_tool = logging.LogRecord(
        "astrbot",
        logging.INFO,
        "tool_loop_agent_runner.py",
        1,
        "Tool `wechat_list_group_members` Result: unchanged",
        (),
        None,
    )
    assert log_filter.filter(other_tool)

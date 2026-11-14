"""Administrator private-chat commands for preparing the Native sticker library."""

import asyncio

from astrbot.api import logger

from .native_client import NativeAPIError, NativeClient
from .state import clean_sticker_caption
from .tool_support import context, is_native_event, is_tool_error, result

HELP = (
    "表情包管理（管理员私聊）\n"
    "/表情包 同步：同步账号的收藏和已添加包目录\n"
    "/表情包 列表 [页码]：查看已添加包及 ID\n"
    "/表情包 下载 PACK_ID：下载一个已添加包到 Native 缓存\n"
    "/表情包 状态 JOB_ID：查看已提交任务\n"
    "先在手机微信添加表情包，再同步、查看列表并下载。"
)


def valid_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value.encode()) <= 1024
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    )


async def added_packs(adapter):
    page = await adapter.client.sticker_packs()
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise ValueError("invalid_sticker_pack_list")
    return [
        p
        for p in page["items"]
        if isinstance(p, dict) and p.get("membership") == "added" and valid_id(p.get("id"))
    ]


async def inspect_library_operation(adapter, row, job=None):
    action, key = row["action"], row["idempotency_key"]
    state, job_id = row["state"], row["job_id"]
    if state in {"success", "failed"}:
        return result(action, state, job_id=job_id)
    state = "needs_review"
    try:
        if job is None:
            if not job_id:
                return result(
                    action, "pending" if row["state"] == "submitting" else state, job_id=None
                )
            job = await adapter.client.job(job_id)
        expected_kind = "sticker_pack" if action == "library:sync" else "sticker_acquire"
        if (
            not isinstance(job, dict)
            or job.get("accountId") != row["account"]
            or job.get("kind") != expected_kind
            or not valid_id(job.get("id"))
            or (job_id and job["id"] != job_id)
        ):
            raise ValueError("invalid_sticker_library_receipt")
        job_id = job["id"]
        payload = job.get("result")
        payload = payload if isinstance(payload, dict) else {}
        if job.get("state") in {"queued", "running"}:
            state = "pending"
        elif job.get("state") == "available" and job.get("phase") == "completed":
            complete = (
                payload.get("serverAcceptance") in {"accepted", "reconciled_noop"}
                and payload.get("profileWriteback") == "complete"
            )
            if action == "library:download":
                complete = complete and (
                    payload.get("packId") == row["content_id"]
                    and payload.get("membership") == "added"
                    and payload.get("contentReadiness") == "available"
                )
            if complete:
                state = "success"
        elif job.get("state") in {"failed", "cancelled", "expired", "unsupported"}:
            state = "failed"
    except (NativeAPIError, ValueError, TypeError):
        state = "needs_review"
    adapter.state.finish_sticker(key, state, job_id)
    return result(action, state, job_id=job_id)


def operation_text(outcome):
    label = "同步" if outcome["action"] == "library:sync" else "下载"
    text = {
        "success": f"{label}完成。可在聊天中请求查找并发送本地可用的 GIF 表情。",
        "pending": f"{label}任务仍在处理中，尚未完成。",
        "failed": f"{label}任务失败，请检查 Native 任务与连接状态。",
        "needs_review": f"{label}结果待核对，请勿重复提交或更换任务重试。",
    }[outcome["status"]]
    job_id = outcome.get("job_id")
    if job_id:
        text += f"\n任务：{job_id}\n查询：/表情包 状态 {job_id}"
    return text


async def run_sticker_command(event, action="帮助", target=""):
    adapter, task = None, asyncio.current_task()
    try:
        if (
            not is_native_event(event)
            or event.is_admin() is not True
            or event.is_private_chat() is not True
        ):
            return "此指令仅允许管理员通过 WeChat Native 私聊操作。"
        if not event.adapter.native_config.enable_stickers:
            return "请先在机器人配置中开启 enable_stickers。"
        ctx = context(event, stickers=True)
        adapter = ctx.adapter
        adapter._send_tasks.add(task)
        if action in {"帮助", "help", ""}:
            return HELP
        if not isinstance(target, str):
            return HELP
        if action == "列表":
            if target and (not target.isascii() or not target.isdecimal() or len(target) > 4):
                return "页码应为正整数。"
            number = int(target or "1")
            packs = await added_packs(adapter)
            context(event, stickers=True)
            if not packs:
                return "当前没有已添加的表情包，请先在手机微信添加，再执行 /表情包 同步。"
            pages = (len(packs) + 9) // 10
            if not 1 <= number <= pages:
                return f"页码应在 1–{pages} 之间。"
            rows = []
            readiness = {"available": "已下载", "partial": "部分就绪", "unavailable": "未下载"}
            for pack in packs[(number - 1) * 10 : number * 10]:
                name = clean_sticker_caption(pack.get("name"))[:50] or "未命名"
                state = readiness.get(pack.get("contentReadiness"), "未下载或未就绪")
                rows.append(f"{name} · {state}\nID：{pack['id']}")
            return "\n\n".join(rows) + f"\n第 {number}/{pages} 页。下载：/表情包 下载 PACK_ID"
        if action == "状态":
            if not valid_id(target):
                return "用法：/表情包 状态 JOB_ID"
            row = adapter.state.sticker_operation_by_job(ctx.account, target)
            if row is None or not row["action"].startswith("library:"):
                return "找不到当前账号通过本插件提交的表情库任务。"
            return operation_text(await inspect_library_operation(adapter, row))
        if action not in {"下载", "同步"}:
            return HELP
        if action == "同步" and target:
            return "用法：/表情包 同步"
        if action == "下载":
            if not valid_id(target):
                return "用法：/表情包 下载 PACK_ID"
            packs = await added_packs(adapter)
            if not any(p["id"] == target for p in packs):
                return "该包尚未添加到当前微信账号，请先在手机添加，再同步目录。"
        context(event, stickers=True)
        downloading = action == "下载"
        source = {"packId": target} if downloading else {"scope": "all"}
        row, won = adapter.stickers.claim(
            event.message_obj.raw_message,
            "library:download" if downloading else "library:sync",
            target if downloading else "all",
            source,
        )
        if not won:
            return operation_text(await inspect_library_operation(adapter, row))
        try:
            if downloading:
                job = await NativeClient.download_sticker_pack(
                    adapter.client, target, row["idempotency_key"]
                )
            else:
                job = await NativeClient.sync_stickers(adapter.client, row["idempotency_key"])
            outcome = await inspect_library_operation(adapter, row, job)
        except BaseException as error:
            state = (
                "failed"
                if isinstance(error, NativeAPIError)
                and 400 <= error.status < 500
                and error.status not in {408, 409, 429}
                else "needs_review"
            )
            adapter.state.finish_sticker(row["idempotency_key"], state)
            if isinstance(error, asyncio.CancelledError):
                raise
            outcome = result(row["action"], state, job_id=row["job_id"])
        return operation_text(outcome)
    except Exception as error:
        if is_tool_error(error):
            return "当前暂时无法管理表情库，请检查机器人开关、私聊权限和连接状态。"
        logger.warning("WeChat Native 表情管理指令失败：error_type=%s", type(error).__name__)
        return "表情库操作暂时无法完成，请检查 Native 连接和任务状态。"
    finally:
        if adapter is not None:
            adapter._send_tasks.discard(task)

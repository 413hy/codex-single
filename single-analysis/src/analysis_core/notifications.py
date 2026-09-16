"""One bounded, human-readable Telegram summary per analysis cycle."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def cycle_notice(store, cid):
    cycle = store.rows("SELECT * FROM cycles WHERE cycle_id=?", (cid,))[0]
    start, end = cycle["started_at"], cycle["completed_at"] or cycle["started_at"]
    stamp = datetime.fromtimestamp(start, TZ).strftime("%m月%d日 %H:%M")
    finish = datetime.fromtimestamp(end, TZ).strftime("%H:%M")
    elapsed = max(1, round((end - start) / 60))
    state = {"SUCCESS": "完成", "PARTIAL_ERROR": "部分失败", "INTERRUPTED": "已中断"}.get(
        cycle["status"], "处理中"
    )
    rows = store.rows("SELECT * FROM signals WHERE cycle_id=? ORDER BY created_at", (cid,))
    lines = [f"🧠 小长线分析 · {state}", f"🕒 北京时间 {stamp}—{finish} · 约{elapsed}分钟", ""]
    for row in rows[:20]:
        lines.append(signal_heading(row))
    if not rows:
        lines += ["本轮未产生方向结果，请查看「分析异常」。", ""]
    if len(rows) > 20:
        lines.append(f"另有{len(rows) - 20}个结果，请查看「最近分析」。")
    if state != "完成":
        lines.append("⚠️ 部分分析未完成，详情见「分析异常」。")
    lines += ["", "点击消息下方币种查看详情 · 仅分析汇总"]
    return "\n".join(lines)


def signal_heading(row):
    evidence = json.loads(row["evidence"])
    primary = evidence.get("direction_required") is True
    hedge = bool(evidence.get("priority_review") or evidence.get("priority_hedge"))
    label = "⭐ 首选" if primary else "🔒 双仓" if hedge else "• 入选"
    if primary and hedge:
        label += " · 双仓"
    direction = {"LONG": "做多", "SHORT": "做空", "SKIP": "观望"}.get(row["side"], "未发布")
    if row["status"] != "PUBLISHED":
        direction = {
            "SKIP_INSUFFICIENT_WEEK_HISTORY": "历史行情不足",
            "ERROR_MODEL_SERVICE": "模型服务异常",
            "ERROR": "分析失败",
        }.get(row["status"], "未完成")
    return f"{label}  {row['symbol']} · {direction}"


def cycle_keyboard(store, cid):
    rows = store.rows("SELECT * FROM signals WHERE cycle_id=? ORDER BY created_at", (cid,))
    buttons = [
        {"text": f"📊 {row['symbol']}", "callback_data": "detail:" + row["signal_id"]}
        for row in rows
    ]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def signal_detail(store, sid):
    rows = store.rows(
        "SELECT signals.*,cycles.started_at FROM signals JOIN cycles USING(cycle_id) "
        "WHERE signal_id=?", (sid,),
    )
    if not rows:
        return "该分析记录不存在，请查看最近分析。", None
    row = rows[0]
    stamp = datetime.fromtimestamp(row["started_at"], TZ).strftime("%Y年%m月%d日 %H:%M")
    payload = json.loads(row["analysis"] or "{}")
    reason = payload.get("reason") or "本轮未产生完整分析结果，请查看分析异常，等待下一轮。"
    text = f"📊 币种分析详情\n北京时间 {stamp}\n{signal_heading(row)}\n\n{reason}"
    text += "\n\n本轮历史分析 · 不重新发布信号"
    return text, cycle_keyboard(store, row["cycle_id"])

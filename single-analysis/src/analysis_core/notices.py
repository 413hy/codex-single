def incident_text(scope, kind, error, incident_id, symbol=None):
    if kind == "communication" or scope == "ANALYSIS_BOT":
        return (
            f"⚠️ 分析Bot通信异常 · {scope}\n{error}\n"
            "管理消息接收或通知发送受影响；此异常不表示启动了分析，暂停状态不变。\n"
            f"编号：{incident_id[:8]}"
        )
    return f"⚠️ 分析异常 · {symbol or scope}\n{error}\n本轮受影响的分析不发布交易信号；下轮重新取行情。\n编号：{incident_id[:8]}"

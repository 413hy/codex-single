def incident_text(scope, kind, error, incident_id, symbol=None):
    return f"⚠️ 分析异常 · {symbol or scope}\n{error}\n本轮受影响的分析不发布交易信号；下轮重新取行情。\n编号：{incident_id[:8]}"

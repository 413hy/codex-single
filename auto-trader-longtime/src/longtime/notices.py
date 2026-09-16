"""Plain-language incident summaries; full diagnostics stay in the ledger."""


def incident_text(scope, kind, error, incident_id, symbol=None):
    category = scope.split(":", 1)[0]
    if category == "SETTLEMENT":
        title = "平仓收益待核对"
        cause = "仓位已结束，成交和收益记录还没有核对齐。"
        impact = "该币暂不重复开仓，其他币照常处理。"
        action = "系统会继续核对；也可点击下方按钮立即检查。"
    elif category in ("SL", "TP"):
        label = "止损" if category == "SL" else "止盈"
        title = label + "单未确认"
        cause = f"未能确认原{label}单有效，自动重试次数已用完。"
        impact = f"该仓位可能缺少{label}保护，需要处理。"
        action = "点击下方按钮，核对后重试一次；已越过原目标则市价退出。"
        if "原触发价已被行情越过" in error:
            cause = "提交时行情已越过原止损价，交易所拒绝该触发方向。"
            action = "点击重试核对原目标；行情已越过目标则仅减仓市价退出。"
    elif category == "MODEL_SERVICE":
        quota = any(word in error for word in ("额度", "credits", "quota"))
        title = "分析额度暂不可用" if quota else "分析服务暂时繁忙"
        cause = "模型上游返回容量不足（capacity），未能完成本轮分析。"
        impact = "本轮剩余分析已跳过，已有止盈止损继续有效。"
        action = "检查模型额度后重试。" if quota else "下一轮自动重新分析，也可点击下方按钮重试。"
        if quota:
            cause = "模型上游返回额度不足，当前无法继续分析。"
        elif "认证" in error or "401" in error:
            title, cause = "分析服务认证失败", "模型上游拒绝当前登录或凭据（401）。"
            action = "检查模型服务登录或凭据，恢复后再重试。"
        elif "超时" in error:
            title, cause = "分析服务响应超时", "本次分析超过等待时限，调用已终止。"
        elif "429" in error:
            title, cause = "分析请求受限", "模型上游限制了请求频率（429）。"
        elif "503" in error:
            title, cause = "分析服务暂不可用", "模型上游返回服务不可用（503）。"
        elif "连接" in error:
            title, cause = "分析服务连接失败", "模型连接中断或无法建立，未取得有效分析。"
        elif "capacity" not in error and "繁忙" not in error:
            title, cause = "分析服务未完成", "模型服务未返回可用结果，详细原因已保留。"
    elif category == "ENTRY":
        title = "开仓结果待核对"
        cause = "交易所尚未给出可确认的开仓结果。"
        impact = "该币已锁定，系统不会重复开仓。"
        action = "点击下方按钮核对原订单，不会直接再下一单。"
        if kind == "candidate":
            title, cause = "开仓被交易所拒绝", "交易所明确拒绝了本次开仓请求。"
            impact = "本次请求未开仓；已有仓位及保护单不受此拒单影响。"
            action = "排除拒单原因后再重试；按钮会重新取行情并筛选，不重发旧方向。"
            if "110126" in error:
                cause = "该合约要求的交易协议尚未签署（110126）。"
                action = "需在Bybit账户确认该合约协议；处理后再重试，程序不能代签。"
            elif "Qty invalid" in error:
                cause = "交易所拒绝了提交的数量格式或精度（Qty invalid）。"
                action = "修复数量格式并核对合约精度后再重试；不会重发旧订单。"
    elif category == "EXTERNAL":
        title = "发现非本系统仓位"
        cause = "无法确认这笔仓位的原始止盈止损。"
        impact = "该币暂停新增交易，系统不会接管或改价。"
        action = "核查原交易系统；下方按钮只重新检查状态。"
    elif category == "TELEGRAM_DELIVERY":
        title = "有通知发送失败"
        cause = "消息未能送达，失败记录已保留。"
        impact = "不代表交易失败，请查看当前持仓。"
        action = "点击下方按钮重新发送失败通知。"
    elif category == "TELEGRAM_POLL":
        title = "Bot接收消息异常"
        cause = "Telegram连接或接收消息处理失败，尚未确认稳定恢复。"
        if any(word in error for word in ("Timeout", "ReadError", "502")):
            cause = "Telegram连接超时、中断或服务暂不可用。"
        impact = "Bot指令可能延迟；此错误不代表交易所仓位或保护单异常。"
        action = "系统会继续接收，连续正常5分钟后解除；也可点击按钮检查状态。"
    elif category in ("SCAN_DATA", "CANDIDATE", "CYCLE", "MODEL"):
        title = "本轮分析未完成"
        cause = (
            "有效行情证据不足6份，无法完成六币复核。"
            if "6份" in error or "不足6" in error
            else "行情采集或模型分析未通过检查。"
        )
        impact = "受影响的候选已跳过，已有止盈止损保留。"
        action = "下一轮会重新取行情；也可点击下方按钮重试。"
    else:
        title = "交易状态检查异常" if kind == "monitor" else "系统操作未完成"
        cause = "未能完成最新状态核对，详细原因已记入日志。"
        impact = "请查看当前持仓及保护单状态。"
        action = "点击下方按钮重新核对一次。"
    heading = "⚠️ " + title + (" · " + symbol if symbol else "")
    return f"{heading}\n原因：{cause}\n影响：{impact}\n处理：{action}\n编号：{incident_id[:8]}"

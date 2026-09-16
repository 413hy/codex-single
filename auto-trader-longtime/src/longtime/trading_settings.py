"""Owner-configured defaults, persisted independently of existing trade intents."""

from dataclasses import dataclass
from decimal import Decimal as D

from longtime.risk import MARGIN, MAX_LEVERAGE, SL_LOSS, TP_TARGETS, number

SETTINGS_KEY = "entry_defaults"


@dataclass(frozen=True)
class EntryDefaults:
    margin: D = MARGIN
    leverage: D = MAX_LEVERAGE
    tp: D = TP_TARGETS[0]
    sl: D = SL_LOSS
    revision: int = 0

    @classmethod
    def parse(cls, value=None):
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - {"cycle_minutes", "cycle_changed_at"} != {
            "margin",
            "leverage",
            "tp",
            "sl",
            "revision",
        }:
            raise ValueError("开仓配置格式无效")
        obj = cls(
            margin=number(value["margin"]),
            leverage=number(value["leverage"]),
            tp=number(value["tp"]),
            sl=number(value["sl"]),
            revision=value["revision"],
        )
        if type(obj.revision) is not int or obj.revision < 0:
            raise ValueError("开仓配置版本无效")
        if min(obj.margin, obj.tp, obj.sl) <= 0:
            raise ValueError("金额必须大于0")
        if not D(1) <= obj.leverage <= D(5):
            raise ValueError("杠杆必须在1至5倍之间")
        return obj

    def document(self):
        return {
            "margin": str(self.margin),
            "leverage": str(self.leverage),
            "tp": str(self.tp),
            "sl": str(self.sl),
            "revision": self.revision,
        }

    @classmethod
    def load(cls, store):
        return cls.parse(store.state(SETTINGS_KEY))

    def description(self):
        return (
            f"每笔保证金：{self.margin:f} U\n"
            f"默认杠杆：{self.leverage:f} 倍（受合约上限及步长限制）\n"
            f"预计开仓总价值：{self.margin * self.leverage:f} U\n"
            f"净止盈目标：{self.tp:f} U\n净止损预算：{self.sl:f} U\n"
            "分析频率由独立分析Bot统一管理。\n"
            "仅影响之后的新仓，已有仓位和已提交意图保持原参数。"
        )

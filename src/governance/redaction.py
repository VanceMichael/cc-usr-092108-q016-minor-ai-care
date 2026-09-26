"""会话信号脱敏。

进入治理后台前，陪伴端先把学生消息变成“信号”：

- 去除手机号、身份证、住址、姓名等直接标识符；
- 学生身份用一次性假名（pseudonym）代替，治理后台不持有真名；
- 原文（raw_text）从不进入后台——本模块是后台边界上唯一见过原文的地方，
  且只返回脱敏后的信号对象，不保留原文。

脱敏是防御性的：规则引擎只依赖结构化特征与少量关键词，
即使脱敏遗漏了某个称谓，也不会影响风险判断。
"""

from __future__ import annotations

import re
from datetime import datetime

from src.governance.models import Signal

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("QQ号", re.compile(r"(?<![\dA-Za-z])[1-9]\d{4,10}(?![\dA-Za-z])")),
    ("地址", re.compile(r"[一-鿿]{2,}(?:省|市|区|县|镇|街道|路|号|栋|单元|室)")),
)

# 常见自报姓名的句式，命中后整体替换为占位符
_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"我叫[一-鿿]{2,4}"),
    re.compile(r"我的名字是[一-鿿]{2,4}"),
    re.compile(r"我是[一-鿿]{2,4}(?=，|。|！|？|\s|$)"),
)

_PLACEHOLDER = "［已隐去］"


def redact_text(text: str) -> str:
    """对单条文本做直接标识符脱敏。"""
    result = text
    for pattern in _NAME_PATTERNS:
        result = pattern.sub(_PLACEHOLDER, result)
    for _, pattern in _PATTERNS:
        result = pattern.sub(_PLACEHOLDER, result)
    return result


def pseudonym_for(student_id: str, school_salt: str) -> str:
    """由学籍号派生一次性假名。

    假名只在治理后台内部使用；学生端展示用 display_name，
    监护人端只收到安全结论，因此假名永不外泄。
    """
    import hashlib

    digest = hashlib.sha256(f"{school_salt}:{student_id}".encode("utf-8")).hexdigest()
    return f"stu-{digest[:12]}"


def make_signal(
    *,
    signal_id: str,
    student_id: str,
    observed_at: datetime,
    raw_text: str,
    features: frozenset[str],
    model_version: str,
) -> Signal:
    """后台边界：原文进，脱敏信号出。原文在此函数返回后即被丢弃。"""
    return Signal(
        signal_id=signal_id,
        student_id=student_id,
        observed_at=observed_at,
        text=redact_text(raw_text),
        features=features,
        model_version=model_version,
    )

"""按角色的可见性矩阵。

学生明确知道“谁能看到哪些信息”；家长只得到必要安全结论；
专业人员可审查接力全貌，但解封真实身份仍需单独授权留痕。
"""

from __future__ import annotations

from dataclasses import dataclass

STUDENT = "student"
GUARDIAN = "guardian"
TEACHER = "teacher"
COUNSELOR = "counselor"
PSYCHOLOGIST = "psychologist"
REVIEWER = "reviewer"      # 审查人员（专业督导/合规）

ROLES = (STUDENT, GUARDIAN, TEACHER, COUNSELOR, PSYCHOLOGIST, REVIEWER)

RESOURCES = (
    "raw_message",          # 会话原文：任何角色都不可见（系统本身不持有）
    "signal_features",      # 脱敏特征值
    "signal_snippets",      # 短语片段（长度受限）
    "trend",                # 去标识化趋势
    "level",                # 当前风险等级
    "rule_basis",           # 判定依据（规则与命中事实）
    "guardian_digest",      # 家长安全结论
    "professional_digest",  # 专业摘要与接力审查
    "identity_real",        # 真实身份映射（需临时解封授权）
    "audit",                # 审计证据
    "retention_certificate",
)

# 显式矩阵：True 表示在最小必要范围内可见
_MATRIX: dict[str, dict[str, bool]] = {
    STUDENT: {
        "raw_message": False, "signal_features": True, "signal_snippets": True,
        "trend": True, "level": True, "rule_basis": True,
        "guardian_digest": True, "professional_digest": False,
        "identity_real": False, "audit": True, "retention_certificate": True,
    },
    GUARDIAN: {
        "raw_message": False, "signal_features": False, "signal_snippets": False,
        "trend": False, "level": True, "rule_basis": False,
        "guardian_digest": True, "professional_digest": False,
        "identity_real": False, "audit": False, "retention_certificate": True,
    },
    TEACHER: {
        "raw_message": False, "signal_features": False, "signal_snippets": False,
        "trend": True, "level": True, "rule_basis": False,
        "guardian_digest": False, "professional_digest": False,
        "identity_real": False, "audit": False, "retention_certificate": False,
    },
    COUNSELOR: {
        "raw_message": False, "signal_features": True, "signal_snippets": True,
        "trend": True, "level": True, "rule_basis": True,
        "guardian_digest": False, "professional_digest": True,
        "identity_real": False,  # 默认化名，需 unmask 授权
        "audit": False, "retention_certificate": False,
    },
    PSYCHOLOGIST: {
        "raw_message": False, "signal_features": True, "signal_snippets": True,
        "trend": True, "level": True, "rule_basis": True,
        "guardian_digest": False, "professional_digest": True,
        "identity_real": False,
        "audit": False, "retention_certificate": False,
    },
    REVIEWER: {
        "raw_message": False, "signal_features": True, "signal_snippets": False,
        "trend": True, "level": True, "rule_basis": True,
        "guardian_digest": False, "professional_digest": True,
        "identity_real": False, "audit": True, "retention_certificate": True,
    },
}


def authorize(role: str, resource: str) -> bool:
    if role not in _MATRIX:
        raise ValueError(f"未知角色 {role}")
    if resource not in RESOURCES:
        raise ValueError(f"未知资源 {resource}")
    return _MATRIX[role][resource]


@dataclass(frozen=True)
class VisibilityLine:
    audience: str
    can_see: tuple[str, ...]
    cannot_see: tuple[str, ...]
    note: str


def student_access_report() -> list[VisibilityLine]:
    """给学生看的“谁可见什么”说明，三类关键受众。"""
    notes = {
        STUDENT: "你本人：可看自己的脱敏信号、等级、依据和收到的通知；看不到任何已被系统拒绝的原始留存。",
        GUARDIAN: "家长：只收到必要安全结论，看不到你的对话、特征值和短语片段。",
        TEACHER: "班主任：只看到等级与去标识趋势，用于日常关心。",
        COUNSELOR: "心理老师：在承接你的案件时，可看到脱敏信号、依据和接力记录；真实身份映射需另行授权留痕。",
        PSYCHOLOGIST: "专职心理教师：紧急/严重情形下承接，可见范围与心理老师相同。",
        REVIEWER: "审查人员：可审查判定依据、接力是否及时、审计与删除证明。",
    }
    lines: list[VisibilityLine] = []
    for role, cell in _MATRIX.items():
        can = tuple(r for r in RESOURCES if cell[r])
        cannot = tuple(r for r in RESOURCES if not cell[r])
        lines.append(VisibilityLine(role, can, cannot, notes[role]))
    return lines

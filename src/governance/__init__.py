"""未成年人AI陪伴治理后台。

模块划分：

- ``redaction``   会话信号脱敏，原文不落盘
- ``models``       共享数据类型与风险等级
- ``audit``        哈希链审计与可核验证书
- ``registry``     学籍、监护授权、服务时段、模型版本、夜间值守登记
- ``rules``        版本化风险规则引擎（自动判断必须说明依据）
- ``escalation``   分级升级、人工接力、人工否决与幂等通知
- ``retention``    去标识化趋势沉淀与到期删除
- ``views``        学生 / 监护人 / 专业人员三视图
- ``backend``      串起全部环节的治理后台门面
"""

from src.governance.backend import GovernanceBackend
from src.governance.models import LEVEL_LABELS, RISK_LEVELS

__all__ = ["GovernanceBackend", "LEVEL_LABELS", "RISK_LEVELS"]

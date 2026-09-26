"""陪伴模型版本登记。

只有登记在册、且在信号产生时点尚未停用的模型版本才允许上报信号；
登记与停用均写审计，保证“当时是哪一个模型、哪一版脱敏抽取器”可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelVersion:
    version: str
    extractor_version: str
    registered_at: str
    retired_at: str | None = None

    def active_at(self, at: str) -> bool:
        if at < self.registered_at:
            return False
        return self.retired_at is None or at < self.retired_at


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ModelVersion] = {}

    def register(self, version: str, extractor_version: str,
                 registered_at: str, retired_at: str | None = None) -> ModelVersion:
        record = ModelVersion(version, extractor_version, registered_at, retired_at)
        self._models[version] = record
        return record

    def retire(self, version: str, retired_at: str) -> ModelVersion:
        old = self._models[version]
        record = ModelVersion(
            old.version, old.extractor_version, old.registered_at, retired_at,
        )
        self._models[version] = record
        return record

    def get(self, version: str) -> ModelVersion:
        return self._models[version]

    def is_allowed(self, version: str, at: str) -> bool:
        record = self._models.get(version)
        return record is not None and record.active_at(at)

    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

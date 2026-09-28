"""管理写操作的审计痕迹（轻量版）：成功后走统一 logger 打一行结构化日志。

不落库、不建表——运维在集中式日志里按 ``audit`` 前缀 grep、或用
``actor_id`` / ``action`` 过滤即得「谁在什么时候对什么做了什么」。请求 ID 由
``RequestIdMiddleware`` 写进 contextvar，统一日志格式已带出，不在这里重复传。

口径：
- 只在业务**成功**（``result="ok"``）或**明确失败**（``result="fail"``）时记；
  审计是旁证，任何情况下都不改变业务流程、不因记日志失败而让请求出错。
- 不记录口令等敏感字段——只记「改了密码」这类动作，绝不把新旧口令写进日志。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("audit")


@dataclass(frozen=True)
class Actor:
    """审计日志里的操作者：管理身份 id + 用户名（够定位，不含敏感信息）。"""

    admin_id: str
    username: str

    @classmethod
    def from_principal(cls, principal) -> Actor:
        """从 ``rbac_service.Principal`` 取操作者（鉴权依赖已解出来的那份）。"""
        return cls(admin_id=principal.admin_id, username=principal.username)


def event(
    actor: Actor | None,
    action: str,
    target: str = "",
    *,
    result: str = "ok",
    **fields,
) -> None:
    """写一行结构化审计日志。

    Args:
        actor: 操作者；``None`` 用于匿名/引导动作（如 onboard 前的引导密钥使用）。
        action: 动作标识，约定 ``域.操作``，如 ``account.delete`` / ``auth.login``。
        target: 作用对象标识（账号 id / 角色 code / 文章 slug 等），可空。
        result: ``ok`` 或 ``fail``。
        fields: 追加的少量上下文（如 ``username`` / ``role``），不进敏感值。
    """
    payload = {
        "actor_id": actor.admin_id if actor else "",
        "actor": actor.username if actor else "",
        "action": action,
        "target": target,
        "result": result,
    }
    payload.update(fields)
    logger.info("audit %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


__all__ = ["Actor", "event"]

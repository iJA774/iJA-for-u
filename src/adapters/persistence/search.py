"""消息与记忆 FTS 的纯查询构造规则。"""

from __future__ import annotations

import re

from domain.models import ComponentType, MessageComponent

_SEARCH_TOKEN_RE = re.compile(
    r"[a-z0-9_]+|[\u3400-\u9fff]+",
    re.IGNORECASE,
)
_FTS_MEMORY_TERM_LIMIT = 64


def normalize_search_text(value: str) -> str:
    """归一化全文检索文本，使 FTS 与无 FTS 降级使用同一语义。"""

    return " ".join(value.casefold().split())


def message_search_text(components: list[MessageComponent]) -> str:
    """只提取用户可见文本；附件内部 ID 等结构化元数据不进入索引。"""

    parts: list[str] = []
    for component in components:
        if component.type == ComponentType.TEXT:
            parts.append(component.text or "")
        elif component.type == ComponentType.MENTION:
            parts.append(f"@{component.target_name or component.target_id}")
        elif component.type == ComponentType.IMAGE_REF:
            parts.append(component.description or f"[图片:{component.filename}]")
        elif component.type == ComponentType.AUDIO_REF:
            parts.append(component.description or f"[语音:{component.filename}]")
        elif component.type == ComponentType.FILE_REF:
            parts.append(component.description or f"[文件:{component.filename}]")
    return normalize_search_text(" ".join(parts))


def fts_phrase(value: str) -> str | None:
    """把至少三个字符的纯文本包装成安全的 FTS5 精确短语。"""

    normalized = normalize_search_text(value)
    if len(normalized) < 3:
        return None
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


def memory_fts_query(value: str) -> str | None:
    """生成宽召回的 trigram OR 查询；短概念要求调用方完整降级。"""

    normalized = normalize_search_text(value)
    # 四字以内的中文概念常靠单字/双字 BM25 命中；trigram 预筛无法无损覆盖。
    if len(normalized) < 5:
        return None
    terms: list[str] = []
    seen: set[str] = set()
    for match in _SEARCH_TOKEN_RE.finditer(normalized):
        token = match.group(0)
        candidates = (
            [token]
            if token.isascii()
            else [token[index : index + 3] for index in range(max(0, len(token) - 2))]
        )
        for candidate in candidates:
            if len(candidate) < 3 or candidate in seen:
                continue
            seen.add(candidate)
            terms.append(candidate)
            if len(terms) == _FTS_MEMORY_TERM_LIMIT:
                break
        if len(terms) == _FTS_MEMORY_TERM_LIMIT:
            break
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)

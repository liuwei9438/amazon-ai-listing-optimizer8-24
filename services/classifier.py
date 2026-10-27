from __future__ import annotations

import json
import re

from services.ai_client import get_openai_base_url

# V2.13.9 分类优化：把产品按「员工名-大分类-产品分类-细小分类」
# 四段层级写入 cat 字段。词表不写死——大分类允许 AI 遇到新品类时
# 自建简短新类（用户要求：灵活，新产品类型自动增加），下面的
# 常见示例只是提示，不是白名单。
#
# 复用页面配置的 AI 服务商（OpenAI / DeepSeek，OpenAI 协议兼容），
# 一块 10 条产品一次调用：控制时延，也比逐条调用省钱。

SYSTEM_PROMPT = """你是亚马逊产品分类助手。把输入的每个产品归入四段层级分类：
员工名-大分类-产品分类-细小分类

规则：
1. 员工名：用输入里给的 owner，原样使用，放在最前面。
2. 大分类：产品大类，3-6 个汉字。常见示例（不限于，遇到新品类就自建简短新类）：
   家电配件、汽摩配件、园林机械、电动工具、缝纫针织、卫浴五金、
   玩具模型、厨房用品、灯具照明、五金紧固件。
3. 产品分类：中类，例如：洗衣机配件、干衣机配件、洗碗机配件、
   链锯配件、遥控车配件、缝纫机压脚、水槽下水器。
4. 细小分类：具体部件，2-8 个汉字，例如：皮带、排水泵、门锁扣、
   定时器、加热管、滚筒轴承、刀片、消音器、压脚、传动轴。
5. old_cat 是旧的人工分类，可能过时或过粗，只作提示，
   不要照抄进新的四段分类。
6. 按 title（英文产品标题）判断产品类型，认真识别部件用途。
7. 只输出 JSON 数组，不加任何别的文字：
   [{"pid": "xxx", "cat": "员工名-大分类-产品分类-细小分类"}]
   每个输入 pid 必须恰好对应一个输出。"""

CHUNK = 10


def _clean_cat(raw: str, owner: str) -> str:
    """规整模型输出：去杂字符、保证员工名前缀、限长（worker cat≤120）。"""
    cat = re.sub(r"[\r\n\t]+", "", str(raw or "")).strip()
    cat = re.sub(r"\s*-\s*", "-", cat)
    cat = cat.strip("-")

    if owner and not cat.startswith(owner + "-"):
        cat = f"{owner}-{cat}"

    return cat[:120]


def _parse_rows(text: str, chunk: list) -> dict:
    """解析模型输出。模型偶尔在 JSON 外包代码栅栏或说明文字，先截取
    首个 [...]，失败再逐行正则兜底。"""
    rows: dict = {}

    m = re.search(r"\[.*\]", text or "", re.S)

    if m:
        try:
            data = json.loads(m.group(0))

            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue

                    pid = str(row.get("pid") or "")
                    cat = str(row.get("cat") or "")

                    if pid and cat:
                        rows[pid] = cat

        except Exception:
            pass

    if not rows:
        for mm in re.finditer(
            r'"pid"\s*:\s*"([^"]+)"[^}]*?"cat"\s*:\s*"([^"]+)"',
            text or "",
        ):
            rows[mm.group(1)] = mm.group(2)

    by_pid = {str(p.get("pid") or ""): str(p.get("owner") or "") for p in chunk}

    return {
        pid: _clean_cat(cat, by_pid.get(pid, ""))
        for pid, cat in rows.items()
        if pid in by_pid and cat
    }


def classify_products(
    api_key: str,
    model: str,
    products: list,
    progress=None,
) -> tuple[dict, int]:
    """products: [{pid, owner, title, old_cat}] → ({pid: cat}, 失败条数)。

    分块调用（一块 10 条）；单块失败只跳过该块并计入失败，
    不让一块的错误毁掉整批。
    """
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=get_openai_base_url() or None,
        timeout=120,
        max_retries=1,
    )

    out: dict = {}
    failed = 0
    total = max(1, len(products))

    for start in range(0, len(products), CHUNK):
        chunk = products[start:start + CHUNK]

        user_prompt = json.dumps(
            [
                {
                    "pid": str(p.get("pid") or ""),
                    "owner": str(p.get("owner") or ""),
                    "title": str(p.get("title") or "")[:200],
                    "old_cat": str(p.get("old_cat") or "")[:60],
                }
                for p in chunk
            ],
            ensure_ascii=False,
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
            )

            text = (resp.choices[0].message.content or "").strip()
            rows = _parse_rows(text, chunk)

            if rows:
                out.update(rows)
                failed += len(chunk) - len(rows)

            else:
                failed += len(chunk)

        except Exception:
            failed += len(chunk)

        if progress:
            try:
                progress(min(start + CHUNK, len(products)) / total)

            except Exception:
                pass

    return out, failed

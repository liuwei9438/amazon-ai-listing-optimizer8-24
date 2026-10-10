# -*- coding: utf-8 -*-
"""Amazon 分类引擎第一阶段（pt 包）。

模块划分：
  store.py         SQLite 存储层（三概念分离 + 店铺/站点隔离 + 任务断点）
  spapi_client.py  SP-API 封装（PTD ×2 + Catalog ×2，官方 2026-10 核对版）
  material.py      读取层：产品记录 + 引擎结构化输出（只读）
  candidates.py    多策略候选检索
  matcher.py       AI 匹配（只准回真实候选）
  verifier.py      V1-V4 独立验证
  tasks.py         任务编排（单商品/批量/重试/恢复/进度）

工程约束：本包绝不 import main —— SP-API 能力（取令牌/发请求）一律由
main 在装配期以回调注入（见 spapi_client 的 configure），防循环导入。
"""

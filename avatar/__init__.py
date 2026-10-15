# -*- coding: utf-8 -*-
"""Avatar —— 让 AI 以人类形式操纵电脑。

产品哲学：除了必要，全是自动。
唯一的打断是：风控验证、高危确认、权限不足。

分层架构（降级链）::

    CDP  →  UIA  →  Vision/OCR  →  Coordinate
    精准    语义     视觉          兜底

阶段一交付：全域分辨率、DPI 适配与底层感知。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

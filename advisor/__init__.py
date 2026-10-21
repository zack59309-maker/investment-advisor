# -*- coding: utf-8 -*-
"""个人投资决策工作流（advisor）。

用法:
    python -m advisor.cli advise --tier conservative --capital 100000
    python -m advisor.cli advise --tier balanced --capital 100000 --with-backtest
    python -m advisor.cli backtest --tier aggressive
    python -m advisor.cli data-check
"""

__version__ = "1.0.0"

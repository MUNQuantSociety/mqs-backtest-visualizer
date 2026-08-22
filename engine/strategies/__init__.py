"""Portfolio strategies and the API they are written against.

Each ``portfolio_*`` package keeps its ``config.json`` next to its
``strategy.py``: BasePortfolio locates the config by looking beside the file
the class was defined in, so a strategy folder must stay whole.
"""

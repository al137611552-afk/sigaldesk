"""形态层。导入本包即完成 A/B 档函数的注册（C 档插件由用户自行导入）。

这里**故意**有导入副作用：函数表是靠 ``@register`` 装饰器填的，
若不在包初始化时导入 primitives，规则编译就会报"未注册的函数 breakout"。
"""

from . import functions, primitives  # noqa: F401  仅为触发注册

__all__ = ["functions", "primitives"]

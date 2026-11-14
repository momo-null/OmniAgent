"""宿主事件面：协议（protocol）+ 注册表（registry）+ 发现（discover，阶段 3）。

``gateway/events/`` 是「通道 / 事件源」这一层的唯一定义处；具体平台实现全部在
``plugins/<name>/``，本包不 import 任何插件（discover 只读 manifest 字符串，
build 走 importlib 懒加载）。
"""

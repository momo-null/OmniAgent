"""OmniAgent 测试包。

显式声明为常规包（含 __init__.py），避免被 site-packages 下的同名 `tests`
第三方包遮蔽——PEP 420 下 namespace package 会被任意路径上的常规包压过，
导致 `from tests.test_m3b import ...` 报 ModuleNotFoundError。
"""

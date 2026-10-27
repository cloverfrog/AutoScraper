# 包的公共入口：对外暴露声明式抓取协议 ScraperSpec。
# 执行器与 Agent 分别按需导入，读取协议本身不需要安装 Agent 依赖。
"""Public boundary for future rule-generating agents."""

from .scraping.spec import ScraperSpec

__all__ = ["ScraperSpec"]

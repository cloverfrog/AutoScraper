# 抓取领域共享异常：统一描述提取、导航、分页和内容等待的失败。
# 本模块只定义异常及诊断字段，不依赖执行器、浏览器或工作流。
"""Shared scraping errors without execution dependencies."""


# 抓取致命错误的基类；工作流捕获后转换为结构化验证反馈。
class ScrapeError(RuntimeError):
    """Fatal navigation, selector, pagination or extraction error."""


# 点击或滚动动作本身失败，与动作成功但列表未更新的内容超时分开反馈。
class PaginationActionError(ScrapeError):
    code = "pagination_action_failure"


# 单条记录的提取失败；由 on_item_error 决定跳过该条还是终止抓取。
class ItemError(ScrapeError):
    """An individual item could not be extracted."""


# 导航失败，保留目标 URL；网络或 HTTP 错误不能直接证明 CSS 规则有误。
class NavigationError(ItemError):
    """A navigation failed, without evidence that extraction selectors are wrong."""

    code = "navigation_failure"

    # 保存失败 URL 并附加可读诊断，供异常转换和最终报告使用。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(f"Navigation failed at {url}: {message}")


# 导航超过时限，与内容未出现的超时分开标记。
class NavigationTimeout(NavigationError):
    code = "navigation_timeout"


# 页面或详情字段在等待期限内未出现；工作流不会据此要求重写选择器。
class ContentTimeout(ItemError):
    """Missing content within the deadline is not evidence of invalid CSS."""

    code = "content_timeout"

    # 保留内容等待失败的位置，避免只剩一段无法定位的异常文本。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(message)

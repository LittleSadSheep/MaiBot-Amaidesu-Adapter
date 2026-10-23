"""工具层共用的错误类型。"""


class ToolCallError(Exception):
    """工具调用失败，消息文本会原样返回给调用方的模型。"""

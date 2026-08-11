"""统一的进度上报:终端始终打印,网页端额外通过回调把同一条消息推进一个队列,
用来驱动实时更新的状态面板。CLI 不传 on_event 时行为等价于普通 print。
"""


def emit(message: str, on_event=None) -> None:
    print(message)
    if on_event:
        on_event(message)

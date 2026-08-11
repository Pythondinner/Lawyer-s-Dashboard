"""逐页判断:这一页是原生文字(可以直接 get_text),
还是拍照/扫描页(0字符,需要走视觉识别路径)。

用 PyMuPDF(fitz)而不是 pypdf——在一份169MB的大卷宗上验证过,pypdf 抠图会抠错对象
(抠出跟目标页完全无关的图片内容),PyMuPDF 渲染出来的页面内容跟人眼看到的一致,更可靠。"""

NATIVE_TEXT_MIN_CHARS = 30  # 低于这个字符数,不当作"有原生文字"处理,交给视觉路径兜底


def page_format(page) -> str:
    text = (page.get_text() or "").strip()
    if len(text) >= NATIVE_TEXT_MIN_CHARS:
        return "native"
    return "vision"

#!/usr/bin/env python3
"""本地预览：首页展示 latest.html —— 与实际 PushPlus 推送的完整页面保持一致（仅开发用）。

- `/`、`/index.html` 或 `/sample` → 最新一份日报（latest.html）；
- `/legacy-sample` → 旧的 guizang 排版示例（日报排版示例.html，示例数据）；
- 其余路径 → output/ 目录静态文件（如 daily_report_*.html）。
注意：超长日报在微信会保持原版式分条发送，预览页展示完整文件。
"""
import http.server
import os
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(ROOT, "latest.html")
SAMPLE = os.path.join(ROOT, "日报排版示例.html")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _simple_page(self, code, message):
        body = (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            "<body style='margin:0;background:#F1F1F1;color:#0A0A0B;"
            "font-family:'Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif;'>"
            "<div style='max-width:640px;margin:80px auto;padding:0 20px;text-align:center;'>"
            f"<div style='font-size:15px;font-weight:700;'>{message}</div>"
            "<div style='font-size:12px;color:#333;margin-top:12px;line-height:1.8;'>"
            "请先运行 <code>python3 output/push.py --no-push</code> 生成日报，再刷新本页面。</div>"
            "</div></body></html>"
        )
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/sample"):
            if os.path.isfile(LATEST):
                # 最新日报：微信超长时按原样分条，不再降级成另一套纯文本版式。
                self.path = "/latest.html"
            else:
                self._simple_page(404, "暂无 latest.html")
                return
        elif path == "/legacy-sample":
            self.path = "/日报排版示例.html"
        return super().do_GET()

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/sample"):
            if os.path.isfile(LATEST):
                self.path = "/latest.html"
        elif path == "/legacy-sample":
            self.path = "/日报排版示例.html"
        return super().do_HEAD()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    http.server.HTTPServer(("0.0.0.0", 8900), Handler).serve_forever()

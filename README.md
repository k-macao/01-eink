# 01-eink

本仓库用于墨水屏（e-ink）设备相关内容的接入与合并。

## 已接入内容：eMemoLog 设备仓库

从设备仓库 https://github.com/einkinner/ememolog 合并了墨水屏日历校准工具（Web Bluetooth）：

- `index.html` — 设备控制页面（蓝牙连接、同步时间、旋转屏幕、日志）
- `css/main.css` — 页面样式
- `js/main.js` — Web Bluetooth 交互逻辑（EPD 服务 `62750001-d828-918d-fb46-b6c11c675aec`、特征值 `62750002-d828-918d-fb46-b6c11c675aec`）
- `.github/workflows/static.yml` — GitHub Pages 静态部署流程
- `LICENSE` — GNU GPL v3

页面可在支持 Web Bluetooth 的浏览器中运行（桌面 Chrome/Edge、Android Chrome/Edge、iOS Bluefy），用于连接墨水屏日历设备并进行时间同步与屏幕旋转等操作。

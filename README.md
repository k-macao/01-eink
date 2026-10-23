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

## AI 总结推送

页面新增「AI 总结推送」区块，一键把 `001/output/latest.html`（章鱼 AI · 上水日报）「总结」栏目的内容渲染成位图并推送到墨水屏。

| 项 | 说明 |
|---|---|
| 内容来源 | 同源抓取 `001/output/latest.html`，取 `总结` 栏目的 `今日盘点 / 明日关注 / 风险关注 / 本栏结论` |
| 预览 | 点「预览」只渲染，不碰蓝牙；可先确认排版再推送 |
| 屏幕尺寸 | 4.2 寸 400×300（默认）、2.9 寸 296×128、2.13 寸 250×122 |
| 选项 | 旋转 180°、三色（黑白红） |
| 排版降级 | 放不下时先截短最长一条 → 再删末条 → 最后才缩到 8px，保证不越界、不重叠 |

BLE 图像协议（EPD-nRF5 系）：

```
INIT(0x01) → 等待 → WRITE_IMAGE(0x30, 黑白层) → [WRITE_IMAGE(0x30, 红色层)] → REFRESH(0x05)
```

- 报文：`[0x30][flag][data...]`，1bpp、8 像素/字节、MSB first
- 黑白层 `1`=白 `0`=黑；红色层 `0`=红 `1`=非红
- 块大小取设备 notification 上报的 `mtu=NNN` 减 2（默认 240）
- 固件上报 `rle=1` 时使用位标志编码（`bit0` 层、`bit1` 首块），否则用 `0x0F/0x00` + `0xF0` 续块标志

> 若连接后设备未上报 `mtu=`，日志会提示「可能是该固件不支持图像推送」。仅做时间同步的日历固件可能没有 `0x30` 指令。

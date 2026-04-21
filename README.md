<div align="center">

# astrbot_plugin_magnetURI_info

<br />

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) 磁链解析插件 ✨_

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)

![:name](https://count.getloli.com/@hajimihajimihajimi?name=hajimihajimihajimi&theme=moebooru&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

</div>

<br />

一个[Astrbot](https://github.com/AstrBotDevs/AstrBot)插件，它能自动识别聊天中的磁力链接，并调用 [whatslink.info](https://whatslink.info/) 提供的 API 来生成包含资源详情和截图的预览消息。

## ✨ 功能特性

- **自动识别**: 无需任何指令，在聊天中发送磁力链接即可自动触发。
- **信息丰富**: 显示资源的名称、总大小、文件数量和内容类型。
- **截图预览**: 可配置是否显示由 API 提供的资源截图。
- **智能发送**:
  - 在 QQ/OneBot 平台下，可配置使用**合并转发**的形式发送，避免长消息刷屏。
  - 解析过程中会发送一条简短提示，便于确认插件已开始处理。

## 💿 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_magnetURI_info` 并安装。

## 📖 使用方法

在任意聊天中发送包含磁力链接的消息即可。插件会自动处理并回复预览信息。

## 🧩 配置示例

在 AstrBot 配置中可通过 `plugin_settings` 配置本插件（示例仅展示相关片段）：

```json
{
  "plugin_settings": {
    "astrbot_plugin_magnet_info": {
      "timeout": 10000,
      "useForward": true,
      "showScreenshot": true,
      "noiseScreenshot": true,
      "noiseStrength": 8,
      "noiseRatio": 0.002,
      "maxMagnetsPerMessage": 3,
      "maxConcurrentRequests": 4,
      "rateLimitCount": 10,
      "rateLimitWindowSec": 60,
      "screenshotHostAllowlist": "",
      "maxScreenshotsPerMagnet": 3,
      "maxScreenshotBytes": 8388608,
      "maxScreenshotRedirects": 3,
      "maxScreenshotPixels": 20000000,
      "requestRetries": 1,
      "requestRetryBaseDelayMs": 200
    }
  }
}
```

## ⚙️ 配置项

你可以在 AstrBot  的插件配置页面找到本插件的设置项。

| 配置项               | 类型        | 默认值     | 描述                                           |
| ----------------- | --------- | ------- | -------------------------------------------- |
| `timeout`         | `number`  | `10000` | 请求 API 的超时时间（毫秒）。                            |
| `useForward`      | `boolean` | `true`  | 在 QQ/OneBot 平台使用合并转发的形式发送结果。                 |
| `showScreenshot`  | `boolean` | `true`  | 是否在结果中显示资源截图。                                |
| `noiseScreenshot` | `boolean` | `true`  | 是否对截图进行轻微加噪后再发送（需 Pillow）。                   |
| `noiseStrength`   | `number`  | `8`     | 截图加噪强度（1-50）。                                |
| `noiseRatio`      | `number`  | `0.002` | 截图加噪比例（0.002-0.05），表示随机扰动像素的占比（建议 0.002-0.005）。 |
| `maxMagnetsPerMessage` | `number` | `3` | 单条消息最多解析的磁链数量（1-20），用于避免长消息触发大量外部请求。 |
| `maxConcurrentRequests` | `number` | `4` | 外部请求并发上限（1-32，API 请求与截图下载）。 |
| `rateLimitCount` | `number` | `10` | 限流额度（按磁链次数），与 `rateLimitWindowSec` 配合使用。 |
| `rateLimitWindowSec` | `number` | `60` | 限流窗口（秒），窗口内累计磁链次数超过 `rateLimitCount` 将被限流。 |
| `screenshotHostAllowlist` | `string` | `""` | 截图域名白名单（逗号分隔，可选），支持子域名匹配；即使放行域名，解析到私网/保留地址仍会被拦截。 |
| `maxScreenshotsPerMagnet` | `number` | `3` | 每个磁链最多发送的截图数量（0-10）；设置为 `0` 表示不发送截图。 |
| `maxScreenshotBytes` | `number` | `8388608` | 单张截图下载大小上限（字节），有效范围约 64KB-50MB。 |
| `maxScreenshotRedirects` | `number` | `3` | 截图下载最大重定向次数（0-10）；每次跳转都会重新做 URL 安全校验。 |
| `maxScreenshotPixels` | `number` | `20000000` | 截图像素上限（用于加噪安全保护），超过将跳过加噪并直接尝试发送原图。 |
| `requestRetries` | `number` | `1` | 网络请求重试次数（0-2，对超时/连接错误/5xx 进行轻量重试）。 |
| `requestRetryBaseDelayMs` | `number` | `200` | 重试基础退避延迟（毫秒），实际延迟含指数退避与随机抖动。 |

说明：

- 截图发送需要平台支持以 bytes/base64/file 形式发送图片；如果运行环境缺少 `Pillow`（`PIL`），仅影响加噪功能，不影响“发送原图”。
- `maxConcurrentRequests` 会在插件进程启动后首次初始化网络信号量时生效；运行中修改可能不会即时改变并发上限。

## 📜 免责声明

本插件仅作为技术学习和研究目的，所有数据均来源于第三方 API ([whatslink.info](https://whatslink.info/))。

插件作者不存储、不分发、不制作任何资源文件，也不对通过磁力链接获取的内容的合法性、安全性、准确性负责。

请用户在使用本插件时，严格遵守当地法律法规。任何因使用本插件而产生的法律后果，由用户自行承担。

## 📝 许可

见 [LICENSE](./LICENSE)

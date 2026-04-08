<div align="center">

# astrbot_plugin_magnetic_link_analysis

<br />

_✨ [astrbot](https://github.com/AstrBotDevs/AstrBot) 磁链解析插件 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
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
  - 自动引用原始消息进行回复，交互清晰。
  - 发送“解析中”的提示后**自动撤回**，保持聊天界面整洁。

## 💿 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_magnetic_link_analysis` 并安装。

## 📖 使用方法

在任意聊天中发送包含磁力链接的消息即可。插件会自动处理并回复预览信息。

## ⚙️ 配置项

你可以在 AstrBot  的插件配置页面找到本插件的设置项。

| 配置项               | 类型        | 默认值     | 描述                                           |
| ----------------- | --------- | ------- | -------------------------------------------- |
| `timeout`         | `number`  | `10000` | 请求 API 的超时时间（毫秒）。                            |
| `useForward`      | `boolean` | `true`  | 在 QQ/OneBot 平台使用合并转发的形式发送结果。                 |
| `showScreenshot`  | `boolean` | `true`  | 是否在结果中显示资源截图。                                |
| `noiseScreenshot` | `boolean` | `true`  | 是否对截图进行轻微加噪后再发送，以提高图片发送成功率（需 Pillow，且会自动回退）。 |
| `noiseStrength`   | `number`  | `8`     | 截图加噪强度（1-50）。                                |
| `noiseRatio`      | `number`  | `0.002` | 截图加噪比例（0-0.05），表示随机扰动像素的占比。                  |

说明：

- 截图加噪需要运行环境提供 `Pillow`（`PIL`）；若不可用或平台不支持以 bytes/base64/file 形式发送图片，将自动回退为直接发送截图 URL。

## 📜 免责声明

本插件仅作为技术学习和研究目的，所有数据均来源于第三方 API ([whatslink.info](https://whatslink.info/))。

插件作者不存储、不分发、不制作任何资源文件，也不对通过磁力链接获取的内容的合法性、安全性、准确性负责。

请用户在使用本插件时，严格遵守当地法律法规。任何因使用本插件而产生的法律后果，由用户自行承担。

## 📝 许可

见 [LICENSE](./LICENSE)

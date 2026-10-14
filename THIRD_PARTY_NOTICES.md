# 来源与许可

项目代码与文档使用 MIT。上游许可证随各依赖保留；以下是内置快照，不要求用户单独安装对应 Skill。

| 来源 | 固定版本 | 许可/归属 |
|---|---|---|
| [alchaincyf/huashu-art-motion](https://github.com/alchaincyf/huashu-art-motion) | `9ff59339f934e08483ba0a5b1db20f4c2c421b35` | 原 MIT 声明保留 |
| [tomatoparkdao/omniailab-ai-director](https://github.com/tomatoparkdao/omniailab-ai-director) | `772a1477cd436dd8d9fdff60e626a0338ff2f682` | 原 MIT 声明保留 |
| [threerocks/hand-drawn-styles](https://github.com/threerocks/hand-drawn-styles) | `4132988b8596bfa36321c970a4472d4e3bb25d96` | 原 MIT 声明保留 |
| [threerocks/sweety-image-art-direction](https://github.com/threerocks/sweety-image-art-direction) | 入口 SHA-256 `fbbfd33d70f729011b6a6bc232851c116cd4a89ead2adea4462888a6ce26fc1a` | 原 MIT 声明保留 |
| [threerocks/sweety-skills / sweety-image-privacy](https://github.com/threerocks/sweety-skills/tree/main/skills/sweety-image-privacy) | 1.1.0 安装快照，逐文件哈希锁定 | 本作者已有贡献，随本项目按 MIT 提供 |
| [hanzi-writer-data](https://github.com/chanind/hanzi-writer-data)（源自 [Make Me a Hanzi](https://github.com/skishore/makemeahanzi) 与文鼎 AR PL KaitiM GB / UKai 字形） | 2.0.1；仅保留 GB2312 6763 字并重新打包，见 `assets/hanzi/NOTICE.txt` | Arphic Public License，原文见 `assets/hanzi/ARPHICPL.TXT` |

为保持单一安装入口，依赖中的 `SKILL.md` 改名为 `GUIDE.md`，相关文字引用同步改名。花叔仓库的宣传展示图片和 GIF 未放入安装包，其制作资料和实现资源保留。原始文件哈希、内置文件哈希、路径变更和排除清单均见 [vendor-manifest.json](skills/sweety-handdraw-video/dependencies/vendor-manifest.json)。上游方法冲突由本项目的[依赖路由](skills/sweety-handdraw-video/references/dependencies.md)处理。

六款手部为本项目生成素材，按美术指导设计并完成隐私处理；“上官婉儿灵感”描述设计参考，不是游戏官方素材或授权声明。四款随包声音由本机 VoxCPM2 仅凭文字描述生成，未使用任何真人、主播或平台音色作参考；两款逼真声音的原片参考不随包分发，公开试听只含已发布成片的节选。来源见 `assets/voices/catalog.json` 与 `references/voice-sources.md`。本项目的 MIT 许可不替代第三方角色权利、图像或语音服务对生成内容的适用条款。未打包系统商业字体。

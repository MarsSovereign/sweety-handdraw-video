# 开始时的可选制作设置

主题、故事、附件正文或可读取的明确来源至少有一项。全部缺失才向用户索要内容。其余是偏好，不构成停工条件。运行能力缺失或质量失败由模型修复或准确报告，不用虚假的音频、图片、授权或通过记录填补。

## 默认方案与等待

模型在打开窗口前根据内容写 `JOB/options/defaults.json`，不要固定所有作品使用同一编号、手臂或音色。广告和水印默认空白；除非用户已提供文字，不发明品牌、账号或版权声明。字体与字幕形态根据背景和用户已认可的作品决定。

```json
{
  "content":"用户的实际主题或故事摘要",
  "style":{"mode":"catalog","catalog_id":"17"},
  "hand":{"preset":"pen-only"},
  "voice":{"voice":"magnetic-narrator"},
  "advertising":{"text":""},
  "watermark":{"text":""},
  "captions":{"mode":"quiet"},
  "rationale":{
    "style":"墨线绘本适合生活机制里的简洁人物与道具，可以逐笔绘制",
    "hand":"仅画笔减少对小幅机制图的遮挡",
    "voice":"磁性解说男声适合这段平实讲解",
    "captions":"浅色画面适合无底板深灰短句"
  }
}
```

示例不是固定默认。画风默认优先从逐笔绘制的 1、4、5、17、19、20 中按内容选一个编号，窗口里会预先显示这个选择；只有内容确实需要无轮廓质感时才默认揭示画风，并写 `reveal_reason`（见[画风文件](style-and-drawing.md)）。画风可选编号 `catalog`、`catalog_auto`、`inferred`、`custom`；完整约定见[画风文件](style-and-drawing.md)。字幕可选 `quiet` 或 `plate`。

用户已经明确的选择放 `options/explicit.json`，键与上述六项一致；这些项跳过等待。用户说“自动选择全部”时把模型拟定的方案记录为委托自动执行（画风的 `confirmation` 为 `user_delegated`），不再强行展示六轮选项。修订已完成影片时沿用已认可选择，除非本次要求改变。

```bash
python3 scripts/production_options.py JOB --defaults JOB/options/defaults.json --explicit JOB/options/explicit.json
```

没有已选项时省略 `--explicit`。打开输出的 `PRODUCTION_OPTIONS_URL`。窗口依次展示画风、手部、声音、广告、水印、字幕；每项最多60秒。后端独立计时，即使页面未刷新也会写入默认方案并进入下一项。最多六分钟，不反复发新问题重新计时。用户选择立刻进入下一项；输入未提交不会当成确认。

用不超过60秒的工具等待间隔继续其它独立工作，读取 `options/selection.json`。`complete:true` 后停止该本地服务并继续制作，不把这一轮对话结束在“等你选择”。若当前环境无法打开 HTML，用可用的异步问答工具逐项询问；每项满60秒未答后记录默认并继续，不能伪造用户确认。广告、水印空白即不加。

## 应用到实际制作

所有决策保留 `value/reason`，超时原因是 `timeout_default`。不要把它写成用户手动确认。画风选择同时保存到 `style/selection.json`；模型接着写实际 `style/resolved.json` 和完整理由。

创建 `job.json` 后用 `production_options.apply_choices(JOB)` 合并手部、声音、广告、水印和字幕。命令行调用：

```bash
python3 scripts/production_options.py JOB --apply
```

该函数保留内容与音频路径，更新 `hand.preset/pen_text`、`voice_studio.voice`、`voxcpm.voice`、`watermark.text` 和 `captions.background/color`。用户在计时结束后、生成开始前补充了偏好，先更新对应选择文件再应用；明确修改优先于超时值。脚本保存配置不是画风解析或音频已经生成的证据。

## 广告、水印与声音

- 广告目前只在笔杆，最多16个 Unicode 字符；原文保存，不自动截短。超过上限的可选输入显示错误，同时保留超时机制，默认不加。16字是格式上限，长句在细笔杆上未必可读；优先使用宽笔杆或仅画笔素材，在正常尺寸检查。
- 水印使用用户文本，允许换行，最多180字符。默认为低对比深灰、24%不透明度、缓慢小范围浮动；在画内，避开底部字幕。选择器留空不加。实际参数可由模型根据画面调整，不以暗水印遮挡主要内容。
- 六款手加 `pen-only`。只有手臂素材要求前臂底边留在画外，完整画笔没有这项前臂检查。
- 声音显示声音目录的六项（逼真男声、逼真女声、磁性解说男声、儒雅青年、温柔知性女声、悬疑故事解说）以及用户已导入的自定义声音。两款逼真声音播放成片节选，只用于试听，生产使用本机原始参考，缺少参考时可试听但不能选择生成；四款合成声音随包可直接生成。自定义声音须声明 `consent`，参考须通过门禁，见[声音与依赖](local-runtime.md)。

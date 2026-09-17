# DESIGN — 米游社签到托盘（Convention Mode）

工具型界面，不追求品牌视觉，以可发现性和低占用为准。

## Token

```
--ink      #1F2937   主文字
--muted    #6B7280   次要文字
--panel    #F3F4F6   设置窗背景
--card     #FFFFFF   表单底
--line     #E5E7EB   分割线
--accent   #2563EB   主操作（保存/立即签到）
--ok       #16A34A   成功/已启用
--warn     #D97706   警告/进行中
--err      #DC2626   失败
```

## Icon

风格家族对齐「抢码工具」最终 logo，语义改为签到。

```
tile    #6EE2FF   浅青圆角砖底
gloss   #A0EFFF   右上柔和高光
cell    #19A3FF   日签蓝格
check   #F9CC14   金色对勾（签名元素）
```

- 生成：`python make_icon.py` → `assets/icon.png` + 多尺寸 `assets/icon.ico`
- 可选覆盖：`assets/custom_icon.png`（必须是正方形应用图标，不会使用预览拼图）
- 引用点：托盘 `ensure_icon()`、打包 `MihoyoBBSTray.spec` / `build.ps1`、设置窗 `iconphoto`

## Type

- UI: `Segoe UI, Microsoft YaHei, system-ui, sans-serif`
- 等宽日志: `Consolas, Cascadia Mono, monospace` 12px
- 标题 16/600，正文 13/400，标签 12/500

## Layout

```
托盘菜单
  立即签到
  打开设置
  开机自启 ✓
  查看日志
  退出

设置窗 (420×520)
  [状态条] 运行状态 · 下次执行
  功能开关  BBS / 星铁 / ZZZ / 原神...
  定时      时间点列表 + 随机延迟
  账号提示  指向 MihoyoBBSTools config.yaml
  启动      开机自启 · 启动后立即签到
  [保存] [立即签到] [关闭]
```

## 低占用策略

- 托盘常驻，无主窗口
- 设置窗按需创建/销毁
- 调度线程 sleep，不做轮询 UI
- 签到用子进程 `python main.py`，跑完即退，不常驻 httpx
- 单实例互斥

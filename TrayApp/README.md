# MihoyoBBSTray

米游社 / 米哈游游戏辅助签到的 **Windows 托盘工具**。低占用、可定时、可开机自启。

> 个人学习与自用。请遵守米哈游用户协议与当地法律法规。**不会自动绕过图形验证**。

## 功能

- 托盘常驻，双击打开设置
- 社区打卡 / 米游币任务（看帖、点赞、分享可选）
- 游戏签到：原神、星穹铁道、绝区零、崩坏2/3、未定等
- 社区板块勾选
- 定时签到（滚轮调整时间 + 随机延迟）
- 开机自启动
- 短信验证码获取 Stoken（触发 `-3101` 时弹出验证窗口，由用户完成）
- 自动获取/生成 `device_id` / `device_fp`（仅写日志，不在界面展示）

## 架构

```
MihoyoBBSTray (托盘 UI / 调度)
    └── 调用 ../MihoyoBBSTools/main.py （短生命周期子进程）
```

账号 Cookie / Stoken 写入 `MihoyoBBSTools/config/config.yaml`。

## 快速开始

```powershell
# 开发运行（需 Python 3.11+）
pip install -r requirements.txt
python main.py

# 或使用打包产物
.\dist\MihoyoBBSTray.exe
```

1. 复制 `MihoyoBBSTools/config/config.yaml.example` 为 `config.yaml`
2. 启动托盘 → 设置 →「账号 / Stoken」短信登录
3. 勾选功能与板块 → 保存 → 立即签到 / 等待定时

## 打包

```powershell
# 先刷新图标（托盘 / exe / 设置窗共用）
python make_icon.py
.\build.ps1
```

图标默认由 `make_icon.py` 程序化生成（青砖 + 日签格 + 金勾）。若要换成自备图，放置正方形 `assets/custom_icon.png` 后重新执行上述命令。

## 开源前检查

见 [OSS_PREP.md](OSS_PREP.md)。

## 依赖项目

- [Womsxd/MihoyoBBSTools](https://github.com/Womsxd/MihoyoBBSTools) — 签到引擎（MIT）
- 协议与错误码参考公开逆向笔记 / UIGF API 合集

## License

MIT，见 [LICENSE](LICENSE)。上游 MihoyoBBSTools 同为 MIT，请保留其版权声明。

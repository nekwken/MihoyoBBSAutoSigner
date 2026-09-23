# MihoyoBBSTray

米游社 / 米哈游游戏辅助签到的 **Windows 托盘工具**。低占用、可定时、可开机自启。

> 个人学习与自用。请遵守米哈游用户协议与当地法律法规。

## 功能

- 托盘常驻，双击打开设置；可配置关闭时最小化到托盘
- 社区打卡（米游币现仅由打卡发放；看帖/点赞/分享奖励已下线，故点赞、分享功能已移除）
- 游戏签到：原神、星穹铁道、绝区零、崩坏2/3、未定事件簿
- 云游戏签到：云原神、云星穹铁道、云绝区零；凭证在签到时自动获取与刷新
- 定时签到（滚轮调整时间 + 随机延迟）；当天没签成功会自动补签，当天已签成功不重复执行，失败会自动重试
- 开机自启动；静默启动（勾选后启动不弹窗，再双击一次程序会弹出窗口）
- 短信验证码登录（触发 `-3101` 时弹出验证窗口，由用户完成；验证窗口居中于设置窗口）
- 账号区显示 UID 与米游社昵称；提示框居中于设置窗口
- 首次运行自动生成引擎配置，并自动补齐缺失字段
- 自动获取/生成 `device_id` / `device_fp`（仅写日志，不在界面展示）

## 架构

```
MihoyoBBSTray (托盘 UI / 调度)
    └── 调用 ../MihoyoBBSTools/main.py （短生命周期子进程）
```

账号登录凭证与云游戏凭证写入 `MihoyoBBSTools/config/config.yaml`（缺失时自动从 `config.yaml.example` 生成）。
内置运行环境的发布包中，启动引擎时显式注入引擎目录到 `sys.path`（嵌入式 Python 的隔离模式不会自动包含脚本目录）。

## 快速开始

```powershell
# 开发运行（需 Python 3.11+）
pip install -r requirements.txt
python main.py

# 或使用打包产物
.\dist\MihoyoBBSAutoSigner.exe
```

1. 启动托盘 → 设置 →「账号」页短信登录（云端凭证随后自动获取）
2. 「功能」页勾选游戏签到 / 社区打卡 / 云游戏签到 → 保存 → 立即签到或等待定时

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
- 错误码与接口行为参考社区公开整理，详见引擎仓库说明

## License

MIT，见 [LICENSE](LICENSE)。上游 MihoyoBBSTools 同为 MIT，请保留其版权声明。

# 米游社自动签到器 · MihoyoBBSAutoSigner

Windows 托盘工具：米游社 / 米哈游游戏辅助自动签到。支持功能开关、定时签到、开机自启、短信登录获取 Stoken。

> **个人学习与自用。请遵守米哈游用户协议与当地法律。不会自动绕过图形验证/风控。**

## 功能

- 托盘常驻；双击打开设置；可配置关闭时最小化到托盘
- 社区打卡 / 米游币（看帖、点赞、分享可选）
- 游戏签到：原神、星穹铁道、绝区零、崩坏2/3、未定等
- 社区板块勾选；滚轮调整每日签到时间
- 短信验证码登录 Stoken（`-3101` 时弹出验证窗口，由用户完成）
- 自动获取/生成 `device_id` / `device_fp`（仅写日志，不在界面展示）
- 签到在后台线程执行，状态栏显示「请稍候…」滚动提示

## 目录结构

```
MihoyoBBSAutoSigner/
├── TrayApp/                 # 托盘 UI（本仓库主体）
├── engine/MihoyoBBSTools/   # 签到引擎（本地补丁版，见下）
├── README.md
├── LICENSE
└── SECURITY.md
```

签到逻辑调用 `engine/MihoyoBBSTools/main.py`（短生命周期子进程）。账号 Cookie / Stoken 写入引擎的 `config/config.yaml`。

## 快速开始

```powershell
# 1. 安装依赖
cd TrayApp
pip install -r requirements.txt
pip install -r ../engine/MihoyoBBSTools/requirements.txt

# 2. 准备引擎配置（请勿提交真实 config.yaml）
copy ..\engine\MihoyoBBSTools\config\config.yaml.example `
     ..\engine\MihoyoBBSTools\config\config.yaml

# 3. 启动
python main.py
```

设置 →「账号 / Stoken」→ 短信登录；勾选功能 → 保存 → 立即签到 / 等待定时。

打包（可选）：

```powershell
cd TrayApp
.\build.ps1
```

## 相对上游的本地补丁（engine/MihoyoBBSTools）

本仓库**不向** [Womsxd/MihoyoBBSTools](https://github.com/Womsxd/MihoyoBBSTools) 提 PR，仅保留本地修改以便自用与分发：

| 文件 | 变更 |
|---|---|
| `setting.py` | 版本对齐 2.114.0；任务列表路径 `apihub/sapi/getUserMissionsState` |
| `mihoyobbs.py` | 真机请求头；紧凑 JSON + DS2；处理 `1008` 已打卡 |
| `account.py` | 优先 `getUserGameRolesByStoken` |
| `main.py` | `RESULT:` 结论日志 + `sys.exit`；修复异常时 `message` 未定义 |

协议要点（与真机抓包一致，公开接口）：

- 社区 `signIn`：`DS2` + x6 盐 + body `{"gids":"N"}` 参与签名
- BBS App 头：`client_type=2`、`x-rpc-verify_key=bll8iq97cem8`、`okhttp/4.9.3`
- 通行证短信登录走 passport API；图形验证由用户完成

## 免责声明

1. 仅限用户**自有账号**
2. **不自动化**极验/风控验证码
3. 不鼓励多开、刷号、商业滥用
4. 接口变更导致失效属预期；本项目不提供对抗服务端风控的指导
5. **非米哈游官方项目**；使用风险自负

## 致谢

- [Womsxd/MihoyoBBSTools](https://github.com/Womsxd/MihoyoBBSTools) — 签到引擎（MIT）
- 公开米游社 / 米哈游通行证协议整理文档作者

## License

MIT，见 [LICENSE](LICENSE)。使用上游引擎时请一并保留其版权声明。

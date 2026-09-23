# 开源前准备清单（OSS Prep）

目标仓库建议：`MihoyoBBSTray`（仅托盘壳）+ 文档说明依赖 `MihoyoBBSTools`。

## 必须完成

- [x] MIT LICENSE（含上游 MihoyoBBSTools 声明）
- [x] README：功能、架构、免责、依赖
- [x] `.gitignore`：排除 `config.yaml`、`tray_config.json`、`tray.log`、HAR、密钥
- [x] `config/config.yaml.example` 空凭证模板
- [ ] 从工作区删除/勿提交：
  - `MihoyoBBSTools/config/config.yaml`（含 stoken）
  - `research/private_captures.json`
  - `extracted/har/*.har`
  - `research/har/*hits*.json`（可能含 cookie）
- [ ] 代码中无硬编码账号 / token / 真实 device 值
- [x] `tray_config.json` 默认 `minimize_to_tray=true`，`device_id`/`device_fp` 为空（运行时生成）
- [x] UI 不展示 device_id/fp 明文（仅日志）
- [x] 图形验证独立进程弹窗 + 浏览器回退
- [x] 关闭窗口默认收起到托盘，可配置直接退出
- [ ] 免责声明出现在 README 与「关于」页

## 建议补充

- [ ] `CONTRIBUTING.md`（可选）
- [ ] Issue 模板：禁止粘贴 Cookie/Stoken
- [ ] CI：`python -m compileall TrayApp` + 可选 ruff
- [ ] Release 说明：仅分发源码或自建 exe；注明非官方
- [ ] 上游致谢：MihoyoBBSTools、协议文档来源

## 发布前自检命令

```powershell
# 应无输出（无密钥）
rg -n "stoken=|v2_[A-Za-z0-9]{20,}|cookie_token" TrayApp --glob '!*.md'
# 设备默认应为空
python -c "from TrayApp.app_config import DEFAULT; print(DEFAULT['device_id'], DEFAULT['device_fp'])"
```

## 合规红线（写入 README）

1. 仅限用户自有账号
2. 不鼓励多开、刷号、商业滥用
3. 接口变更导致失效属预期

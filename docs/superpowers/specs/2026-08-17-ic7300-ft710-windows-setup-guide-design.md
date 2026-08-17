# Windows 配置与运行指南（IC-7300 / FT-710）— 设计

日期：2026-08-17
状态：已批准

## 目标

在操作指南中新增「Windows 配置与运行」章节，分别讲清楚 IC-7300 与 FT-710 在 Windows 下如何配置并使其工作（驱动 → 端口 → env → 电台侧 → 音频 → 频谱 → 验证），并附带聚焦两机差异与 Windows 特有问题的 FAQ。

## 决策（已与用户确认）

1. **目标文档**：两个都写
   - `docs/OPERATION_GUIDE.md`（中文，网站指南源文件）→ 重建 `website/guide.html` + `zh/guide.html`
   - `docs/WINDOWS_INSTALLER_GUIDE.md`（英文，仓库内手册）：修正配置示例变量名 + 补齐 IC-7300 小节
2. **FAQ**：放在新章节内部，不扩充第 5 节
3. **变量名**：统一用 `MRRC_*` 新名；附一句说明旧 `FT710_*` 仍可用
4. **结构**：分电台两完整小节（`### IC-7300` + `### FT-710`），各自走完整流程，加一个简短通用前置小节

## 章节位置与编号

`docs/OPERATION_GUIDE.md`：插入「0.5 电台型号差异」之后，编号 **`0.6 Windows 配置与运行`**。
现有编号 1–5、附 不动。

## 新章节大纲

```
## 0.6 Windows 配置与运行

### 0.6.1 通用步骤（两台适用）
    安装 → 连接 USB → 设备管理器确认 COM → 编辑 mrrc_modern.env → 启动

### 0.6.2 IC-7300 / IC-7300MK2
    驱动（免装 FTDI）→ CI-V COM 口 → env 示例（MRRC_RADIO_MODEL=ic7300,
    MRRC_SERIAL_PORT, MRRC_BAUD_RATE=115200, USB Audio）
    → 电台侧（CI-V USB 波特率 115200、地址 0x94）→ 音频 48kHz 原生
    → 频谱 CI-V 0x27 → 验证清单

### 0.6.3 FT-710
    驱动（CP210x 必装，FTDI 仅真频谱）→ 两个 COM 口（较低号 CAT）
    → env 示例（MRRC_RADIO_MODEL=ft710, MRRC_SERIAL_PORT,
    MRRC_BAUD_RATE=38400, USB Audio, MRRC_FTDI_LIB_DIR）
    → 电台侧 MOD SOURCE=USB → 音频 44.1kHz 重采样 → 频谱 FT4222/S-meter
    → 验证清单

### 0.6.4 常见问题（FAQ）
    选模型 / 换电台 / COM 口 / Serial 红点 / 没声音 / 频谱不动 /
    USB Audio 设备锁定 / 波特率与地址
```

## 事实依据

- IC-7300：`backends/ic7300/config_ic7300.py`（CIV_BAUD_RATE=115200、CIV_ADDR=0x94）、
  `backends/ic7300/backend.py`、`config.py` 的 `_env()` 别名逻辑
- FT-710：`docs/WINDOWS_INSTALLER_GUIDE.md`（变量名改新）、`windows/default.env`
- 通用：`windows/default.env` 注释即配置模板

## 验证

- `build_guide.py` 成功重建两个 HTML
- 生成的 `guide.html` 新章节 id 正确、侧栏 TOC 自动含新条目

# 客户端与运行环境兼容性

本包把技能说明、配置脚本及 Windows Python 运行环境放在同一目录中。运行入口为 `runtime/tx-mailbox.exe`，必须连同其旁边的 `_internal` 等发布文件一起保留。客户端需要能够读取用户明确提供的本地工程，并执行本地 Windows 命令。AI 使用哪个模型不改变本工具的运行方式。

## Codex

默认安装到 `~/.agents/skills/autosar-tx-mailbox`。也可以放到工程的 `.agents/skills/autosar-tx-mailbox`，让该工程内的 Codex 发现技能。安装脚本的 `-Destination` 可指定项目目录。没有修改 Codex 配置文件，也没有注册 MCP。

依据：[OpenAI 官方技能文档](https://learn.chatgpt.com/docs/build-skills)，2026-10-21 核实。该文档说明用户目录为 `$HOME/.agents/skills`，工程目录为从当前工作目录向仓库根目录查找的 `.agents/skills`；原 `https://developers.openai.com/codex/skills/` 当前重定向到此页面。

## OpenCode

默认安装到 `~/.config/opencode/skills/autosar-tx-mailbox`。项目安装可使用 `.opencode/skills/autosar-tx-mailbox`。使用原生 `SKILL.md`，由 OpenCode 的技能机制加载。宿主的文件、命令和技能权限仍需允许当前工作流执行。

依据：[OpenCode 官方 Agent Skills 文档](https://opencode.ai/docs/skills/)，2026-10-21 核实。文档明确列出了上述全局和工程目录，也支持 `.agents/skills`。

## DeepSeek Harness / DSH Desktop

已针对用户提供的 `D:\DSH\DSH Desktop` 做只读检查，没有向该程序目录或用户配置目录安装文件。

实际依据是该程序 `resources/app.asar` 内的 `node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js`，包版本 `0.2.0-rc.2`。其 `roots(cwd)` 实现列出：

| 作用域 | 搜索位置 |
|---|---|
| 工程 | `<projectRoot>/.dsh/skills` |
| 工程通用 | `<projectRoot>/.agents/skills` |
| 用户 | `<dshHome>/skills` |
| 用户通用 | `<agentsHome>/skills` |
| 自定义 | 配置的 `customSkillDirs` |

`agentsHome` 默认取配置值、`DSH_AGENTS_HOME` 或 `~/.agents`。因此安装器默认使用用户通用目录，可与 Codex 共用。使用 `-Destination` 可以选择已核实的其他发现目录。若客户端通过显式配置改变了 `agentsHome`，请明确提供目标路径。

程序主入口 `out/main/index.js` 使用 `join(app.getPath("userData"), "harness")` 设置桌面版的 `dshHome`。程序安装目录不是技能目录；不能把本技能复制到 `D:\DSH\DSH Desktop` 就宣称已被发现。上述结论限定于本次检查版本，其他版本应检查其实际技能目录。

## ChatGPT 与 ChatGPT Work

普通云聊天可以接收用户上传的 ZIP 和工程文件，用于阅读技能说明和分析数据。上传 ZIP 不等于安装 Windows 执行器，也不会授权云聊天访问本机磁盘。此包内的 Windows 可执行文件不能据此宣称能在普通云聊天直接运行。

有本地执行器的 ChatGPT/Work 可以使用同一套说明和程序，前提是执行器允许本地文件和命令操作。没有统一可假定的安装路径，所以 `-Target ChatGPT` 必须同时指定 `-Destination`；该操作仅复制目录，是否自动发现由宿主配置决定。只具备云聊天时，应由本地人员或本地 AI 执行程序，再把计划、报告和输出文件提供给聊天检查。

## 离线与依赖

发布包运行时不联网安装依赖，不寻找系统 Python，不执行 `pip`，不调用汽车 MCP。源码目录和发布包要区分：只有包含 `runtime/tx-mailbox.exe` 及完整运行文件的 Windows 发布包才满足开箱即用要求。安装器遇到不完整包会停止并给出可读错误。

安装脚本使用现有 PowerShell 7；手动复制完整目录可省去安装脚本。配置程序自身通过随包 Python 运行，不以 PowerShell 7 作为运行依赖。当前发布形态为 Windows 程序；没有声明 macOS 或 Linux 执行支持。

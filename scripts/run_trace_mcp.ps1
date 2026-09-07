<#
文件用途：
- 使用已有 Python/Conda 环境启动项目 Trace MCP。
- 不创建环境、不安装依赖，也不读取任何模型 API Key。

解释器选择顺序：
1. ANDA_PYTHON 环境变量；
2. .codex/python-path.txt 中的本机路径；
3. 当前已激活 Conda 环境；
4. PATH 中可实际导入项目依赖的 python。
#>

$ErrorActionPreference = 'Stop'
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = '1'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$localPythonFile = Join-Path $repositoryRoot '.codex/python-path.txt'
$candidates = [System.Collections.Generic.List[string]]::new()

if ($env:ANDA_PYTHON) {
    $candidates.Add($env:ANDA_PYTHON)
}
if (Test-Path -LiteralPath $localPythonFile -PathType Leaf) {
    $configuredPython = (Get-Content -LiteralPath $localPythonFile -Raw -Encoding utf8).Trim()
    if ($configuredPython) {
        $candidates.Add($configuredPython)
    }
}
if ($env:CONDA_PREFIX) {
    $candidates.Add((Join-Path $env:CONDA_PREFIX 'python.exe'))
}
$candidates.Add('python')

foreach ($candidate in $candidates | Select-Object -Unique) {
    try {
        & $candidate -c 'import anda, fastmcp, can, cantools' 2>$null
        if ($LASTEXITCODE -eq 0) {
            Set-Location -LiteralPath $repositoryRoot
            & $candidate -m anda.mcp.trace.server
            exit $LASTEXITCODE
        }
    }
    catch {
        continue
    }
}

throw '未找到可运行 Trace MCP 的 Python。请激活已安装项目依赖的 Conda 环境，或在 .codex/python-path.txt 中写入其 python.exe 绝对路径。'

<#
文件用途：
- 从已安装插件目录启动 Trace MCP，并复用用户已有的 Python/Conda 环境。
- 不创建环境、不安装依赖，也不读取任何模型 API Key。

解释器选择顺序：
1. ANDA_PYTHON 环境变量；
2. 用户本机配置目录中的 python-path.txt；
3. 当前已激活 Conda 环境；
4. PATH 中可实际导入项目依赖的 python。
#>

$ErrorActionPreference = 'Stop'
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = '1'

$pluginRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $pluginRoot 'src'
$localAppData = [Environment]::GetFolderPath('LocalApplicationData')
$userPythonFile = Join-Path $localAppData 'AutomotiveNetworkDebugAgent/python-path.txt'
$candidates = [System.Collections.Generic.List[string]]::new()

# 插件包内自带 anda 源码。把它放到 PYTHONPATH 最前面，确保安装后的插件从自己的
# 缓存目录运行，而不是继续依赖开发仓库的可编辑安装路径。
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = $sourceRoot + [System.IO.Path]::PathSeparator + $env:PYTHONPATH
}
else {
    $env:PYTHONPATH = $sourceRoot
}

if ($env:ANDA_PYTHON) {
    $candidates.Add($env:ANDA_PYTHON)
}
if (Test-Path -LiteralPath $userPythonFile -PathType Leaf) {
    $configuredPython = (Get-Content -LiteralPath $userPythonFile -Raw -Encoding utf8).Trim()
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
            Set-Location -LiteralPath $pluginRoot
            & $candidate -m anda.mcp.trace.server
            exit $LASTEXITCODE
        }
    }
    catch {
        continue
    }
}

throw '未找到可运行 Trace MCP 的 Python。请激活已安装依赖的 Conda 环境、设置 ANDA_PYTHON，或在本机 LocalAppData/AutomotiveNetworkDebugAgent/python-path.txt 中写入 python.exe 绝对路径。'

<#
在无法连接 Codex 的内网环境中，一键采集 CANoe Dynamic 能力并写出 JSON。
只创建临时 Offline Configuration，不发送 Raw Frame、诊断请求或 Replay。
#>

[CmdletBinding()]
param(
    [string]$OutputPath = (Join-Path (
        [Environment]::GetFolderPath('LocalApplicationData')
    ) 'AutomotiveNetworkDebugAgent/diagnostics/canoe-capabilities.json'),
    [string]$TempRoot
)

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
        & $candidate -c 'import anda.dynamic.probe, py_canoe' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $arguments = @(
                '-m',
                'anda.dynamic.probe',
                '--output',
                [System.IO.Path]::GetFullPath($OutputPath)
            )
            if ($TempRoot) {
                $arguments += @('--temp-root', [System.IO.Path]::GetFullPath($TempRoot))
            }
            & $candidate @arguments
            $probeExitCode = $LASTEXITCODE
            if (Test-Path -LiteralPath $OutputPath -PathType Leaf) {
                $report = Get-Content -LiteralPath $OutputPath -Raw -Encoding utf8 |
                    ConvertFrom-Json
                $probeTemp = [System.IO.Path]::GetFullPath(
                    [string]$report.temporary_directory
                )
                $probeLeaf = Split-Path -Leaf $probeTemp
                if (-not $probeLeaf.StartsWith('anda-canoe-probe-')) {
                    throw "拒绝清理非探测目录：$probeTemp"
                }
                for ($attempt = 1; $attempt -le 10; $attempt++) {
                    if (-not (Test-Path -LiteralPath $probeTemp)) {
                        break
                    }
                    try {
                        Remove-Item -LiteralPath $probeTemp -Recurse -Force
                    }
                    catch {
                        if ($attempt -eq 10) {
                            Write-Warning "CANoe 探测临时目录尚未释放：$probeTemp"
                        }
                        else {
                            Start-Sleep -Milliseconds 500
                        }
                    }
                }
            }
            exit $probeExitCode
        }
    }
    catch {
        continue
    }
}

throw '未找到同时包含 automotive-network-debug-agent 和 py-canoe==26.4.1 的 Python。'

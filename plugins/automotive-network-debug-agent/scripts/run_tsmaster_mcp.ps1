<# 启动独立 TSMaster MCP；解释器沿用本机 ANDA_PYTHON 或已配置的 Miniconda 环境。 #>
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run_mcp.ps1') -Module 'anda.mcp.tsmaster.server'
exit $LASTEXITCODE

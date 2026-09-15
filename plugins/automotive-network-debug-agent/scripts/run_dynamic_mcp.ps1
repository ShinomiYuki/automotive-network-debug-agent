<# 启动同一插件内的 Dynamic Verification MCP。 #>

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run_mcp.ps1') -Module 'anda.mcp.dynamic.server'
exit $LASTEXITCODE

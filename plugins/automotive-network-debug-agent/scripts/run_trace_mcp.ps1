<# 从插件目录启动 Trace MCP；解释器选择由同目录 run_mcp.ps1 统一处理。 #>

& (Join-Path $PSScriptRoot 'run_mcp.ps1') -Module 'anda.mcp.trace.server'
exit $LASTEXITCODE

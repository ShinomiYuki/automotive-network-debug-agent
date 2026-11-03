#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Codex', 'OpenCode', 'ChatGPT', 'DeepSeekHarness', 'Custom')]
    [string]$Target,

    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$skillName = 'autosar-tx-mailbox'
$sourceDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)

if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory 'SKILL.md') -PathType Leaf)) {
    throw '安装包缺少 SKILL.md。请解压完整的 autosar-tx-mailbox 目录后重试。'
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory 'runtime/tx-mailbox.exe') -PathType Leaf)) {
    throw '安装包缺少 runtime/tx-mailbox.exe。请使用包含运行环境的 Windows 发布包。'
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $userDirectory = [Environment]::GetFolderPath('UserProfile')
    switch ($Target) {
        'Codex' { $Destination = Join-Path $userDirectory ".agents/skills/$skillName" }
        'OpenCode' { $Destination = Join-Path $userDirectory ".config/opencode/skills/$skillName" }
        'DeepSeekHarness' {
            $agentsDirectory = if ([string]::IsNullOrWhiteSpace($env:DSH_AGENTS_HOME)) {
                Join-Path $userDirectory '.agents'
            } else {
                $env:DSH_AGENTS_HOME
            }
            $Destination = Join-Path $agentsDirectory "skills/$skillName"
        }
        'ChatGPT' {
            throw 'ChatGPT 没有通用的本地 skill 安装目录。普通云聊天请上传 ZIP；具备本地执行器时，请用 -Destination 明确指定该执行器实际发现的完整技能目录。'
        }
        'Custom' { throw 'Custom 安装必须通过 -Destination 指定完整技能目录。' }
    }
}

if (-not [System.IO.Path]::IsPathFullyQualified($Destination)) {
    throw '-Destination 必须是完整绝对路径，并包含最后一级 autosar-tx-mailbox 目录。'
}
$destinationDirectory = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationDirectory) {
    throw "目标已存在，不会覆盖：$destinationDirectory。请使用新的目标目录；如果已安装，请直接使用现有技能。"
}
if ($destinationDirectory.StartsWith($sourceDirectory + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw '目标目录不能位于安装包自身目录内。'
}

$parentDirectory = [System.IO.Path]::GetDirectoryName($destinationDirectory)
[void][System.IO.Directory]::CreateDirectory($parentDirectory)
$stagingDirectory = Join-Path $parentDirectory ('.autosar-tx-mailbox-install-' + [Guid]::NewGuid().ToString('N'))
try {
    [void][System.IO.Directory]::CreateDirectory($stagingDirectory)
    foreach ($entry in Get-ChildItem -LiteralPath $sourceDirectory -Force) {
        Copy-Item -LiteralPath $entry.FullName -Destination $stagingDirectory -Recurse -Force
    }
    [System.IO.Directory]::Move($stagingDirectory, $destinationDirectory)
} finally {
    if (Test-Path -LiteralPath $stagingDirectory) {
        $resolvedStaging = [System.IO.Path]::GetFullPath($stagingDirectory)
        if ([System.IO.Path]::GetDirectoryName($resolvedStaging) -eq $parentDirectory -and
            [System.IO.Path]::GetFileName($resolvedStaging).StartsWith('.autosar-tx-mailbox-install-')) {
            Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
        }
    }
}

Write-Output "已复制完整技能包：$destinationDirectory"
Write-Output '运行环境随包提供，不需要安装 Python、执行 pip 或连接汽车配置 MCP。'
if ($Target -eq 'ChatGPT' -or $Target -eq 'Custom') {
    Write-Output '请让当前 AI 读取该目录的 SKILL.md；自动发现取决于宿主实际配置。'
} else {
    Write-Output '请让 AI 加载 autosar-tx-mailbox；如果技能列表尚未更新，请重新打开会话。'
}

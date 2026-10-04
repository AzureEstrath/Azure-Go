# 重新打包游戏 PCK（分享版只需替换 AzureGoGodot.pck，exe 是 Godot 引擎本体）
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File tools\build_pck.ps1
param(
    [string]$Godot   = "D:\Godot\Godot_v4.6.3-stable_win64_console.exe",
    [string]$Project = (Join-Path $PSScriptRoot "..\game"),
    [string]$OutPck  = (Join-Path $PSScriptRoot "..\..\AzureGoGodot-share\AzureGoGodot.pck")
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Godot)) { throw "找不到 Godot：$Godot（用 -Godot 指定）" }
$Project = (Resolve-Path $Project).Path

Write-Host "1/2 导入资源：$Project"
& $Godot --headless --path $Project --import | Out-Null

Write-Host "2/2 导出 PCK → $OutPck"
& $Godot --headless --path $Project --export-pack "Windows Desktop" $OutPck

if (Test-Path $OutPck) {
    Write-Host ("完成：{0}（{1:N0} 字节）" -f $OutPck, (Get-Item $OutPck).Length)
} else {
    throw "导出失败，未生成 PCK"
}
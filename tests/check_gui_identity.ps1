# Compile the actual injector, but invoke ONLY its pure identity predicate.
# Do not dot-source the injector: its top-level code performs GUI actions.
$ErrorActionPreference = 'Stop'
$sourcePath = Join-Path (Split-Path $PSScriptRoot -Parent) 'gui_inject.ps1'
$source = [IO.File]::ReadAllText($sourcePath)
$match = [regex]::Match($source, '(?s)Add-Type -TypeDefinition @"\r?\n(.*?)\r?\n"@ -ReferencedAssemblies')
if (-not $match.Success) { throw 'C# source block not found' }
Add-Type -TypeDefinition $match.Groups[1].Value -ReferencedAssemblies UIAutomationClient, UIAutomationTypes, WindowsBase
function Check($expected, $sid, $aid, $help, $name, $selected, $document, $offscreen, $targetTitle = '') {
    $actual = [GuiInjector]::IsActiveTaskIdentity($sid, $aid, $help, $name, $selected, $document, $offscreen, $targetTitle)
    if ($actual -ne $expected) {
        throw "Identity predicate mismatch: expected=$expected actual=$actual aid=$aid name=$name targetTitle=$targetTitle"
    }
}
Check $true 'sid-123' 'active-thread-sid-123' '' '' $false $false $false
Check $true 'sid-123' 'thread-sid-123' '' '' $false $true $false
Check $true 'sid-123' '' 'codex://threads/sid-123' '' $false $true $false
Check $false 'sid-123' '' '' 'Same task title' $true $false $false
Check $false 'sid-123' 'thread-sid-123' '' '' $false $false $false
Check $false 'sid-123' 'thread-sid-123' '' '' $true $false $false
Check $false 'sid-123' '' '' 'Transcript quotes sid-123' $false $true $false
Check $false 'sid-123' 'active-thread-sid-1234' '' '' $true $true $false
Check $false 'sid-123' 'active-thread-sid-123' '' '' $true $true $true
Check $false '' 'active-thread-' '' '' $true $true $false

$fullSid = '01a0cc3a-2f4f-7791-be89-f5f59e005336'
$shortSid = '01a0cc3a'
$exactTitle = 'Doloris long-horizon fork'
Check $true $fullSid "active-thread-$shortSid" '' $exactTitle $false $false $false $exactTitle
Check $true $fullSid "thread-$shortSid" '' $exactTitle $false $false $false $exactTitle
Check $true $fullSid '' "codex://threads/$shortSid" $exactTitle $false $false $false $exactTitle
Check $false $fullSid "active-thread-$shortSid" '' $exactTitle $false $false $false
Check $false $fullSid '' '' $exactTitle $false $true $false $exactTitle
Check $false $fullSid "active-thread-$shortSid" '' 'Different task title' $false $false $false $exactTitle
Check $false $fullSid 'active-thread-01a0cc3' '' $exactTitle $false $false $false $exactTitle
Check $false $fullSid "active-thread-$shortSid" '' "$exactTitle..." $false $false $false 'Another exact title'

# 同一 SID 的多个标题来源 (侧栏显示名 / 简短标题 / 旧索引名) 用 Unit Separator (0x1F)
# 拼接为候选集合；集合内命中任意一个即认定为同一任务，但集合外仍必须拒绝。
$separator = [char]0x1F
if ([int]$separator -ne 31) {
    # Windows PowerShell 5.1 会把无 BOM 的 UTF-8 源文件按 ANSI 解码；中文注释一旦吞掉换行，
    # 下面的赋值就会整体变成注释。这里显式失败，避免测试静默通过。
    throw "Separator literal lost: expected 0x1F, got $([int]$separator). Check file encoding (UTF-8 BOM required)."
}
$titleSet = $exactTitle + $separator + 'Fork display name'
if ([GuiInjector]::SplitTitles($titleSet).Count -ne 2) {
    throw 'SplitTitles did not split the candidate title set on Unit Separator.'
}
Check $true $fullSid "active-thread-$shortSid" '' $exactTitle $false $false $false $titleSet
Check $true $fullSid "active-thread-$shortSid" '' 'Fork display name' $false $false $false $titleSet
Check $true $fullSid '' "codex://threads/$shortSid" 'Fork display name' $false $false $false $titleSet
Check $true $fullSid "thread-$shortSid" '' "$exactTitle..." $false $true $false $titleSet
Check $false $fullSid "active-thread-$shortSid" '' 'Fork display name' $false $false $false ($exactTitle + $separator + 'Other title')
Check $false $fullSid "active-thread-$shortSid" '' $exactTitle $false $false $false ([string]$separator)
Write-Output 'IDENTITY_TESTS_OK'

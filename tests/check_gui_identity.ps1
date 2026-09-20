# Compile the actual injector, but invoke ONLY its pure identity predicate.
# Do not dot-source the injector: its top-level code performs GUI actions.
$ErrorActionPreference = 'Stop'
$sourcePath = Join-Path (Split-Path $PSScriptRoot -Parent) 'gui_inject.ps1'
$source = [IO.File]::ReadAllText($sourcePath)
$match = [regex]::Match($source, '(?s)Add-Type -TypeDefinition @"\r?\n(.*?)\r?\n"@ -ReferencedAssemblies')
if (-not $match.Success) { throw 'C# source block not found' }
Add-Type -TypeDefinition $match.Groups[1].Value -ReferencedAssemblies UIAutomationClient, UIAutomationTypes, WindowsBase
function Check($expected, $sid, $aid, $help, $name, $selected, $document, $offscreen) {
    $actual = [GuiInjector]::IsActiveTaskIdentity($sid, $aid, $help, $name, $selected, $document, $offscreen)
    if ($actual -ne $expected) { throw "Identity predicate mismatch: aid=$aid name=$name" }
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
Write-Output 'IDENTITY_TESTS_OK'

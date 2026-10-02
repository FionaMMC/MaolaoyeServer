param([Parameter(Mandatory=$true)][string]$OutputDirectory)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Output exists; use a fresh private directory.' }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$items = @()
foreach ($task in @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'Hydra*' })) {
    $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath
    $safeName = ($task.TaskPath + $task.TaskName) -replace '[^a-zA-Z0-9_-]', '_'
    Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath |
        Set-Content -LiteralPath (Join-Path $OutputDirectory ($safeName + '.xml')) -Encoding UTF8
    $items += [pscustomobject]@{Name=$task.TaskName;Path=$task.TaskPath;State=[string]$task.State;LastRun=$info.LastRunTime;LastResult=$info.LastTaskResult;NextRun=$info.NextRunTime}
}
$items | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'tasks.json') -Encoding UTF8
@{status='READ_ONLY_TASK_INVENTORY';count=$items.Count;changed_tasks=0} | ConvertTo-Json -Compress

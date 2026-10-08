# PowerShell script to register Antigravity Telegram AI Butler as auto-starting Windows Task
$ErrorActionPreference = "Stop"

$taskName = "AntigravityTelegramButler"
$scriptDir = "C:\Users\BRand\.gemini\antigravity\scratch\telegram_ai_butler"
$vbsPath = "$scriptDir\run_silently.vbs"

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsPath`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 365)

# Register or overwrite task
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force

Write-Host "TASK_REGISTERED_SUCCESSFULLY: $taskName"

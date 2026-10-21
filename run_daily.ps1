# run_daily.ps1 - generate today's investment decision report (also registers a Windows scheduled task)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\run_daily.ps1 -Tier balanced -Capital 100000
#   powershell -ExecutionPolicy Bypass -File .\run_daily.ps1 -Tier conservative -Capital 100000 -RegisterTask
#
# NOTE: this file is intentionally ASCII-only, so Windows PowerShell 5.1 (which reads .ps1
# as ANSI/GBK when there is no BOM) can parse it correctly. Chinese text lives in the Python
# modules/reports instead.
param(
    [ValidateSet('conservative', 'balanced', 'aggressive')]
    [string]$Tier = 'balanced',
    [double]$Capital = 100000,
    [ValidateSet('auto', 'build', 'rebalance')]
    [string]$Mode = 'auto',
    [switch]$WithBacktest,
    [switch]$RegisterTask,
    [string]$TaskName = 'InvestmentAdvisorDaily'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Force UTF-8 so the Chinese report text is not mangled
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$logDir = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("run_{0}_{1}.log" -f (Get-Date -Format 'yyyyMMdd'), $Tier)

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

try {
    Write-Log "START tier=$Tier capital=$Capital mode=$Mode"
    $pythonArgs = @('-m', 'advisor.cli', 'advise', '--tier', $Tier, '--capital', "$Capital", '--mode', $Mode, '--quiet')
    if ($WithBacktest) { $pythonArgs += '--with-backtest' }

    & python @pythonArgs 2>&1 | Tee-Object -FilePath $logFile -Append | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "advisor.cli exited with code $LASTEXITCODE" }

    $latest = Get-ChildItem (Join-Path $root 'reports') -Filter "*_$Tier`_*.md" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Write-Log ("DONE report={0}" -f $latest.FullName)
}
catch {
    Write-Log ("FAILED {0}" -f $_.Exception.Message)
    Write-Log 'HINT run: python -m advisor.cli data-check'
    exit 1
}

if ($RegisterTask) {
    $ps = (Get-Command powershell.exe).Source
    $scriptPath = Join-Path $root 'run_daily.ps1'
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument ("-ExecutionPolicy Bypass -File `"{0}`" -Tier {1} -Capital {2}" -f $scriptPath, $Tier, $Capital) `
        -WorkingDirectory $root
    # 17:30 every day (after market close); harmless on non-trading days
    $trigger = New-ScheduledTaskTrigger -Daily -At '17:30'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Daily rule-based investment decision report' -Force | Out-Null
    Write-Host "Scheduled task registered: $TaskName (daily 17:30)"
}

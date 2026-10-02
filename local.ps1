param([ValidateSet('start','stop','status')][string]$Action='status')
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$script=Join-Path $root 'desk.py'
$python=(Get-Command python).Source
$processes=@(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -match [regex]::Escape($script) -and $_.CommandLine -match '\bserve\b'
})
if($Action -eq 'status') { $processes | Select-Object ProcessId,CommandLine; return }
if($Action -eq 'stop') {
    foreach($entry in $processes) {
        $now=Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.ProcessId)"
        if($now -and $now.CommandLine -eq $entry.CommandLine) {Stop-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue}
    }
    'Stopped workbench. Interrupted investigations will require review on restart.'; return
}
if($processes.Count) {'Workbench is already running.';return}
if(Get-NetTCPConnection -State Listen -LocalPort 8805 -ErrorAction SilentlyContinue) {throw 'Port 8805 belongs to another process.'}
& $python $script init
if($LASTEXITCODE -ne 0) {throw 'Initialization failed.'}
Start-Process -FilePath $python -ArgumentList @(('"'+$script+'"'),'serve') -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'runtime/server.log') -RedirectStandardError (Join-Path $root 'runtime/server-error.log')
'Starting http://127.0.0.1:8805; logs are under runtime/.'

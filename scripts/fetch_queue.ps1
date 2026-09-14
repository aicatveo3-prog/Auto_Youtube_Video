<#
    fetch_queue.ps1 - batch transcript fetcher for the PostFileSave hook.

    Reads urls.txt in the workspace root and pulls a transcript for every
    entry. Runs entirely as a local shell command: no LLM call, so it costs
    zero Kiro credits. Progress goes to transcripts/_fetch.log, never to
    stdout, because anything printed here could end up in an agent context
    on stdout-forwarding triggers.

    Lines beginning with # are ignored. Blank lines are ignored.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = Split-Path -Parent $PSScriptRoot
$queue = Join-Path $root 'urls.txt'
$logDir = Join-Path $root 'transcripts'
$log = Join-Path $logDir '_fetch.log'

if (-not (Test-Path $queue)) { exit 0 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log([string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $log -Value "[$stamp] $Message" -Encoding utf8
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Log 'python not found on PATH'; exit 0 }

$targets = Get-Content -LiteralPath $queue -Encoding utf8 |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith('#') }

if (-not $targets) { exit 0 }

$env:PYTHONIOENCODING = 'utf-8'
$ytscript = Join-Path $PSScriptRoot 'ytscript.py'
$ok = 0
$fail = 0

Write-Log "run start: $($targets.Count) target(s)"
foreach ($t in $targets) {
    # Arguments stay as separate array elements so a hostile line in urls.txt
    # cannot break out into a shell command.
    $out = & $python $ytscript 'fetch' $t 2>&1
    if ($LASTEXITCODE -eq 0) {
        $ok++
        Write-Log "ok   $t :: $($out -join ' ')"
    }
    else {
        $fail++
        Write-Log "FAIL $t :: $($out -join ' ')"
    }
}
Write-Log "run end: ok=$ok fail=$fail"
exit 0

$PY = ".\.venv\Scripts\python.exe"

Write-Host "== pytest =="
& $PY -m pytest -q
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== angle literal scan =="
$hits = Get-ChildItem -Path server -Filter *.py -Recurse | Select-String -Pattern "\b(180|125|120|105|80)\b"
if ($hits) {
    $hits | ForEach-Object { Write-Host $_.ToString() }
    Write-Host "angle literal found - R1 violation"
    exit 1
}

Write-Host "== calibration hash =="
if (Test-Path "tools\gen_calibration.py") {
    & $PY tools\gen_calibration.py --check
    if ($LASTEXITCODE -ne 0) { exit 1 }
} else {
    Write-Host "skip (tools/gen_calibration.py not implemented yet)"
}

Write-Host "OK"
exit 0

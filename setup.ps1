# Windows bootstrap for the runnable AutoImplants loop.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    & uv venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv could not create the Python 3.12 environment" }
    & uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
}
else {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $launcher) {
        throw "Install Python 3.12 or uv, then run this script again."
    }
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is not available through py.exe" }
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
}

& .\.venv\Scripts\python.exe -c "import sys, cadquery, trimesh; print('python', sys.version.split()[0]); print('cadquery', cadquery.__version__); print('trimesh', trimesh.__version__)"
if ($LASTEXITCODE -ne 0) { throw "environment verification failed" }

Write-Host ""
Write-Host "Environment ready. Run the baseline with:"
Write-Host "  .\.venv\Scripts\python.exe -m autoimplants.run --validators geometry,stress"

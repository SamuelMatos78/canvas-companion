$ErrorActionPreference = "Stop"

# Configure once before publishing on your portfolio.
$DefaultSourceUrl = ""
$SourceUrl = $env:PUC_SOURCE_URL
if ([string]::IsNullOrWhiteSpace($SourceUrl)) { $SourceUrl = $DefaultSourceUrl }
if ([string]::IsNullOrWhiteSpace($SourceUrl)) {
    $SourceUrl = Read-Host "Cole o link público do ZIP de código-fonte no Google Drive"
}
if ($SourceUrl -notmatch '^https?://') {
    throw "Informe um link HTTP ou HTTPS do Google Drive."
}

$PythonCommand = $null
$PythonPrefix = @()
$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($PyLauncher) {
    $PythonCommand = $PyLauncher.Source
    $PythonPrefix = @("-3")
}
if (-not $PythonCommand) {
    foreach ($Candidate in @("python", "python3")) {
        $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Found) { $PythonCommand = $Found.Source; break }
    }
}
if (-not $PythonCommand) { throw "Python 3.10 ou superior é necessário. Instale Python e execute novamente." }
$VersionText = & $PythonCommand @PythonPrefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Não foi possível iniciar o Python." }
$VersionParts = $VersionText.Trim().Split(".")
if ([int]$VersionParts[0] -lt 3 -or ([int]$VersionParts[0] -eq 3 -and [int]$VersionParts[1] -lt 10)) {
    throw "Python 3.10 ou superior é necessário (encontrado: $VersionText)."
}

$DriveId = $null
if ($SourceUrl -match 'drive\.google\.com') {
    if ($SourceUrl -match '/d/([A-Za-z0-9_-]+)') { $DriveId = $Matches[1] }
    elseif ($SourceUrl -match '[?&]id=([A-Za-z0-9_-]+)') { $DriveId = $Matches[1] }
}
$DownloadUrl = $SourceUrl
if ($DriveId) { $DownloadUrl = "https://drive.google.com/uc?export=download&id=$DriveId" }

$AppRoot = Join-Path $env:LOCALAPPDATA "PUC Central"
$WorkDir = Join-Path $env:TEMP ("puc-central-" + [Guid]::NewGuid().ToString("N"))
$Archive = Join-Path $WorkDir "source.zip"
$Extracted = Join-Path $WorkDir "extracted"
$MaxArchiveBytes = 500000000L
New-Item -ItemType Directory -Force -Path $WorkDir, $Extracted, $AppRoot | Out-Null

try {
    $DriveSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $Archive -UseBasicParsing -WebSession $DriveSession
    if ((Get-Item -LiteralPath $Archive).Length -gt $MaxArchiveBytes) { throw "O ZIP excede o limite de download de 500 MB." }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $ValidZip = $false
    try {
        $Zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
        $BasePath = [System.IO.Path]::GetFullPath($Extracted) + [System.IO.Path]::DirectorySeparatorChar
        $TotalBytes = 0L
        foreach ($Entry in $Zip.Entries) {
            $TargetPath = [System.IO.Path]::GetFullPath((Join-Path $Extracted $Entry.FullName))
            if (-not $TargetPath.StartsWith($BasePath, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "O ZIP contém um caminho inválido."
            }
            $TotalBytes += $Entry.Length
            if ($TotalBytes -gt 1500000000) { throw "O ZIP excede o limite de extração de 1,5 GB." }
        }
        $Zip.Dispose()
        $ValidZip = $true
    } catch {
        if ($Zip) { $Zip.Dispose() }
    }

    if (-not $ValidZip -and $DriveId) {
        if ((Get-Item -LiteralPath $Archive).Length -gt 10000000) { throw "O arquivo recebido não parece ser um ZIP válido do Drive." }
        $Html = [System.IO.File]::ReadAllText($Archive)
        $Confirmation = [regex]::Match($Html, 'confirm=([0-9A-Za-z_-]+)')
        if (-not $Confirmation.Success) { $Confirmation = [regex]::Match($Html, 'name="confirm"\s+value="([^"]+)"') }
        if ($Confirmation.Success) {
            $Token = $Confirmation.Groups[1].Value
            $ConfirmedUrl = "https://drive.google.com/uc?export=download&id=$DriveId&confirm=$Token"
            Invoke-WebRequest -Uri $ConfirmedUrl -OutFile $Archive -UseBasicParsing -WebSession $DriveSession
            if ((Get-Item -LiteralPath $Archive).Length -gt $MaxArchiveBytes) { throw "O ZIP excede o limite de download de 500 MB." }
            try {
                $Zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
                $BasePath = [System.IO.Path]::GetFullPath($Extracted) + [System.IO.Path]::DirectorySeparatorChar
                $TotalBytes = 0L
                foreach ($Entry in $Zip.Entries) {
                    $TargetPath = [System.IO.Path]::GetFullPath((Join-Path $Extracted $Entry.FullName))
                    if (-not $TargetPath.StartsWith($BasePath, [System.StringComparison]::OrdinalIgnoreCase)) {
                        throw "O ZIP contém um caminho inválido."
                    }
                    $TotalBytes += $Entry.Length
                    if ($TotalBytes -gt 1500000000) { throw "O ZIP excede o limite de extração de 1,5 GB." }
                }
                $Zip.Dispose()
                $ValidZip = $true
            } catch { $ValidZip = $false }
        }
    }
    if (-not $ValidZip) { throw "O download não parece ser um ZIP público válido. Confira o compartilhamento do arquivo no Drive." }

    [System.IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Extracted)
    $ProjectFile = Get-ChildItem -LiteralPath $Extracted -Filter "puc.py" -File -Recurse | Select-Object -First 1
    if (-not $ProjectFile) { throw "Não encontrei puc.py no arquivo ZIP." }
    $ProjectDir = $ProjectFile.Directory.FullName
    if (-not (Test-Path (Join-Path $ProjectDir "requirements.txt"))) { throw "O ZIP não contém requirements.txt ao lado de puc.py." }

    $NewSource = Join-Path $WorkDir "source"
    New-Item -ItemType Directory -Force -Path $NewSource | Out-Null
    Get-ChildItem -LiteralPath $ProjectDir -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $NewSource -Recurse -Force
    }
    $SourcePath = Join-Path $AppRoot "source"
    $OldSource = Join-Path $AppRoot "source.old"
    if (Test-Path $OldSource) { Remove-Item -LiteralPath $OldSource -Recurse -Force }
    if (Test-Path $SourcePath) { Move-Item -LiteralPath $SourcePath -Destination $OldSource }
    Move-Item -LiteralPath $NewSource -Destination $SourcePath
    if (Test-Path $OldSource) { Remove-Item -LiteralPath $OldSource -Recurse -Force }

    $Venv = Join-Path $AppRoot "venv"
    if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
        & $PythonCommand @PythonPrefix -m venv $Venv
        if ($LASTEXITCODE -ne 0) { throw "Não foi possível preparar o ambiente Python." }
    }
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Falha ao preparar o pip." }
    & $VenvPython -m pip install -r (Join-Path $SourcePath "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as dependências Python." }

    $BinDir = Join-Path $AppRoot "bin"
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $Launcher = Join-Path $BinDir "puc.cmd"
    @'
@echo off
set "PUC_HOME=%LOCALAPPDATA%\PUC Central"
set "PUC_DOWNLOADS_ROOT=%PUC_HOME%\data\downloads_canvas"
set "PUC_COURSES_CACHE=%PUC_HOME%\data\cursos.json"
pushd "%PUC_HOME%\source"
"%PUC_HOME%\venv\Scripts\python.exe" "%PUC_HOME%\source\puc.py" %*
set "PUC_EXIT=%ERRORLEVEL%"
popd
exit /b %PUC_EXIT%
'@ | Set-Content -LiteralPath $Launcher -Encoding ASCII

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathParts = @($UserPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($PathParts -notcontains $BinDir) {
        $NewUserPath = (@($PathParts) + $BinDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
    }
    if (($env:Path -split ";") -notcontains $BinDir) { $env:Path = $env:Path + ";" + $BinDir }

    Write-Host "Instalação concluída. Abra um terminal novo e rode: puc" -ForegroundColor Green
    Write-Host "Para conferir o ambiente: puc debug"
    Write-Host "A interface local abre em http://127.0.0.1:8091."
} finally {
    if (Test-Path $WorkDir) { Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue }
}

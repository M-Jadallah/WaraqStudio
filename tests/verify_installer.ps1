param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedOpenCodeVersion
)

$ErrorActionPreference = 'Stop'
# This installs and uninstalls software, so restrict it to disposable CI hosts.
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'Installer acceptance runs only on a disposable GitHub Actions runner.'
}

function Invoke-HiddenCheck {
    param([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds = 120)
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        & taskkill /PID $process.Id /T /F | Out-Null
        throw "Process timed out: $Executable"
    }
    $process.Refresh()
    if ($process.ExitCode -ne 0) {
        throw "Process failed with exit code $($process.ExitCode): $Executable"
    }
}

$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$runnerRoot = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)) + [IO.Path]::DirectorySeparatorChar
$checkRoot = [IO.Path]::GetFullPath((Join-Path $runnerRoot ('waraq-installer-' + [guid]::NewGuid().ToString('N'))))
if (-not $checkRoot.StartsWith($runnerRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installer test directory escaped RUNNER_TEMP.'
}
$installDir = Join-Path $checkRoot 'Installed Waraq'
$dataDir = Join-Path $checkRoot 'User Data'
$installLog = Join-Path $checkRoot 'install.log'
$reportPath = Join-Path $checkRoot 'environment.json'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$sentinel = Join-Path $dataDir 'preserve-user-data.txt'
Set-Content -LiteralPath $sentinel -Value 'Keep user data after uninstall.' -Encoding utf8
$originalData = Get-FileHash -LiteralPath $sentinel -Algorithm SHA256
$previousQtPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = 'offscreen'

try {
    Invoke-HiddenCheck $installer @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', "/DIR=`"$installDir`"", "/LOG=`"$installLog`"")
    $executable = Join-Path $installDir 'WaraqStudio.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw 'Installer did not create WaraqStudio.exe.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $installDir '_internal/python314.dll') -PathType Leaf)) {
        throw 'Installer did not include the Python runtime.'
    }
    Invoke-HiddenCheck $executable @('--data-dir', "`"$dataDir`"", '--check-environment', "`"$reportPath`"")
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($report.version -ne $ExpectedVersion -or -not $report.ffmpeg_exists) {
        throw 'Installed application version or FFmpeg runtime check failed.'
    }
    if ($report.opencode_version -ne $ExpectedOpenCodeVersion) {
        throw "Installed application did not detect OpenCode $ExpectedOpenCodeVersion."
    }
    Invoke-HiddenCheck $executable @('--data-dir', "`"$dataDir`"", '--smoke')

    $uninstaller = Join-Path $installDir 'unins000.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw 'Installer did not create its uninstaller.'
    }
    Invoke-HiddenCheck $uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
    if (Test-Path -LiteralPath $executable) {
        throw 'Uninstaller left the application executable behind.'
    }
    if ((Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash -ne $originalData.Hash) {
        throw 'Uninstaller changed the user data sentinel.'
    }
    Write-Output "Installer acceptance passed: Waraq $ExpectedVersion, OpenCode $ExpectedOpenCodeVersion, install/launch/uninstall/user-data preservation."
} catch {
    if (Test-Path -LiteralPath $installLog) {
        Get-Content -LiteralPath $installLog -Tail 60
    }
    throw
} finally {
    $env:QT_QPA_PLATFORM = $previousQtPlatform
}

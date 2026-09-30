<#
    build.ps1 - builds Background Editor: source zips, packaged app, bundle checks,
    self-test, signing, portable zip, installer and the signed release manifest.

    Usage (PowerShell in this folder):
        .\build.ps1                     # full build: everything below, signed
        .\build.ps1 -NoInstaller        # skip Inno Setup (the zips are still made)
        .\build.ps1 -NoSign             # skip Authenticode signing (the app will not
                                        # install such a build as an update)
        .\build.ps1 -Version 1.1.0-rc1  # build under another version than the source's
        .\build.ps1 -MinUpdateFrom 1.2.0
                                        # only copies of 1.2.0 or newer may update to this
                                        # build automatically (default 1.1.0)
        .\build.ps1 -CertSha256 <SHA-256>
                                        # sign with another certificate than the one the
                                        # app pins (only for the key-rotation release; see
                                        # README.md, "Replacing the signing key")

    Results in dist\:
        BackgroundEditor\                                 the app folder (what the installer installs)
        BackgroundEditor-Setup-<ver>.exe                  the installer
        BackgroundEditor-<ver>-portable.zip               the app folder plus portable.conf
        BackgroundEditor-<ver>-manifest.ps1               the signed release manifest: SHA-256 and
                                                          size of the installer, the portable zip and
                                                          every file in it (data only, never run)
        BackgroundEditor-<ver>-src.zip                    source of this program (also in the app
                                                          folder as source\BackgroundEditor-<ver>-src.zip)
        BackgroundEditor-<ver>-third-party-sources.zip    source of the GPL/LGPL/MPL components
        BackgroundEditor-<ver>-SHA256SUMS.txt
    Upload these six files to the GitHub release (README.md, "Publishing a release"). The app
    installs an update only when the release carries the signed manifest. Give the two source
    zips along whenever you give the program to someone else (GNU GPL v3, section 6).

    The version comes from __version__ in bgeditor\__init__.py, which the spec and
    installer.iss read as well. -Version overrides it for one build: the exe's version
    resource, About, the download User-Agent and the installer all get the override.
    The signing certificate is the one in CurrentUser\My whose SHA-256 (of its DER encoding)
    is SIGNER_CERT_SHA256 in bgeditor\__init__.py, the certificate the app pins.

    Needs: the project venv (.venv) with the packages in requirements.txt, Inno Setup 6
    (ISCC.exe) for the installer, the code signing certificate in CurrentUser\My for
    signing, and internet on the first build: the third-party source archives listed in
    tools\third_party_sources.json are downloaded once into -SourceCache, checked against
    their pinned SHA-256, and reused from there.
#>
# Background Editor - portrait-aware background removal
# Copyright (C) 2026 Optimey CommV
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.
param(
    [string]$Version = "",
    [switch]$NoInstaller,
    [switch]$NoSign,
    [string]$MinUpdateFrom = "1.1.0",
    [string]$CertSha256 = "",
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [int]$SelfTestTimeout = 300,
    [string]$SourceCache = (Join-Path $env:LOCALAPPDATA "BackgroundEditor-build-cache")
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Project venv not found: $py" }
$tools = Join-Path $PSScriptRoot "tools\build_tools.py"
$init = Join-Path $PSScriptRoot "bgeditor\__init__.py"

if (-not $Version) {
    $m = Select-String -Path $init -Pattern '^__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $m) { throw "No __version__ = `"...`" found in $init" }
    $Version = $m.Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d+(\.\d+){0,3}([-+][0-9A-Za-z.-]+)?$') {
    throw "Version '$Version' does not look like 1.2.3 or 1.2.3-beta1"
}
if ($MinUpdateFrom -and $MinUpdateFrom -notmatch '^\d+(\.\d+){0,3}$') {
    throw "MinUpdateFrom '$MinUpdateFrom' does not look like 1.2.3"
}
# The certificate the app pins; the build signs with it unless -CertSha256 says otherwise.
$pinLine = Select-String -Path $init -Pattern '^SIGNER_CERT_SHA256\s*=\s*"([0-9A-Fa-f]{64})"' | Select-Object -First 1
if (-not $pinLine) { throw "No SIGNER_CERT_SHA256 = `"...`" found in $init" }
$pin = $pinLine.Matches[0].Groups[1].Value.ToUpperInvariant()
if (-not $CertSha256) { $CertSha256 = $pin }
$CertSha256 = $CertSha256.ToUpperInvariant()
Write-Host "Background Editor $Version" -ForegroundColor Yellow
if (-not $NoSign -and $CertSha256 -ne $pin) {
    Write-Warning ("Signing with certificate $CertSha256, but this build pins $pin. Only the release that " +
        "hands over to a new signing key does this (README.md, 'Replacing the signing key').")
}

function Invoke-Tool([string]$what) {
    # Runs tools\build_tools.py with the remaining arguments; stops the build on failure.
    & $py $tools @args
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

# PyInstaller's work folder on the local disk: much faster than on a network share.
$work = Join-Path $env:TEMP "BackgroundEditor-build"
$dist = Join-Path $PSScriptRoot "dist"
$appDir = Join-Path $dist "BackgroundEditor"
$exe = Join-Path $appDir "BackgroundEditor.exe"
$base = "BackgroundEditor-$Version"
$srcZip = Join-Path $dist "$base-src.zip"
$thirdZip = Join-Path $dist "$base-third-party-sources.zip"
$portableZip = Join-Path $dist "$base-portable.zip"
$setup = Join-Path $dist "BackgroundEditor-Setup-$Version.exe"
$manifest = Join-Path $dist "$base-manifest.ps1"
$bundleReport = Join-Path $work "bundle-check.json"
New-Item -ItemType Directory -Force -Path $dist, $work | Out-Null
# Results of an earlier build of this version must not end up in this one's checksums.
foreach ($old in @($manifest, $setup)) { if (Test-Path $old) { Remove-Item -Force $old } }

Write-Host "=== 1/9 Source zip ===" -ForegroundColor Cyan
# Made first, from the tree PyInstaller is about to build; step 4 checks that nothing changed.
Invoke-Tool "Source zip" source-zip --version $Version --out $srcZip

Write-Host "=== 2/9 Third-party sources ===" -ForegroundColor Cyan
Invoke-Tool "Third-party sources zip" third-party --version $Version --cache $SourceCache --out $thirdZip

Write-Host "=== 3/9 Packaging with PyInstaller ===" -ForegroundColor Cyan
if (Test-Path $appDir) { Remove-Item -Recurse -Force $appDir }
$env:BGR_VERSION = $Version
try {
    & $py -m PyInstaller BackgroundEditor.spec --noconfirm --clean --distpath $dist --workpath $work
} finally {
    Remove-Item Env:BGR_VERSION -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
# app.py needs QtNetwork for the single-instance hand-over; an exclude would break every launch.
if (-not (Get-ChildItem $appDir -Recurse -Filter "QtNetwork*.pyd")) { throw "PyQt6.QtNetwork is missing from the build" }

Write-Host "=== 4/9 Source, licences and bundle checks ===" -ForegroundColor Cyan
Invoke-Tool "Source check" verify-source-zip --zip $srcZip
$appSource = Join-Path $appDir "source"
New-Item -ItemType Directory -Force -Path $appSource | Out-Null
Copy-Item $srcZip (Join-Path $appSource "$base-src.zip")
Invoke-Tool "Qt notices" qt-notices --cache $SourceCache --out (Join-Path $appDir "licenses\Qt6\THIRD-PARTY-NOTICES.txt")
# Fails on DirectML.dll, VC++ runtime DLLs, Qt PDF, OpenCV/IPP, AVIF, unresolved imports,
# a missing LICENSE or source zip, and copyleft files without a source archive.
Invoke-Tool "Bundle checks" check-bundle --app-dir $appDir --version $Version --json-out $bundleReport
$bundle = Get-Content $bundleReport -Raw | ConvertFrom-Json
$vcMin = [string]$bundle.vc_runtime_min
if ($vcMin -notmatch '^(\d+)\.(\d+)$') { throw "Could not work out the minimum VC++ runtime version ('$vcMin')" }
$vcMajor = [int]$Matches[1]; $vcMinor = [int]$Matches[2]

function Start-App([string]$arguments, [int]$seconds) {
    # Start the packaged exe with CreateProcess (UseShellExecute = false), not ShellExecute:
    # on a network drive Windows puts the exe in the Internet zone, and ShellExecute then
    # shows the "Open File - Security Warning" prompt, which blocks an unattended build.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $arguments
    $psi.WorkingDirectory = $appDir
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit($seconds * 1000)) {
        try { $proc.Kill() } catch { }
        throw "BackgroundEditor.exe $arguments did not finish within $seconds s"
    }
    return $proc.ExitCode
}

Write-Host "=== 5/9 Self-test of the packaged app ===" -ForegroundColor Cyan
$report = Join-Path $env:TEMP "BackgroundEditor-selftest.txt"
if (Test-Path $report) { Remove-Item $report }
$code = Start-App "--self-test `"$report`"" $SelfTestTimeout
if (-not (Test-Path $report)) { throw "Self-test produced no report (exit $code)" }
Get-Content $report | ForEach-Object { Write-Host "   $_" }
if ($code -ne 0) { throw "Self-test failed (exit $code)" }
# The self-test must not leave anything behind in the app folder (it goes into both
# the installer and the portable zip).
$stray = Get-ChildItem $appDir -Force | Where-Object { $_.Name -in @("portable.conf", "data", "models") }
if ($stray) { throw "The self-test left files in the app folder: $($stray.FullName -join ', ')" }

$script:signingCert = $null
function Get-SigningCert {
    # The certificate in CurrentUser\My whose SHA-256 (of its DER encoding) is $CertSha256.
    if ($script:signingCert) { return $script:signingCert }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    foreach ($c in @(Get-ChildItem Cert:\CurrentUser\My)) {
        $hex = -join ($sha.ComputeHash($c.RawData) | ForEach-Object { $_.ToString('X2') })
        if ($hex -eq $CertSha256) {
            if (-not $c.HasPrivateKey) { throw "Signing certificate $CertSha256 has no private key here" }
            $script:signingCert = $c
            return $c
        }
    }
    throw "Signing certificate with SHA-256 $CertSha256 not found in CurrentUser\My"
}

function Sign-File([string]$path) {
    $s = Set-AuthenticodeSignature -FilePath $path -Certificate (Get-SigningCert) -TimestampServer $TimestampServer -HashAlgorithm SHA256
    if ($s.Status -ne "Valid") { throw "Signing $path failed: $($s.Status) $($s.StatusMessage)" }
    Write-Host ("   signed {0}" -f (Split-Path $path -Leaf)) -ForegroundColor Green
}

if (-not $NoSign) {
    Write-Host "=== 6/9 Signing ===" -ForegroundColor Cyan
    Sign-File $exe
} else {
    Write-Host "=== 6/9 Signing skipped ===" -ForegroundColor DarkGray
}

Write-Host "=== 7/9 Portable zip ===" -ForegroundColor Cyan
Invoke-Tool "Portable zip" portable --app-dir $appDir --version $Version --vc-min $vcMin --out $portableZip

if (-not $NoInstaller) {
    Write-Host "=== 8/9 Installer ===" -ForegroundColor Cyan
    $iscc = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }
    & $iscc "/DAppVersion=$Version" "/DVCRedistMajor=$vcMajor" "/DVCRedistMinor=$vcMinor" "/Qp" installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)" }
    if (-not (Test-Path $setup)) { throw "Inno Setup did not produce $setup" }
    if (-not $NoSign) { Sign-File $setup }
} else {
    Write-Host "=== 8/9 Installer skipped ===" -ForegroundColor DarkGray
}

function Test-ManifestIsData([string]$path) {
    # PowerShell's own parser, without running anything: exactly one statement, which assigns
    # a single-quoted here-string (no expansion) to $BackgroundEditorManifest.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    $statements = @(if ($ast.EndBlock) { $ast.EndBlock.Statements })
    $ok = ($errors.Count -eq 0) -and -not ($ast.ParamBlock -or $ast.BeginBlock -or $ast.ProcessBlock -or $ast.DynamicParamBlock) -and
        ($statements.Count -eq 1) -and
        ($statements[0] -is [System.Management.Automation.Language.AssignmentStatementAst]) -and
        ($statements[0].Left.VariablePath.UserPath -eq 'BackgroundEditorManifest') -and
        ($statements[0].Right.Expression -is [System.Management.Automation.Language.StringConstantExpressionAst]) -and
        ($statements[0].Right.Expression.StringConstantType -eq 'SingleQuotedHereString')
    if (-not $ok) { throw "$path is not data only: PowerShell parses it as more than the assignment of one here-string" }
    Write-Host "   PowerShell parses it as data only (one single-quoted here-string assignment)"
}

function Test-WithApp([string]$path) {
    # The packaged app's own check (WinVerifyTrust through ctypes, the pin, the PE check and,
    # for the manifest, the parser), exactly as an installed copy runs it.
    $rep = Join-Path $work "verify-update.txt"
    if (Test-Path $rep) { Remove-Item $rep }
    $code = Start-App "--verify-update `"$path`" --report `"$rep`"" 120
    $text = if (Test-Path $rep) { (Get-Content $rep -Raw).Trim() } else { "(no report)" }
    Write-Host ("   BackgroundEditor.exe --verify-update {0}: {1}" -f (Split-Path $path -Leaf), $text)
    if ($code -ne 0) { throw "The packaged app refuses $path (exit $code): $text" }
}

Write-Host "=== 9/9 Release manifest ===" -ForegroundColor Cyan
# Made from the finished (signed) installer and portable zip, and signed last of all.
# (Windows PowerShell drops empty arguments to programs, so only non-empty ones are passed.)
$manifestArgs = @("--version", $Version, "--portable", $portableZip)
if ($MinUpdateFrom) { $manifestArgs += @("--min-update-from", $MinUpdateFrom) }
if (-not $NoInstaller) { $manifestArgs += @("--setup", $setup) }
Invoke-Tool "Release manifest" release-manifest @manifestArgs --out $manifest
if (-not $NoSign) { Sign-File $manifest }
Test-ManifestIsData $manifest
if (-not $NoSign) {
    Invoke-Tool "Manifest check" verify-manifest --pin $CertSha256 --manifest $manifest --app-dir $appDir @manifestArgs
    if ($CertSha256 -eq $pin) {
        Test-WithApp $manifest
        if (-not $NoInstaller) { Test-WithApp $setup }
    } else {
        # A hand-over release: this build pins the new certificate but is signed with the old
        # one, which only the previous versions pin (checked just above with that pin).
        Write-Host "   hand-over release: the packaged app pins $pin, so its own check is left out" -ForegroundColor DarkYellow
    }
} else {
    Invoke-Tool "Manifest check" verify-manifest --unsigned --manifest $manifest --app-dir $appDir @manifestArgs
}

# Final guard: everything that is handed out must exist, and the source zips with it.
$outputs = @($srcZip, $thirdZip, $portableZip) + $(if (-not $NoInstaller) { @($setup) } else { @() }) + @($manifest)
foreach ($f in $outputs + @((Join-Path $appDir "LICENSE"), (Join-Path $appSource "$base-src.zip"))) {
    if (-not (Test-Path $f)) { throw "Missing from the build: $f" }
}
$sums = Join-Path $dist "$base-SHA256SUMS.txt"
$lines = foreach ($f in $outputs) {
    "{0}  {1}" -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path $f -Leaf)
}
[System.IO.File]::WriteAllText($sums, (($lines -join "`n") + "`n"))

$size = (Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("`nApp folder: {0} ({1:N0} MB); needs the VC++ runtime {2} or newer" -f $appDir, $size, $vcMin) -ForegroundColor Yellow
foreach ($f in $outputs + @($sums)) {
    $item = Get-Item $f
    Write-Host ("{0,-52} {1,8:N1} MB" -f $item.Name, ($item.Length / 1MB)) -ForegroundColor Yellow
}
Write-Host "Upload these files to the GitHub release: $((@($outputs + @($sums)) | ForEach-Object { Split-Path $_ -Leaf }) -join ', ')" -ForegroundColor Yellow
if ($NoSign) { Write-Warning "This build is not signed: the app does not install it as an update." }

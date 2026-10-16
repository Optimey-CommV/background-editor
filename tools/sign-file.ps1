<#
    sign-file.ps1 - signs one file with a pinned Optimey code signing certificate.
    makensis runs it for the uninstaller it is about to pack (!uninstfinalize in
    installer.nsi, set up by build.ps1 -InstallerKind NSIS); build.ps1 signs the rest itself.

    The certificate is opened by its path Cert:\CurrentUser\My\<SHA-1 thumbprint> and its
    SHA-256 (of the DER encoding) must be -CertSha256; the store is never listed. Exit code 0
    when the signature is valid, 1 otherwise.
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
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Thumbprint,
    [Parameter(Mandatory = $true)][string]$CertSha256,
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
try {
    # Windows PowerShell started from PowerShell 7 inherits a PSModulePath that finds
    # PowerShell 7's Security module first; load Windows PowerShell's own by its path.
    Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1")
    if ($Thumbprint -notmatch '^[0-9A-Fa-f]{40}$') { throw "Not a SHA-1 thumbprint: $Thumbprint" }
    $certPath = "Cert:\CurrentUser\My\$Thumbprint"
    if (-not (Test-Path -LiteralPath $certPath)) { throw "Signing certificate $Thumbprint not found in CurrentUser\My" }
    $cert = Get-Item -LiteralPath $certPath
    $hex = -join ([System.Security.Cryptography.SHA256]::Create().ComputeHash($cert.RawData) | ForEach-Object { $_.ToString('X2') })
    if ($hex -ne $CertSha256.ToUpperInvariant()) { throw "Certificate $Thumbprint has SHA-256 $hex, not $CertSha256" }
    if (-not $cert.HasPrivateKey) { throw "Signing certificate $Thumbprint has no private key here" }
    $s = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -TimestampServer $TimestampServer -HashAlgorithm SHA256
    if ($s.Status -ne "Valid") { throw "Signing $Path failed: $($s.Status) $($s.StatusMessage)" }
    Write-Host ("   signed {0}" -f (Split-Path $Path -Leaf))
    exit 0
} catch {
    Write-Host "sign-file.ps1: $($_.Exception.Message)"
    exit 1
}

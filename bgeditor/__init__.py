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

"""Background Editor: portrait-aware background removal."""

# The single source of the version: BackgroundEditor.spec, build.ps1 and installer.iss
# read it from this line, so keep it a plain string literal.
__version__ = "1.1.0"
__author__ = "Optimey CommV"

# App updates (bgeditor.app_updates): the GitHub repository whose releases are checked, and
# the SHA-256 of the DER-encoded certificate that signs BackgroundEditor.exe, the installer
# and the release manifest ('CN=Optimey Code Signing (Joost), O=Optimey'; Windows shows it
# with the SHA-1 thumbprint DC5A657A60F166815ACDA04E71581F7313553BEA, which is not used for
# the check). Its CA is private, so other PCs do not trust the chain; an update is accepted
# only when it is signed by exactly one of the certificates in SIGNER_CERTS_ACCEPTED.
# build.ps1 signs with the certificate that has this SHA-256 and reads it from this line;
# README.md explains how to replace it.
UPDATE_REPO = "Optimey-CommV/background-editor"
SIGNER_CERT_SHA256 = "60F4CFB0C10D0D3247E29E460027F798981ED56A95BAA27F86779BF541C40C5A"

# Every certificate an update may be signed with (SHA-256 of the DER encoding, as above).
# The second one has the same name and CA and takes over from the first, whose key cannot
# be exported from the PC it was made on (SHA-1 thumbprint 55711A7F59D7D47D0CBF76673718446EB4CF4A61,
# valid until October 2031). Accepting both lets this version take updates signed with either.
SIGNER_CERTS_ACCEPTED = (
    "60F4CFB0C10D0D3247E29E460027F798981ED56A95BAA27F86779BF541C40C5A",
    "180E01743637A8FBE9BB6F556BAC8431790D8381F4E12F46F7FD09C137440224",
)

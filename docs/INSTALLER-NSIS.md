<!--
SPDX-FileCopyrightText: 2026 Optimey CommV

SPDX-License-Identifier: GPL-3.0-or-later
-->
# The NSIS installer (installer.nsi)

Status (2026-10-16): **written, not yet compiled or tested.** `installer.iss` (Inno Setup)
stays the installer of every release until `installer.nsi` has passed the tests below.
`build.ps1 -InstallerKind NSIS` builds it; the default is still `Inno`.

## Why

Inno Setup asks companies above USD 5000 in revenue to buy a licence (about EUR 145 per
developer). NSIS has the zlib licence: free for any use, no request to pay. The order of the
switch is Background Editor first, then AANC Extended, then CherryPatch.

## What it does, compared with installer.iss

| | installer.iss | installer.nsi |
|---|---|---|
| Install | per user, `%LOCALAPPDATA%\Programs\Background Editor` | the same |
| Pages | licence, folder (first install only), tasks, ready | licence, folder (first install only), components, progress, finish |
| Tasks | desktop icon, "Remove background" menu verb | the same two, as components; a reinstall offers the earlier choice |
| Uninstall entry | `HKCU\...\Uninstall\{80AB4259-...}_is1` | `HKCU\...\Uninstall\{80AB4259-...}` (no `_is1`) |
| VC++ runtime | registry and DLL check, download with Inno's page | the same checks; Windows PowerShell downloads and checks Microsoft's signature |
| Old Background Remover | removed silently | the same |
| Background Editor 1.1.x (Inno build) | n/a | removed silently first (models and settings stay); Setup waits until its uninstaller has finished |
| App still running | Restart Manager closes it | waits up to 30 s when the app started Setup, else asks to close it |
| Taskbar identity | `AppUserModelID` on the shortcuts | set through `IPropertyStore` with the System plug-in |
| Signing | build.ps1 signs the Setup | build.ps1 signs the Setup; makensis signs the uninstaller (`!uninstfinalize`, `tools\sign-file.ps1`) |
| Uninstall | removes the files in its uninstall log; asks before deleting models and settings | removes exactly the files build.ps1 listed from the app folder (Delete per file, RMDir without /r per folder), so anything else in those folders stays; the same question; a silent uninstall keeps them |
| Links | (not checked) | folders are never deleted through a junction or symbolic link: a models folder moved elsewhere with `mklink /J` keeps its files |
| Architecture | x64compatible: x64, and ARM64 with x64 emulation (Windows 11) | the same: 64-bit Windows 10 1809 or newer, ARM64 only from Windows 11 |

## Command line

The app's "Update now" starts the installer with Inno Setup switches:
`/SILENT /SP- /NOCANCEL /NORESTART /CLOSEAPPLICATIONS /RESTARTAPP=1 /LOG="..."`. Copies
installed with the Inno build keep doing that, so `installer.nsi` understands it:

- `/SILENT`: only the progress window (all other pages are skipped), then it closes.
- `/VERYSILENT` or `/S`: no window at all.
- `/RESTARTAPP=1`: with `/SILENT` or `/S`, start the app again when done.
- `/LOG="file"`: append a short log.
- the other Inno switches are accepted and ignored. `/D=folder` (NSIS, last on the line)
  chooses the folder.

`bgeditor\app_updates.py` needs no change for this. winget passes `/S` to an NSIS installer
by itself.

## Before the first test

1. NSIS 3.11 or newer: the zip from https://nsis.sourceforge.io/Download (no administrator
   rights needed), unpacked to a folder of your choice; set `MAKENSIS` to its `makensis.exe`.
   Installing anything needs Joost's ok.
2. `.\build.ps1 -InstallerKind NSIS -Version 1.1.1-nsis1` for every test except 7 (a test
   version; nothing is published). The app's updater only takes plain versions (its asset
   and tag patterns refuse `-nsis1`), so test 7 needs `-Version 1.1.1`.
3. Test 7 needs a release the test copy can find. The copy that checks is the Inno-built
   1.1.0, and its repository is the constant `UPDATE_REPO` in `bgeditor\__init__.py`: build
   that 1.1.0 test copy with `UPDATE_REPO` pointing at a throwaway GitHub repository and
   publish the 1.1.1 test release there (tag `v1.1.1`, not a pre-release, the Setup and the
   signed manifest). **Never publish a test release to Optimey-CommV/background-editor:**
   every installed 1.1.x would be offered it.

## Tests (all on a test PC or VM, with a snapshot)

1. **Compile.** makensis gives no error and no warning that points at a real problem. The
   NSIS version check (`NSIS_PACKEDVERSION`) passes with 3.11 and stops an older makensis.
2. **Signatures.** `Get-AuthenticodeSignature` on the Setup and on the installed
   `uninstall.exe`: both Valid, signed by the Optimey certificate. The app's own
   `--verify-update` on the Setup passes (build.ps1 step 9 runs it).
3. **First install, interactive.** Licence, folder, both components ticked, progress,
   finish with "Launch". Check: the Start menu shortcut, the desktop shortcut, the menu verb
   on a .jpg and a .heic, Settings > Apps shows Background Editor with version and icon.
4. **Taskbar identity.** Pin the running app to the taskbar, close it, start it from the
   Start menu: one taskbar button, not two. Also read the property:
   `(New-Object -ComObject Shell.Application).NameSpace("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Background Editor").ParseName("Background Editor.lnk").ExtendedProperty("System.AppUserModel.ID")`
   gives `BackgroundEditor.App.1`.
5. **Reinstall with other choices.** Untick the menu verb: the verbs are gone, the desktop
   shortcut stays as chosen; the next reinstall offers the same choice; no folder page.
6. **Upgrade from the Inno build.** Install 1.1.0 with the Inno Setup installer (desktop
   icon off, menu verb on, a downloaded model), then run the NSIS Setup: no folder page, the
   desktop component unticked and the verb ticked, the Inno uninstall entry is gone, the
   models and settings are still there, the app starts and finds its model, a taskbar pin
   still works.
7. **Update from inside the app.** From an Inno-built 1.1.0, let "Update now" install the
   NSIS build (a test release with its signed manifest): progress window only, the app
   closes and starts again; the log next to the downloaded Setup shows the steps. Cancel and
   the close box stay disabled while it installs (NSIS disables them on the progress page;
   the app passes /NOCANCEL, which this Setup does not read). Repeat from the NSIS build
   itself.
8. **App running.** Start Setup while the app runs: it asks to close it; Retry after closing
   goes on; Cancel stops without changes.
9. **VC++ runtime.** On a PC without the runtime (or with an old one): the question, the
   download, Microsoft's installer with its UAC prompt, and the check afterwards. A failed
   download (no network) keeps Setup on the components page. A silent install only logs it.
10. **Uninstall.** Interactive: the models question (No keeps them, Yes removes
    `%LOCALAPPDATA%\BackgroundEditor`, `models` next to the exe and
    `HKCU\Software\BackgroundEditor`); the shortcuts, verbs and uninstall entry are gone; the
    app folder is gone when nothing else is in it. Silent (`uninstall.exe /S`): keeps the
    models.
11. **Old Background Remover.** With Background Remover 1.0 installed: Setup removes it and
    the app moves its models over on the first start.
12. **winget-style.** `Setup.exe /S` with exit code 0, and `uninstall.exe /S`. An NSIS
    uninstaller copies itself to `%TEMP%` and the first process exits at once with 0, so judge
    the uninstall by the uninstall entry disappearing (or run it with
    `_?=<install folder>`, which waits but cannot remove itself or the folder).
13. **Links and foreign files.** Before uninstalling with Yes: make
    `%LOCALAPPDATA%\BackgroundEditor\models` a junction (`mklink /J`) to a folder elsewhere with
    a marker file, put a junction `models` next to the exe the same way, and put a file of
    your own in `<app>\source` and `<app>\licenses`. After the uninstall: both markers and
    your own files are still there, the junctions themselves are gone, and the folders that
    held your files remain with only those files.
    Also: type an existing folder of your own on the folder page of a first install (one
    with its own `source` folder): Setup asks first, and after the install your `source`
    files are still there next to the app's. And with the app folder and
    `%LOCALAPPDATA%\BackgroundEditor` inside a OneDrive folder (Files On-Demand): an update
    still clears the old `_internal`, and uninstall with Yes still removes the models.
14. **ARM64.** On Windows 10 on ARM Setup refuses with the "on ARM: Windows 11" message; on
    Windows 11 on ARM it installs (if such a machine is at hand).
15. **Started from PowerShell 7.** Run the Setup from a `pwsh` window on a PC without the
    VC++ runtime: the downloaded vc_redist is still recognised as signed by Microsoft (the
    command loads Windows PowerShell's own Security module; without that, the module path
    inherited from PowerShell 7 broke `Get-AuthenticodeSignature`, also in installer.iss).
    Repeat with `TEMP` and `TMP` set to a folder whose name holds `'` and a typographic `’`
    before starting Setup: the download and the signature check still work (the path goes to
    PowerShell through the environment variable `BGEDITOR_VC_FILE`; installer.iss doubles all
    four kinds of single quote). Tested outside the installers on AI-MGMT on 2026-10-16.

When all pass: make NSIS the default of `build.ps1`, update README.md ("Publishing a
release", "Needs"), and keep `installer.iss` one more release for anyone who builds an older
version.

## Known points to check while testing

- `NSIS_PACKEDVERSION` packs 3.11 as `0x03011000` (NSIS's SConstruct: `0x%02i%03i%02i%i`);
  a self-built snapshot packs lower and is refused.
- `${AtLeastBuild}` comes from `WinVer.nsh` (NSIS 3.02 and newer).
- makensis is 32-bit, so `!uninstfinalize` runs the 32-bit Windows PowerShell; signing a
  `.tmp` copy of a PE file through 32-bit `cmd` and `powershell.exe` with
  `tools\sign-file.ps1` was tested on AI-MGMT on 2026-10-16 (Valid, Authenticode).
- The log is written as ANSI: characters outside the code page become `?`.
- Without build.ps1's file list (`/DInstalledFiles=`), makensis warns and the uninstaller
  falls back to removing `_internal`, `licenses` and `source` as a whole (still without
  following links). A release build always passes the list.
- The VC++ download command is about 770 characters after expansion, whatever the temp path
  (NSIS strings hold 1024).
- DeleteTreeNoLinks reads the reparse tag from `WIN32_FIND_DATAW.dwReserved0` (offset 36 in
  this 32-bit Setup) and compares it with `0x9000001A` after masking with `0xFFFF0FFF`;
  check on the first compile that makensis takes these hex values in `IntOp` and LogicLib as
  intended (test 13 covers it at run time).
- The taskbar identity is set with System plug-in COM calls (`IShellLinkW`,
  `IPersistFile`, `IPropertyStore`); Setup logs an HRESULT when that fails.
- Windows PowerShell does the VC++ download, because NSIS has no HTTPS download built in.

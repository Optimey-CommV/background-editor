; Background Editor - portrait-aware background removal
; Copyright (C) 2026 Optimey CommV
; SPDX-License-Identifier: GPL-3.0-or-later
;
; This program is free software: you can redistribute it and/or modify it under the terms
; of the GNU General Public License as published by the Free Software Foundation, either
; version 3 of the License, or (at your option) any later version.
;
; This program is distributed in the hope that it will be useful, but WITHOUT ANY
; WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
; PARTICULAR PURPOSE. See the GNU General Public License for more details.
;
; You should have received a copy of the GNU General Public License along with this
; program. If not, see <https://www.gnu.org/licenses/>.

; NSIS script for Background Editor: the successor of installer.iss (docs\INSTALLER-NSIS.md
; says why and what is still to test). Per-user install (no administrator rights needed).
; Built by build.ps1 -InstallerKind NSIS, which passes /DBuildDefines=<file> with the version,
; the VC++ runtime minimum and the command that signs the uninstaller.
;
; Command lines it understands besides the NSIS ones (/S, /D=dir):
;   /VERYSILENT            the same as /S: no window at all
;   /SILENT                only the progress window (what Inno Setup's /SILENT did); the app's
;                          "Update now" uses this, so copies installed with the Inno Setup
;                          build update to this one without a change
;   /RESTARTAPP=1          with /SILENT or /S: start Background Editor again when done
;   /LOG="file"            append a short log to that file
; Inno Setup switches the app passes along (/SP- /NOCANCEL /NORESTART /CLOSEAPPLICATIONS
; /SUPPRESSMSGBOXES) are accepted and need nothing here.

Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64

; NSIS_PACKEDVERSION packs 3.11 as 0x03011000. 3.11 fixed the insecure plug-in folder
; (CVE-2025-43715); 3.08 added !uninstfinalize, which signs the uninstaller.
!if ${NSIS_PACKEDVERSION} < 0x03011000
  !error "NSIS 3.11 or newer is needed"
!endif

!ifdef BuildDefines
  !include "${BuildDefines}"
!endif
!ifndef AppVersion
  ; build.ps1 defines it; otherwise use __version__ from bgeditor\__init__.py, the single
  ; source of the version.
  !searchparse /file "${__FILEDIR__}\bgeditor\__init__.py" '__version__ = "' AppVersion '"'
!endif
!ifndef AppVersionNumeric
  ; The four-part number of the version resource; build.ps1 works it out for 1.2.0-rc1 too.
  !define AppVersionNumeric "${AppVersion}.0"
!endif
; The oldest Microsoft Visual C++ runtime the bundled binaries work with (see installer.iss).
!ifndef VCRedistMajor
  !define VCRedistMajor 14
!endif
!ifndef VCRedistMinor
  !define VCRedistMinor 51
!endif

!define AppName "Background Editor"
!define AppExe "BackgroundEditor.exe"
!define AppPublisher "Optimey CommV"
!define AppCopyrightText "Copyright (C) 2026 Optimey CommV"
; Must match SetCurrentProcessExplicitAppUserModelID in app.py, so a pinned shortcut and
; the running window share one taskbar button.
!define AppUserModelID "BackgroundEditor.App.1"
; The AppId of installer.iss, without Inno's "_is1": the uninstall entry of this installer
; (and winget's ProductCode).
!define AppGuid "{80AB4259-3ACF-4A4E-BD21-DB72FA5BA4EB}"
!define UninstKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppGuid}"
; Background Editor 1.1.x, installed with the Inno Setup build: removed before installing.
!define InnoKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppGuid}_is1"
; The app was called Background Remover before 1.1; Setup removes it too.
!define OldAppKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{6D3F4B8E-2A71-4C1B-9E55-3B7C0F2A9D41}_is1"
; Microsoft's permalink for the latest supported Visual C++ v14 redistributable (x64).
!define VCRedistUrl "https://aka.ms/vc14/vc_redist.x64.exe"
!define VCKey "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
!define DataDir "$LOCALAPPDATA\BackgroundEditor"
!define StartMenuLink "$SMPROGRAMS\${AppName}\${AppName}.lnk"
!define DesktopLink "$DESKTOP\${AppName}.lnk"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

Name "${AppName}"
OutFile "dist\BackgroundEditor-Setup-${AppVersion}.exe"
InstallDir "$LOCALAPPDATA\Programs\${AppName}"
InstallDirRegKey HKCU "${UninstKey}" "InstallLocation"
RequestExecutionLevel user
ManifestDPIAware true
ShowInstDetails hide
ShowUninstDetails hide
BrandingText "${AppName} ${AppVersion}"

VIProductVersion "${AppVersionNumeric}"
VIFileVersion "${AppVersionNumeric}"
VIAddVersionKey "ProductName" "${AppName}"
VIAddVersionKey "CompanyName" "${AppPublisher}"
VIAddVersionKey "LegalCopyright" "${AppCopyrightText}. GPL-3.0."
VIAddVersionKey "FileDescription" "${AppName} Setup"
VIAddVersionKey "FileVersion" "${AppVersion}"
VIAddVersionKey "ProductVersion" "${AppVersion}"

!ifdef SignCommand
  ; makensis replaces %1 with the uninstaller it is about to pack; a failed signature stops
  ; the build.
  !uninstfinalize '${SignCommand}' = 0
!endif

Var UpdateMode   ; 1: /SILENT, only the progress window
Var RestartApp   ; 1: /RESTARTAPP=1
Var LogFile      ; /LOG= file, or empty
Var PrevDir      ; folder of an earlier install (this installer's or the Inno Setup build's)
Var PrevTasks    ; its chosen tasks: "desktopicon,contextmenu", or fewer
Var VCFound      ; what VCRuntimeOK found, for the messages

; ----------------------------------------------------------------------------- pages

!define MUI_ICON "assets\app.ico"
!define MUI_UNICON "assets\app.ico"
; The side image of the finish page (assets\build_installer_art.py renders it from the logo).
!define MUI_WELCOMEFINISHPAGE_BITMAP "assets\installer-side.bmp"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_NODESC

!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipInUpdateMode
!insertmacro MUI_PAGE_LICENSE "LICENSE"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirPage
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE DirLeave
!insertmacro MUI_PAGE_DIRECTORY
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipInUpdateMode
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ComponentsLeave
!define MUI_COMPONENTSPAGE_TEXT_TOP "Choose the extras you want."
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipInUpdateMode
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${AppName}"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------- shared macros

!macro Log Text
  Push `${Text}`
  Call LogLine
!macroend

; "Remove background" in the Explorer menu for one photo type (per user). Keep the list in
; ForEachPhotoType in line with INPUT_EXTENSIONS in bgeditor\imageio.py.
!macro WriteVerb Ext
  WriteRegStr HKCU "Software\Classes\SystemFileAssociations\${Ext}\shell\BackgroundEditor" "" "Remove background"
  WriteRegStr HKCU "Software\Classes\SystemFileAssociations\${Ext}\shell\BackgroundEditor" "Icon" '"$INSTDIR\${AppExe}",0'
  WriteRegStr HKCU "Software\Classes\SystemFileAssociations\${Ext}\shell\BackgroundEditor" "MultiSelectModel" "Player"
  WriteRegStr HKCU "Software\Classes\SystemFileAssociations\${Ext}\shell\BackgroundEditor\command" "" '"$INSTDIR\${AppExe}" "%1"'
!macroend
!macro DeleteVerb Ext
  DeleteRegKey HKCU "Software\Classes\SystemFileAssociations\${Ext}\shell\BackgroundEditor"
!macroend
!macro ForEachPhotoType Macro
  !insertmacro ${Macro} ".jpg"
  !insertmacro ${Macro} ".jpeg"
  !insertmacro ${Macro} ".jfif"
  !insertmacro ${Macro} ".png"
  !insertmacro ${Macro} ".webp"
  !insertmacro ${Macro} ".bmp"
  !insertmacro ${Macro} ".tif"
  !insertmacro ${Macro} ".tiff"
  !insertmacro ${Macro} ".heic"
  !insertmacro ${Macro} ".heif"
!macroend

; Tells Explorer that the menu verbs changed (Inno Setup: ChangesAssociations=yes).
!macro NotifyAssociations
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend

; Waits until BackgroundEditor.exe in $INSTDIR can be opened for writing, that is until the
; app has closed. $0 = 1 when it is closed, 0 when Setup must stop. When the app started this
; Setup (an update) or nobody can be asked, it waits up to 30 s, since the app quits right
; after it starts an update; otherwise it asks at once.
!macro WaitForAppClosedBody Waits
  StrCpy $1 0
  ${Do}
    ${IfNot} ${FileExists} "$INSTDIR\${AppExe}"
      ${Break}
    ${EndIf}
    ClearErrors
    FileOpen $2 "$INSTDIR\${AppExe}" a
    ${IfNot} ${Errors}
      FileClose $2
      ${Break}
    ${EndIf}
    ${If} $1 < ${Waits}
      Sleep 500
      IntOp $1 $1 + 1
      ${Continue}
    ${EndIf}
    ${If} ${Silent}
      StrCpy $0 0
      Return
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${AppName} is still running. Close it, then click Retry." IDRETRY +3
    StrCpy $0 0
    Return
    StrCpy $1 0
  ${Loop}
  StrCpy $0 1
!macroend

; Deletes the folder whose path is on the stack with everything in it, without ever following
; a junction or a symbolic link: a link is removed itself and its target is left alone.
; (RMDir /r follows them, so a models folder moved elsewhere with mklink /J would lose the
; files at the other end.) Read-only files are deleted too (Delete clears the attribute).
; A folder with a reparse point counts as a link, except a cloud placeholder (OneDrive Files
; On-Demand and other Cloud Files providers: tags 0x9000x01A), which is an ordinary folder.
; An unknown tag or a failed lookup counts as a link: then the folder stays rather than that
; something at the other end goes.
!macro DeleteTreeNoLinksFunc un
Function ${un}DeleteTreeNoLinks
  Exch $0  ; the folder
  Push $1  ; find handle
  Push $2  ; entry name
  Push $3  ; attributes
  Push $4
  Push $5  ; WIN32_FIND_DATAW buffer
  Push $6  ; FindFirstFileW handle
  Push $7  ; reparse tag
  System::Call 'kernel32::GetFileAttributesW(w r0) i .r3'
  ${If} $3 = -1  ; INVALID_FILE_ATTRIBUTES: not there
    Goto done
  ${EndIf}
  IntOp $4 $3 & 0x400  ; FILE_ATTRIBUTE_REPARSE_POINT
  ${If} $4 <> 0
    StrCpy $7 0
    System::Alloc 592  ; sizeof(WIN32_FIND_DATAW)
    Pop $5
    System::Call 'kernel32::FindFirstFileW(w r0, p r5) p .r6'
    ${If} $6 <> -1  ; INVALID_HANDLE_VALUE
      System::Call 'kernel32::FindClose(p r6)'
      IntOp $4 $5 + 36  ; dwReserved0 holds the reparse tag (this Setup is 32-bit)
      System::Call '*$4(i .r7)'
    ${EndIf}
    System::Free $5
    IntOp $7 $7 & 0xFFFF0FFF  ; IO_REPARSE_TAG_CLOUD_1..F differ only in 0x0000F000
    ${If} $7 <> 0x9000001A  ; not IO_REPARSE_TAG_CLOUD
      RMDir "$0"  ; a link to a folder: only the link goes
      Goto done
    ${EndIf}
  ${EndIf}
  ClearErrors
  FindFirst $1 $2 "$0\*.*"
  ${IfNot} ${Errors}
    ${DoWhile} $2 != ""
      ${If} $2 != "."
      ${AndIf} $2 != ".."
        System::Call 'kernel32::GetFileAttributesW(w "$0\$2") i .r3'
        ${If} $3 <> -1
          IntOp $4 $3 & 0x10  ; FILE_ATTRIBUTE_DIRECTORY (a folder link goes to the check above)
          ${If} $4 <> 0
            Push "$0\$2"
            Call ${un}DeleteTreeNoLinks
          ${Else}
            Delete "$0\$2"  ; a file, or a link to a file (the link itself)
          ${EndIf}
        ${EndIf}
      ${EndIf}
      FindNext $1 $2
    ${Loop}
    FindClose $1
  ${EndIf}
  RMDir "$0"
  done:
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
!macroend
!insertmacro DeleteTreeNoLinksFunc ""
!insertmacro DeleteTreeNoLinksFunc "un."

!macro DeleteTreeNoLinks Dir
  Push `${Dir}`
  Call DeleteTreeNoLinks
!macroend
!macro UnDeleteTreeNoLinks Dir
  Push `${Dir}`
  Call un.DeleteTreeNoLinks
!macroend

; ------------------------------------------------------------------------ sections

Section "-${AppName}" SecMain
  ${If} $UpdateMode = 1
    SetAutoClose true
  ${EndIf}
  ${If} ${Silent}
  ${OrIf} $UpdateMode = 1
    Call LogVCRuntime
  ${EndIf}

  DetailPrint "Waiting for ${AppName} to close..."
  Call WaitForAppClosed
  ${If} $0 = 0
    !insertmacro Log "${AppName} is still running in $INSTDIR; Setup stopped."
    Abort "${AppName} is still running."
  ${EndIf}
  Call RemoveOldBackgroundRemover
  Call RemoveInnoBuild

  SetOutPath "$INSTDIR"
  ; Files of the previous version that this build no longer has (renamed DLLs, Qt plugins,
  ; licence files, the previous source zip). A models folder in $INSTDIR and the settings
  ; are not touched. Only in an earlier install of this app: a folder typed on the folder
  ; page may have its own folders of these names. (The Inno build removed its own already.)
  ${If} ${FileExists} "$INSTDIR\${AppExe}"
  ${OrIf} ${FileExists} "$INSTDIR\uninstall.exe"
    !insertmacro DeleteTreeNoLinks "$INSTDIR\_internal"
    !insertmacro DeleteTreeNoLinks "$INSTDIR\licenses"
    !insertmacro DeleteTreeNoLinks "$INSTDIR\source"
  ${EndIf}
  ; The app folder: BackgroundEditor.exe, _internal, LICENSE, LICENSES.txt, licenses\ and
  ; source\ (the uninstaller removes exactly the files listed by build.ps1 from this
  ; folder). portable.conf would turn an installed copy into a portable one.
  File /r /x portable.conf "dist\BackgroundEditor\*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${AppName}"
  CreateShortcut "${StartMenuLink}" "$INSTDIR\${AppExe}"
  Push "${StartMenuLink}"
  Call SetShortcutAppId
SectionEnd

Section "Create a desktop shortcut" SecDesktop
  SetOutPath "$INSTDIR"
  CreateShortcut "${DesktopLink}" "$INSTDIR\${AppExe}"
  Push "${DesktopLink}"
  Call SetShortcutAppId
SectionEnd

; Short enough for the components list (a longer text is cut off there).
Section 'Add "Remove background" to the photo menu' SecContext
  !insertmacro ForEachPhotoType WriteVerb
SectionEnd

Section "-Finish"
  ; The tasks, kept in the uninstall entry so that a reinstall offers the same choice.
  StrCpy $0 ""
  ${If} ${SectionIsSelected} ${SecDesktop}
    StrCpy $0 "desktopicon"
  ${EndIf}
  ${If} ${SectionIsSelected} ${SecContext}
    ${If} $0 != ""
      StrCpy $0 "$0,"
    ${EndIf}
    StrCpy $0 "$0contextmenu"
  ${Else}
    ; Unticked on a reinstall: the verbs of the earlier install go.
    !insertmacro ForEachPhotoType DeleteVerb
  ${EndIf}
  !insertmacro NotifyAssociations

  WriteRegStr HKCU "${UninstKey}" "DisplayName" "${AppName}"
  WriteRegStr HKCU "${UninstKey}" "DisplayVersion" "${AppVersion}"
  WriteRegStr HKCU "${UninstKey}" "Publisher" "${AppPublisher}"
  WriteRegStr HKCU "${UninstKey}" "DisplayIcon" '"$INSTDIR\${AppExe}"'
  WriteRegStr HKCU "${UninstKey}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UninstKey}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UninstKey}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKCU "${UninstKey}" "Tasks" "$0"
  WriteRegDWORD HKCU "${UninstKey}" "NoModify" 1
  WriteRegDWORD HKCU "${UninstKey}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $1 $2 $3
  IntFmt $1 "0x%08X" $1
  WriteRegDWORD HKCU "${UninstKey}" "EstimatedSize" $1
  !insertmacro Log "Installed ${AppName} ${AppVersion} in $INSTDIR (tasks: $0)."
SectionEnd

; ------------------------------------------------------------------ installer start

Function .onInit
  ; The command line first: /VERYSILENT must be in force before the first message box.
  StrCpy $UpdateMode 0
  StrCpy $RestartApp 0
  StrCpy $LogFile ""
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/VERYSILENT" $R1
  ${IfNot} ${Errors}
    SetSilent silent
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/SILENT" $R1
  ${IfNot} ${Errors}
  ${AndIfNot} ${Silent}
    StrCpy $UpdateMode 1
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/RESTARTAPP=" $R1
  ${IfNot} ${Errors}
  ${AndIf} $R1 == "1"
    StrCpy $RestartApp 1
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/LOG=" $R1
  ${IfNot} ${Errors}
    ; Without the quotes the app puts around the path.
    StrCpy $R2 $R1 1
    ${If} $R2 == '"'
      StrCpy $R1 $R1 "" 1
    ${EndIf}
    StrCpy $R2 $R1 1 -1
    ${If} $R2 == '"'
      StrCpy $R1 $R1 -1
    ${EndIf}
    StrCpy $LogFile $R1
  ${EndIf}
  !insertmacro Log "${AppName} Setup ${AppVersion}: $CMDLINE"

  ; One Setup at a time.
  System::Call 'kernel32::CreateMutex(p 0, i 0, t "BackgroundEditorSetup") p .r0 ?e'
  Pop $1
  ${If} $1 = 183  ; ERROR_ALREADY_EXISTS
    !insertmacro Log "Another ${AppName} Setup is running; Setup stopped."
    MessageBox MB_OK|MB_ICONEXCLAMATION "${AppName} Setup is already running." /SD IDOK
    Abort
  ${EndIf}

  ${IfNot} ${RunningX64}
  ${OrIfNot} ${AtLeastWin10}
  ${OrIfNot} ${AtLeastBuild} 17763
    !insertmacro Log "Not 64-bit Windows 10 1809 or newer; Setup stopped."
    MessageBox MB_OK|MB_ICONSTOP "${AppName} needs 64-bit Windows 10 version 1809 or newer (on ARM: Windows 11)." /SD IDOK
    Abort
  ${EndIf}
  ; ${RunningX64} is also true on ARM64, where x64 programs run only from Windows 11 on
  ; (Inno Setup: ArchitecturesAllowed=x64compatible).
  ${If} ${IsNativeARM64}
  ${AndIfNot} ${AtLeastBuild} 22000
    !insertmacro Log "ARM64 older than Windows 11 (no x64 emulation); Setup stopped."
    MessageBox MB_OK|MB_ICONSTOP "${AppName} needs 64-bit Windows 10 version 1809 or newer (on ARM: Windows 11)." /SD IDOK
    Abort
  ${EndIf}

  ; An earlier install: this installer's, else the Inno Setup build's (1.1.x).
  ReadRegStr $PrevDir HKCU "${UninstKey}" "InstallLocation"
  ReadRegStr $PrevTasks HKCU "${UninstKey}" "Tasks"
  ${If} $PrevDir == ""
    ReadRegStr $PrevDir HKCU "${InnoKey}" "InstallLocation"
    ReadRegStr $PrevTasks HKCU "${InnoKey}" "Inno Setup: Selected Tasks"
    ; Inno Setup writes the folder with a trailing backslash.
    StrCpy $R2 $PrevDir 1 -1
    ${If} $R2 == "\"
      StrCpy $PrevDir $PrevDir -1
    ${EndIf}
    ; Install into the same folder, unless /D= chose another one.
    ${If} $PrevDir != ""
    ${AndIf} $INSTDIR == "$LOCALAPPDATA\Programs\${AppName}"
      StrCpy $INSTDIR $PrevDir
    ${EndIf}
  ${EndIf}
  ; A reinstall offers the tasks chosen last time; a first install offers both.
  ${If} $PrevDir != ""
    Push ",$PrevTasks,"
    Push ",desktopicon,"
    Call StrContains
    Pop $R1
    ${If} $R1 = 0
      !insertmacro UnselectSection ${SecDesktop}
    ${EndIf}
    Push ",$PrevTasks,"
    Push ",contextmenu,"
    Call StrContains
    Pop $R1
    ${If} $R1 = 0
      !insertmacro UnselectSection ${SecContext}
    ${EndIf}
  ${EndIf}
FunctionEnd

Function SkipInUpdateMode
  ${If} $UpdateMode = 1
    Abort
  ${EndIf}
FunctionEnd

; The folder is asked only on a first install (Inno Setup: DisableDirPage=auto).
Function SkipDirPage
  ${If} $UpdateMode = 1
  ${OrIf} $PrevDir != ""
    Abort
  ${EndIf}
FunctionEnd

; A folder that already exists and is not an install of this app gets a question first
; (Inno Setup: DirExistsWarning=auto).
Function DirLeave
  ${If} ${FileExists} "$INSTDIR\*.*"
  ${AndIfNot} ${FileExists} "$INSTDIR\${AppExe}"
    MessageBox MB_YESNO|MB_ICONEXCLAMATION "The folder$\n$\n$INSTDIR$\n$\nalready exists. Install ${AppName} into it anyway?" /SD IDYES IDYES dirok
    Abort
    dirok:
  ${EndIf}
FunctionEnd

; The last page before the install: the place for the VC++ runtime (Inno: the Ready page).
Function ComponentsLeave
  Call EnsureVCRuntime
  Pop $0
  ${If} $0 = 0
    Abort
  ${EndIf}
FunctionEnd

Function LaunchApp
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\${AppExe}"'
FunctionEnd

; Update now in the app runs this Setup with /SILENT ... /RESTARTAPP=1 and closes itself;
; start it again once the new version is installed. Other installs do not start it here.
Function .onInstSuccess
  ${If} $RestartApp = 1
    ${If} ${Silent}
    ${OrIf} $UpdateMode = 1
      SetOutPath "$INSTDIR"
      Exec '"$INSTDIR\${AppExe}"'
      !insertmacro Log "Started ${AppName} again."
    ${EndIf}
  ${EndIf}
FunctionEnd

Function .onInstFailed
  !insertmacro Log "Setup failed."
FunctionEnd

Function WaitForAppClosed
  ${If} ${Silent}
  ${OrIf} $UpdateMode = 1
    !insertmacro WaitForAppClosedBody 60
  ${Else}
    !insertmacro WaitForAppClosedBody 0
  ${EndIf}
FunctionEnd

; Appends one line to the /LOG= file and shows it in the details.
Function LogLine
  Exch $R9
  Push $R8
  DetailPrint "$R9"
  ${If} $LogFile != ""
    ClearErrors
    FileOpen $R8 "$LogFile" a
    ${IfNot} ${Errors}
      FileSeek $R8 0 END
      FileWrite $R8 "$R9$\r$\n"
      FileClose $R8
    ${EndIf}
  ${EndIf}
  Pop $R8
  Pop $R9
FunctionEnd

; Push haystack, push needle, call; pops 1 when the haystack contains the needle, else 0.
Function StrContains
  Exch $R1  ; needle
  Exch
  Exch $R0  ; haystack
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  StrLen $R2 $R1
  StrLen $R5 $R0
  StrCpy $R3 0
  StrCpy $R4 0
  ${DoWhile} $R3 <= $R5
    StrCpy $R4 $R0 $R2 $R3
    ${If} $R4 == $R1
      StrCpy $R4 1
      ${Break}
    ${EndIf}
    StrCpy $R4 0
    IntOp $R3 $R3 + 1
  ${Loop}
  StrCpy $R0 $R4
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  ; The stack holds the old $R1 and, on top, the old $R0: restore both, leave the result.
  Exch $R0
  Exch
  Pop $R1
FunctionEnd

; ----------------------------------------------------------- the taskbar identity

; Sets System.AppUserModel.ID on the shortcut whose path is on the stack (Inno Setup:
; AppUserModelID), through IShellLink, IPersistFile and IPropertyStore.
Function SetShortcutAppId
  Exch $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  ; CLSID_ShellLink, IID_IShellLinkW
  System::Call 'ole32::CoCreateInstance(g "{00021401-0000-0000-C000-000000000046}", p 0, i 1, g "{000214F9-0000-0000-C000-000000000046}", *p .r1) i .r2'
  ${If} $2 = 0
    ; IID_IPersistFile
    System::Call '$1->0(g "{0000010B-0000-0000-C000-000000000046}", *p .r3) i .r2'
    ${If} $2 = 0
      System::Call '$3->5(w r0, i 2) i .r2'  ; Load(path, STGM_READWRITE)
      ${If} $2 = 0
        ; IID_IPropertyStore
        System::Call '$1->0(g "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}", *p .r4) i .r2'
        ${If} $2 = 0
          ; PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
          System::Call '*(g "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", i 5) p .r5'
          System::Call '*(&w64 "${AppUserModelID}") p .r7'
          ; PROPVARIANT: vt = VT_LPWSTR (31), three reserved words, the string pointer
          System::Call '*(&i2 31, &i2 0, &i2 0, &i2 0, p r7, p 0) p .r6'
          System::Call '$4->6(p r5, p r6) i .r2'  ; SetValue
          ${If} $2 = 0
            System::Call '$4->7() i .r2'  ; Commit
          ${EndIf}
          System::Free $6
          System::Free $7
          System::Free $5
          System::Call '$4->2()'  ; Release
          ${If} $2 = 0
            System::Call '$3->6(p 0, i 1) i .r2'  ; Save(NULL, TRUE)
          ${EndIf}
        ${EndIf}
      ${EndIf}
      System::Call '$3->2()'
    ${EndIf}
    System::Call '$1->2()'
  ${EndIf}
  ${If} $2 <> 0
    !insertmacro Log "Could not set the taskbar identity of $0 (HRESULT $2)."
  ${EndIf}
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; ------------------------------------------------------------ Visual C++ runtime

; $7 = 1 when $5.$6 is at least the minimum, else 0.
Function AtLeastVC
  StrCpy $7 0
  ${If} $5 > ${VCRedistMajor}
    StrCpy $7 1
  ${ElseIf} $5 = ${VCRedistMajor}
    ${If} $6 >= ${VCRedistMinor}
      StrCpy $7 1
    ${EndIf}
  ${EndIf}
FunctionEnd

; Keeps in $R1.$R2.$R3 the newer of what it holds and what the current registry view
; registers; $R0 = 1 once one is found.
Function ReadVCRegistryView
  ClearErrors
  ReadRegDWORD $0 HKLM "${VCKey}" "Installed"
  ReadRegDWORD $1 HKLM "${VCKey}" "Major"
  ReadRegDWORD $2 HKLM "${VCKey}" "Minor"
  ${If} ${Errors}
    Return
  ${EndIf}
  ${If} $0 <> 1
    Return
  ${EndIf}
  ClearErrors
  ReadRegDWORD $3 HKLM "${VCKey}" "Bld"
  ${If} ${Errors}
    StrCpy $3 0
  ${EndIf}
  StrCpy $4 0
  ${If} $R0 = 0
    StrCpy $4 1
  ${ElseIf} $1 > $R1
    StrCpy $4 1
  ${ElseIf} $1 = $R1
    ${If} $2 > $R2
      StrCpy $4 1
    ${EndIf}
  ${EndIf}
  ${If} $4 = 1
    StrCpy $R0 1
    StrCpy $R1 $1
    StrCpy $R2 $2
    StrCpy $R3 $3
  ${EndIf}
FunctionEnd

!macro CheckVCDll Dll
  ${If} $8 = 1
    ClearErrors
    GetDLLVersion "$WINDIR\System32\${Dll}" $5 $6
    ${If} ${Errors}
      StrCpy $VCFound "$VCFound, but ${Dll} is missing"
      StrCpy $8 0
    ${Else}
      IntOp $6 $5 & 0xFFFF
      IntOp $5 $5 >> 16
      Call AtLeastVC
      ${If} $7 = 0
        StrCpy $VCFound "$VCFound, but ${Dll} is $5.$6"
        StrCpy $8 0
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; Pushes 1 when the registered runtime and the DLLs the app loads from System32 are new
; enough, else 0. $VCFound describes what is there, for the messages.
Function VCRuntimeOK
  ; vc_redist registers itself in the 64-bit or the 32-bit view, depending on its version;
  ; an older entry can linger in the other one, so take the newest.
  StrCpy $R0 0
  StrCpy $R1 0
  StrCpy $R2 0
  StrCpy $R3 0
  SetRegView 64
  Call ReadVCRegistryView
  SetRegView 32
  Call ReadVCRegistryView
  SetRegView default
  ${If} $R0 = 0
    StrCpy $VCFound "not installed"
    Push 0
    Return
  ${EndIf}
  StrCpy $VCFound "$R1.$R2.$R3"
  StrCpy $5 $R1
  StrCpy $6 $R2
  Call AtLeastVC
  ${If} $7 = 0
    Push 0
    Return
  ${EndIf}
  ; The registry entry alone is not proof: check the DLLs themselves, in the 64-bit System32
  ; (spelled out, with the redirection of this 32-bit Setup switched off).
  StrCpy $8 1
  ${DisableX64FSRedirection}
  !insertmacro CheckVCDll "vcruntime140.dll"
  !insertmacro CheckVCDll "vcruntime140_1.dll"
  !insertmacro CheckVCDll "msvcp140.dll"
  !insertmacro CheckVCDll "msvcp140_1.dll"
  !insertmacro CheckVCDll "msvcp140_2.dll"
  ${EnableX64FSRedirection}
  Push $8
FunctionEnd

; Silent and update installs only log a missing runtime; the app needs it to start.
Function LogVCRuntime
  Call VCRuntimeOK
  Pop $0
  ${If} $0 = 0
    !insertmacro Log "VC++ runtime ${VCRedistMajor}.${VCRedistMinor} or newer needed; found: $VCFound. Not installed by a silent Setup."
  ${EndIf}
FunctionEnd

; Offers to download and run Microsoft's vc_redist. Pushes 0 to stay on the page (Install
; offers the download again, Cancel stops), 1 to go on.
Function EnsureVCRuntime
  Call VCRuntimeOK
  Pop $0
  ${If} $0 = 1
    Push 1
    Return
  ${EndIf}
  !insertmacro Log "VC++ runtime ${VCRedistMajor}.${VCRedistMinor} or newer needed; found: $VCFound"
  MessageBox MB_YESNO|MB_ICONQUESTION "${AppName} needs the Microsoft Visual C++ Redistributable (x64), version ${VCRedistMajor}.${VCRedistMinor} or newer.$\n$\nFound: $VCFound.$\n$\nDownload Microsoft's installer (about 20 MB) and start it now? It asks for administrator permission itself.$\n$\nChoose No to install it later from:$\n${VCRedistUrl}" /SD IDNO IDYES download
  MessageBox MB_OK|MB_ICONINFORMATION "${AppName} will not start until the Microsoft Visual C++ Redistributable (x64) ${VCRedistMajor}.${VCRedistMinor} or newer is installed. You can install it later from:$\n${VCRedistUrl}" /SD IDOK
  Push 1
  Return

  download:
  InitPluginsDir
  StrCpy $1 "$PLUGINSDIR\vc_redist.x64.exe"
  ; The path reaches PowerShell through the environment, never inside its source: a user
  ; folder with an apostrophe (also a typographic one, which PowerShell treats as a quote too)
  ; cannot break the command.
  System::Call 'kernel32::SetEnvironmentVariableW(w "BGEDITOR_VC_FILE", w r1) i .r2'
  ; Windows PowerShell downloads (through the system proxy, with the logged-on user's
  ; credentials, as Inno Setup's WinINet download did) and then checks Microsoft's
  ; signature: exit 0 signed by Microsoft, 2 download failed, 3 not signed by Microsoft.
  Banner::show "Downloading the Visual C++ Redistributable..."
  nsExec::Exec `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$f = $$env:BGEDITOR_VC_FILE; Import-Module ($$PSHOME + '\Modules\Microsoft.PowerShell.Security'); $$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; try { [Net.WebRequest]::DefaultWebProxy.Credentials = [Net.CredentialCache]::DefaultCredentials } catch {}; try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri '${VCRedistUrl}' -OutFile $$f } catch { exit 2 }; $$s = Get-AuthenticodeSignature -LiteralPath $$f; if ($$s.Status -eq 'Valid' -and $$s.SignerCertificate.Subject -match 'O=Microsoft Corporation') { exit 0 } else { exit 3 }"`
  Pop $0
  Banner::destroy
  !insertmacro Log "VC++ runtime download and signature check: $0"
  ${If} $0 == "3"
    MessageBox MB_OK|MB_ICONSTOP "The downloaded file is not signed by Microsoft, so Setup did not start it. Please install the Visual C++ Redistributable yourself from:$\n${VCRedistUrl}"
    Delete "$1"
    Push 1
    Return
  ${ElseIf} $0 != "0"
    MessageBox MB_OK|MB_ICONSTOP "The download of the Visual C++ Redistributable failed. Check the internet connection and click Install to try again, or install it later from:$\n${VCRedistUrl}"
    Push 0
    Return
  ${EndIf}
  ; ShellExecute, not CreateProcess: the redistributable asks for elevation (UAC) itself.
  ExecShellWait "open" "$1" "/install /passive /norestart"
  Call VCRuntimeOK
  Pop $0
  ${If} $0 = 1
    Push 1
    Return
  ${EndIf}
  !insertmacro Log "VC++ runtime still missing after vc_redist; found: $VCFound"
  MessageBox MB_YESNO|MB_ICONSTOP "The Visual C++ Redistributable ${VCRedistMajor}.${VCRedistMinor} or newer is still not installed (found: $VCFound).$\n$\n${AppName} will not start without it. Continue installing anyway? You can install it later from:$\n${VCRedistUrl}" IDYES keepgoing
  Push 0
  Return
  keepgoing:
  Push 1
FunctionEnd

; ----------------------------------------------- earlier installs with Inno Setup

; Strips the quotes Inno Setup puts around its UninstallString ($R7).
Function StripQuotesR7
  StrCpy $R9 $R7 1
  ${If} $R9 == '"'
    StrCpy $R7 $R7 "" 1
  ${EndIf}
  StrCpy $R9 $R7 1 -1
  ${If} $R9 == '"'
    StrCpy $R7 $R7 -1
  ${EndIf}
FunctionEnd

; $R9 = 1 while the old Background Remover's uninstall entry is in the view $R6 names.
Function OldAppKeyExists
  StrCpy $R9 ""
  ${If} $R6 == "HKCU"
    ReadRegStr $R9 HKCU "${OldAppKey}" "UninstallString"
  ${Else}
    ${If} $R6 == "HKLM64"
      SetRegView 64
    ${Else}
      SetRegView 32
    ${EndIf}
    ReadRegStr $R9 HKLM "${OldAppKey}" "UninstallString"
    SetRegView default
  ${EndIf}
  ${If} $R9 != ""
    StrCpy $R9 1
  ${Else}
    StrCpy $R9 0
  ${EndIf}
FunctionEnd

; Removes the old Background Remover silently before the files are installed. Its silent
; uninstall keeps the downloaded models and settings; Background Editor moves them over on
; its first start.
Function RemoveOldBackgroundRemover
  StrCpy $R6 "HKCU"
  ReadRegStr $R7 HKCU "${OldAppKey}" "UninstallString"
  ${If} $R7 == ""
    StrCpy $R6 "HKLM64"
    SetRegView 64
    ReadRegStr $R7 HKLM "${OldAppKey}" "UninstallString"
    ${If} $R7 == ""
      StrCpy $R6 "HKLM32"
      SetRegView 32
      ReadRegStr $R7 HKLM "${OldAppKey}" "UninstallString"
    ${EndIf}
    SetRegView default
  ${EndIf}
  ${If} $R7 == ""
    Return
  ${EndIf}
  Call StripQuotesR7
  !insertmacro Log "Removing the old Background Remover: $R7"
  ${IfNot} ${FileExists} "$R7"
    !insertmacro Log "Its uninstaller is gone; removing the stale uninstall entry."
    ${If} $R6 == "HKCU"
      DeleteRegKey HKCU "${OldAppKey}"
    ${Else}
      ; Needs administrator rights; without them the entry stays, as with Inno Setup.
      ${If} $R6 == "HKLM64"
        SetRegView 64
      ${Else}
        SetRegView 32
      ${EndIf}
      DeleteRegKey HKLM "${OldAppKey}"
      SetRegView default
    ${EndIf}
    Return
  ${EndIf}
  ClearErrors
  ExecWait '"$R7" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART' $R8
  ${If} ${Errors}
    StrCpy $R8 "-1"
  ${EndIf}
  ${If} $R8 != "0"
    !insertmacro Log "The old Background Remover's uninstaller ended with code $R8."
    MessageBox MB_OK|MB_ICONSTOP "Setup could not remove the old Background Remover (code $R8). Remove it via Settings > Apps, then run Setup again." /SD IDOK
    Abort "Setup could not remove the old Background Remover."
  ${EndIf}
  ; The uninstaller finishes in a second process; wait until it has removed its entry.
  StrCpy $R5 0
  ${Do}
    Call OldAppKeyExists
    ${If} $R9 = 0
    ${OrIf} $R5 >= 60
      ${Break}
    ${EndIf}
    Sleep 500
    IntOp $R5 $R5 + 1
  ${Loop}
FunctionEnd

; Removes Background Editor 1.1.x, installed with the Inno Setup build, before the files are
; installed (usually into the same folder). Its silent uninstall keeps the models and the
; settings. Setup waits until that uninstaller has really finished (its entry and its
; unins000.exe are gone), so it cannot delete files this Setup has just written.
Function RemoveInnoBuild
  ReadRegStr $R7 HKCU "${InnoKey}" "UninstallString"
  ${If} $R7 == ""
    Return
  ${EndIf}
  Call StripQuotesR7
  !insertmacro Log "Removing ${AppName} installed with the Inno Setup build: $R7"
  ${IfNot} ${FileExists} "$R7"
    !insertmacro Log "Its uninstaller is gone; removing the stale uninstall entry."
    DeleteRegKey HKCU "${InnoKey}"
    Return
  ${EndIf}
  ClearErrors
  ExecWait '"$R7" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART' $R8
  ${If} ${Errors}
    StrCpy $R8 "-1"
  ${EndIf}
  ${If} $R8 == "0"
    StrCpy $R5 0
    ${Do}
      ReadRegStr $R9 HKCU "${InnoKey}" "UninstallString"
      ${If} $R9 == ""
      ${AndIfNot} ${FileExists} "$R7"
        ; Inno Setup removes the empty app folder right after; give it that moment.
        Sleep 1000
        Return
      ${EndIf}
      ${If} $R5 >= 120
        ${Break}
      ${EndIf}
      Sleep 500
      IntOp $R5 $R5 + 1
    ${Loop}
    StrCpy $R8 "timeout"
  ${EndIf}
  !insertmacro Log "The previous ${AppName}'s uninstaller did not finish ($R8)."
  MessageBox MB_OK|MB_ICONSTOP "Setup could not remove the previous version of ${AppName} ($R8). Remove it via Settings > Apps, then run Setup again." /SD IDOK
  Abort "Setup could not remove the previous version."
FunctionEnd

; ------------------------------------------------------------------------- uninstall

Function un.WaitForAppClosed
  ${If} ${Silent}
    !insertmacro WaitForAppClosedBody 60
  ${Else}
    !insertmacro WaitForAppClosedBody 0
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.WaitForAppClosed
  ${If} $0 = 0
    Abort "${AppName} is still running."
  ${EndIf}

  ; Exactly the files Setup installed, like Inno Setup's uninstall log: build.ps1 lists every
  ; file of the app folder (Delete) and every subfolder, deepest first (RMDir without /r, so a
  ; folder that holds anything else stays, with that content).
!ifdef InstalledFiles
  !include "${InstalledFiles}"
!else
  !warning "No InstalledFiles list (build.ps1 makes it): the uninstaller removes the three app subfolders as a whole"
  Delete "$INSTDIR\${AppExe}"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\LICENSES.txt"
  !insertmacro UnDeleteTreeNoLinks "$INSTDIR\_internal"
  !insertmacro UnDeleteTreeNoLinks "$INSTDIR\licenses"
  !insertmacro UnDeleteTreeNoLinks "$INSTDIR\source"
!endif
  Delete "$INSTDIR\uninstall.exe"

  Delete "${StartMenuLink}"
  RMDir "$SMPROGRAMS\${AppName}"
  Delete "${DesktopLink}"
  !insertmacro ForEachPhotoType DeleteVerb
  !insertmacro NotifyAssociations
  DeleteRegKey HKCU "${UninstKey}"

  ; The downloaded AI models and the settings stay unless the user asks; a silent uninstall
  ; keeps them. Models copied by hand next to the exe (for offline use) count too: Setup did
  ; not install them, so they would otherwise keep the app folder.
  ${If} ${FileExists} "${DataDir}\*.*"
  ${OrIf} ${FileExists} "$INSTDIR\models\*.*"
    ; GetSize writes its result to the status line; keep that out of sight.
    SetDetailsPrint none
    StrCpy $1 0
    ${If} ${FileExists} "${DataDir}\models\*.*"
      ${un.GetSize} "${DataDir}\models" "/S=0M" $1 $2 $3
    ${EndIf}
    StrCpy $4 0
    ${If} ${FileExists} "$INSTDIR\models\*.*"
      ${un.GetSize} "$INSTDIR\models" "/S=0M" $4 $2 $3
    ${EndIf}
    IntOp $1 $1 + $4
    SetDetailsPrint lastused
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete the AI models ($1 MB) and settings?" /SD IDNO IDNO keepdata
      ; Never through a link: a models folder moved with mklink /J keeps its target.
      !insertmacro UnDeleteTreeNoLinks "${DataDir}"
      !insertmacro UnDeleteTreeNoLinks "$INSTDIR\models"
      DeleteRegKey HKCU "Software\BackgroundEditor"
    keepdata:
  ${EndIf}
  RMDir "$INSTDIR"
SectionEnd

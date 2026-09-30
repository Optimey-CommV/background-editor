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

; Inno Setup script for Background Editor.
; Per-user install (no administrator rights needed). Built by build.ps1.

#ifndef AppVersion
  ; build.ps1 passes /DAppVersion; otherwise use __version__ from bgeditor\__init__.py,
  ; the single source of the version.
  #define VersionFile FileOpen(AddBackslash(SourcePath) + "bgeditor\__init__.py")
  #if !VersionFile
    #error Cannot open bgeditor\__init__.py to read the version
  #endif
  #define AppVersion ""
  #sub ReadVersionLine
    #define VersionLine = FileRead(VersionFile)
    #if Pos("__version__", VersionLine) == 1 && Pos('"', VersionLine) > 0
      #define public AppVersion = Copy(VersionLine, Pos('"', VersionLine) + 1, RPos('"', VersionLine) - Pos('"', VersionLine) - 1)
    #endif
  #endsub
  #for {0; AppVersion == "" && !FileEof(VersionFile); 0} ReadVersionLine
  #expr FileClose(VersionFile)
  #if AppVersion == ""
    #error No __version__ = "..." found in bgeditor\__init__.py
  #endif
#endif

; The oldest Microsoft Visual C++ runtime the bundled binaries work with. build.ps1 works it
; out from the app folder (the newest MSVC linker version among the binaries that import the
; runtime) and passes it; the defaults are the value measured for 1.1.0 (Pillow 12.3.0 is
; linked with MSVC 14.51).
#ifndef VCRedistMajor
  #define VCRedistMajor 14
#endif
#ifndef VCRedistMinor
  #define VCRedistMinor 51
#endif

; "public" is needed: after the #for above, ISPP no longer makes new definitions global,
; and the #sub further down could not see them.
#define public AppName "Background Editor"
#define public AppExe "BackgroundEditor.exe"
#define public AppPublisher "Optimey CommV"
#define public AppCopyrightText "Copyright (C) 2026 Optimey CommV"
; Must match SetCurrentProcessExplicitAppUserModelID in app.py, so a pinned shortcut and
; the running window share one taskbar button.
#define public AppUserModelID "BackgroundEditor.App.1"
; The app was called Background Remover before 1.1 and used this AppId; Setup removes it.
#define public OldAppId "{6D3F4B8E-2A71-4C1B-9E55-3B7C0F2A9D41}"
; Microsoft's permalink for the latest supported Visual C++ v14 redistributable (x64).
; (aka.ms/vs/17/release/vc_redist.x64.exe serves the Visual Studio 2022 line, 14.44,
; which is older than what the bundled binaries need.)
#define public VCRedistUrl "https://aka.ms/vc14/vc_redist.x64.exe"

; Photo types that get "Remove background" in the Explorer menu; keep in line with
; INPUT_EXTENSIONS in bgeditor\imageio.py.
#dim public VerbExts[10] {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
#define public VerbIndex 0

[Setup]
AppId={{80AB4259-3ACF-4A4E-BD21-DB72FA5BA4EB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright={#AppCopyrightText}
VersionInfoCompany={#AppPublisher}
VersionInfoCopyright={#AppCopyrightText}. GPL-3.0.
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductTextVersion={#AppVersion}
; The source is GPL v3 or later; the program as distributed includes PyQt6 (GPL v3 only) and
; is therefore conveyed under the GNU GPL v3 (LICENSES.txt). Setup shows the licence.
LicenseFile=LICENSE
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=dist
OutputBaseFilename=BackgroundEditor-Setup-{#AppVersion}
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ChangesAssociations=yes
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.ContextMenu=Add "Remove background" to the right-click menu of photos
english.RemoveModels=Also delete the AI models (%1 MB) and settings?
; The messages below are English in every language.
VCRuntimeNeeded=Background Editor needs the Microsoft Visual C++ Redistributable (x64), version %1 or newer.%n%nFound: %2.%n%nDownload Microsoft's installer (about 20 MB) and start it now? It asks for administrator permission itself.%n%nChoose No to install it later from:%n%3
VCRuntimeNotFound=not installed
VCRuntimeNotSigned=The downloaded file is not signed by Microsoft, so Setup did not start it. Please install the Visual C++ Redistributable yourself from:%n%1
VCRuntimeStillMissing=The Visual C++ Redistributable %1 or newer is still not installed (Microsoft's installer ended with code %2).%n%nBackground Editor will not start without it. Continue installing anyway? You can install it later from:%n%3
VCRuntimeLater=Background Editor will not start until the Microsoft Visual C++ Redistributable (x64) %1 or newer is installed. You can install it later from:%n%2
OldVersionFailed=Setup could not remove the old Background Remover (code %1). Remove it via Settings > Apps, then run Setup again.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "contextmenu"; Description: "{cm:ContextMenu}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Files of the previous version that this build no longer has (renamed DLLs, Qt plugins,
; licence files, the previous source zip). A models folder in {app} and the settings are
; not touched.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"
Type: filesandordirs; Name: "{app}\source"

[Files]
; The app folder: BackgroundEditor.exe, _internal, LICENSE, LICENSES.txt, licenses\ and
; source\ (the source zip of this version). portable.conf would turn an installed copy into
; a portable one, so it is never installed.
Source: "dist\BackgroundEditor\*"; DestDir: "{app}"; Excludes: "\portable.conf"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Registry]
; "Remove background" in the Explorer menu for each photo type (per user). When the task is
; unticked on a reinstall, the verbs of the earlier install are removed.
#sub EmitVerb
  #define VerbKey "Software\Classes\SystemFileAssociations\" + VerbExts[VerbIndex] + "\shell\BackgroundEditor"
Root: HKCU; Subkey: "{#VerbKey}"; ValueType: string; ValueName: ""; ValueData: "Remove background"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "{#VerbKey}"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "{#VerbKey}"; ValueType: string; ValueName: "MultiSelectModel"; ValueData: "Player"; Tasks: contextmenu
Root: HKCU; Subkey: "{#VerbKey}\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu
Root: HKCU; Subkey: "{#VerbKey}"; ValueType: none; Flags: deletekey; Tasks: not contextmenu
#endsub
#for {VerbIndex = 0; VerbIndex < DimOf(VerbExts); VerbIndex++} EmitVerb

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; Update now in the app runs this Setup with /SILENT ... /RESTARTAPP=1 and closes itself;
; start it again once the new version is installed. Other silent installs do not start it.
Filename: "{app}\{#AppExe}"; Flags: nowait skipifnotsilent; Check: RestartAfterUpdate

[Code]
const
  VCKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  VCMajor = {#VCRedistMajor};
  VCMinor = {#VCRedistMinor};
  VCUrl = '{#VCRedistUrl}';
  OldUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#OldAppId}_is1';

var
  DownloadPage: TDownloadWizardPage;

{ ---------------------------------------------------------- Visual C++ runtime }

function AtLeast(Major, Minor: Cardinal): Boolean;
begin
  Result := (Major > VCMajor) or ((Major = VCMajor) and (Minor >= VCMinor));
end;

{ Highest Major.Minor.Bld registered in one registry view; False when none is installed. }
function RegisteredVC(RootKey: Integer; var Major, Minor, Bld: Cardinal): Boolean;
var
  Installed: Cardinal;
begin
  Result := False;
  if not RegQueryDWordValue(RootKey, VCKey, 'Installed', Installed) then
    exit;
  if Installed <> 1 then
    exit;
  if not RegQueryDWordValue(RootKey, VCKey, 'Major', Major) then
    exit;
  if not RegQueryDWordValue(RootKey, VCKey, 'Minor', Minor) then
    exit;
  if not RegQueryDWordValue(RootKey, VCKey, 'Bld', Bld) then
    Bld := 0;
  Result := True;
end;

{ True when the registered runtime and the DLLs the app loads from System32 are new enough.
  Found describes what is there, for the message. }
function VCRuntimeOK(var Found: String): Boolean;
var
  Major, Minor, Bld, Major32, Minor32, Bld32: Cardinal;
  Registered, Registered32: Boolean;
  Dlls: TArrayOfString;
  I: Integer;
  MS, LS: Cardinal;
  Path: String;
begin
  { vc_redist registers itself in the 64-bit or the 32-bit view, depending on its
    version; an older entry can linger in the other one, so take the newest. }
  Registered := RegisteredVC(HKLM64, Major, Minor, Bld);
  Registered32 := RegisteredVC(HKLM32, Major32, Minor32, Bld32);
  if Registered32 and (not Registered or (Major32 > Major) or ((Major32 = Major) and (Minor32 > Minor))) then
  begin
    Major := Major32;
    Minor := Minor32;
    Bld := Bld32;
    Registered := True;
  end;
  if not Registered then
  begin
    Found := CustomMessage('VCRuntimeNotFound');
    Result := False;
    exit;
  end;
  Found := Format('%d.%d.%d', [Major, Minor, Bld]);
  Result := AtLeast(Major, Minor);
  if not Result then
    exit;
  { The registry entry alone is not proof: check the DLLs themselves. }
  SetArrayLength(Dlls, 5);
  Dlls[0] := 'vcruntime140.dll';
  Dlls[1] := 'vcruntime140_1.dll';
  Dlls[2] := 'msvcp140.dll';
  Dlls[3] := 'msvcp140_1.dll';
  Dlls[4] := 'msvcp140_2.dll';
  for I := 0 to GetArrayLength(Dlls) - 1 do
  begin
    Path := ExpandConstant('{sys}\') + Dlls[I];
    if not GetVersionNumbers(Path, MS, LS) then
    begin
      Found := Found + ', but ' + Dlls[I] + ' is missing';
      Result := False;
      exit;
    end;
    if not AtLeast(MS shr 16, MS and $FFFF) then
    begin
      Found := Found + Format(', but %s is %d.%d', [Dlls[I], MS shr 16, MS and $FFFF]);
      Result := False;
      exit;
    end;
  end;
end;

function MinText: String;
begin
  Result := Format('%d.%d', [VCMajor, VCMinor]);
end;

{ Microsoft's signature on the downloaded installer, checked with PowerShell. }
function SignedByMicrosoft(const FileName: String): Boolean;
var
  Command: String;
  ResultCode: Integer;
  Quoted: String;
begin
  Quoted := FileName;
  StringChangeEx(Quoted, '''', '''''', True);
  Command := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$s = Get-AuthenticodeSignature -LiteralPath ''' + Quoted + '''; ' +
    'if ($s.Status -eq ''Valid'' -and $s.SignerCertificate.Subject -match ''O=Microsoft Corporation'') { exit 0 } else { exit 1 }"';
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Command, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

{ Offers to download and run Microsoft's vc_redist. False aborts the page change. }
function EnsureVCRuntime: Boolean;
var
  Found, Installer: String;
  ResultCode: Integer;
  Downloaded: Boolean;
begin
  Result := True;
  if VCRuntimeOK(Found) then
    exit;
  Log('VC++ runtime ' + MinText + ' or newer needed; found: ' + Found);
  if WizardSilent then
  begin
    Log('Silent install: not installing the VC++ runtime; the app needs it to start.');
    exit;
  end;
  if MsgBox(FmtMessage(CustomMessage('VCRuntimeNeeded'), [MinText, Found, VCUrl]),
            mbConfirmation, MB_YESNO) <> IDYES then
  begin
    MsgBox(FmtMessage(CustomMessage('VCRuntimeLater'), [MinText, VCUrl]), mbInformation, MB_OK);
    exit;
  end;

  DownloadPage.Clear;
  DownloadPage.Add(VCUrl, 'vc_redist.x64.exe', '');
  DownloadPage.Show;
  Downloaded := False;
  try
    try
      DownloadPage.Download;
      Downloaded := True;
    except
      if not DownloadPage.AbortedByUser then
        MsgBox(AddPeriod(Format('%s: %s', [DownloadPage.LastBaseNameOrUrl, GetExceptionMessage])),
               mbCriticalError, MB_OK);
    end;
  finally
    DownloadPage.Hide;
  end;
  if not Downloaded then
  begin
    { Stay on the Ready page: Install offers the download again, Cancel stops. }
    Result := False;
    exit;
  end;

  Installer := ExpandConstant('{tmp}\vc_redist.x64.exe');
  if not SignedByMicrosoft(Installer) then
  begin
    MsgBox(FmtMessage(CustomMessage('VCRuntimeNotSigned'), [VCUrl]), mbError, MB_OK);
    DeleteFile(Installer);
    exit;
  end;
  { ShellExec, not Exec: the redistributable asks for elevation (UAC) itself. }
  if not ShellExec('', Installer, '/install /passive /norestart', '', SW_SHOW,
                   ewWaitUntilTerminated, ResultCode) then
    ResultCode := -1;
  Log(Format('vc_redist.x64.exe ended with code %d', [ResultCode]));
  if not VCRuntimeOK(Found) then
    Result := MsgBox(FmtMessage(CustomMessage('VCRuntimeStillMissing'), [MinText, IntToStr(ResultCode), VCUrl]),
                     mbError, MB_YESNO) = IDYES;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
    Result := EnsureVCRuntime;
end;

{ ------------------------------------------------ the old Background Remover }

function OldUninstaller(var RootKey: Integer; var Command: String): Boolean;
begin
  RootKey := HKCU;
  Result := RegQueryStringValue(RootKey, OldUninstallKey, 'UninstallString', Command);
  if not Result then
  begin
    RootKey := HKLM64;
    Result := RegQueryStringValue(RootKey, OldUninstallKey, 'UninstallString', Command);
  end;
  if not Result then
  begin
    RootKey := HKLM32;
    Result := RegQueryStringValue(RootKey, OldUninstallKey, 'UninstallString', Command);
  end;
end;

{ Removes the old Background Remover silently before the files are installed. Its silent
  uninstall keeps the downloaded models and settings; Background Editor moves them over
  on its first start. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RootKey, ResultCode, Waited: Integer;
  Command: String;
begin
  Result := '';
  if not OldUninstaller(RootKey, Command) then
    exit;
  Command := RemoveQuotes(Command);
  Log('Removing the old Background Remover: ' + Command);
  if not FileExists(Command) then
  begin
    Log('Its uninstaller is gone; removing the stale uninstall entry.');
    RegDeleteKeyIncludingSubkeys(RootKey, OldUninstallKey);
    exit;
  end;
  if not Exec(Command, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE,
              ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Result := FmtMessage(CustomMessage('OldVersionFailed'), [IntToStr(ResultCode)]);
    exit;
  end;
  { The uninstaller finishes in a second process; wait until it has removed its entry. }
  Waited := 0;
  while RegKeyExists(RootKey, OldUninstallKey) and (Waited < 60) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
end;

{ --------------------------------------------------------------- app updates }

{ True when the app itself started this Setup for Update now: silent, with /RESTARTAPP=1. }
function RestartAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RESTARTAPP|0}') = '1');
end;

{ ------------------------------------------------------------------ uninstall }

function DirSizeMB(const Dir: String): Integer;
var
  FindRec: TFindRec;
  Total: Int64;
begin
  Total := 0;
  if FindFirst(Dir + '\*', FindRec) then
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
        Total := Total + (Int64(FindRec.SizeHigh) shl 32) + FindRec.SizeLow;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
  Result := Integer(Total div (1024 * 1024));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, AppDir, PortableModels: String;
  SizeMB: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\BackgroundEditor');
    AppDir := ExpandConstant('{app}');
    { Models copied by hand next to the exe for offline use; Setup did not install them,
      so the uninstaller would otherwise leave them and the app folder behind. }
    PortableModels := AppDir + '\models';
    SizeMB := DirSizeMB(DataDir + '\models') + DirSizeMB(PortableModels);
    if (DirExists(DataDir) or DirExists(PortableModels)) and not UninstallSilent then
      if MsgBox(FmtMessage(CustomMessage('RemoveModels'), [IntToStr(SizeMB)]),
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        if DirExists(DataDir) then
          DelTree(DataDir, True, True, True);
        if DirExists(PortableModels) then
          DelTree(PortableModels, True, True, True);
        RemoveDir(AppDir);
        RegDeleteKeyIncludingSubkeys(HKCU, 'Software\BackgroundEditor');
      end;
  end;
end;

; Inno Setup 6 script; build via: python scripts/build_exe.py --installer
; build_exe.py passes AppVersion, AppExe, SourceDir, IconFile, /O and /F,
; plus BundledEngines (|-separated exe names) when --engine is given.
#ifndef BundledEngines
  #define BundledEngines ""
#endif

#define AppName "Sturddle View"
; Installed copy runs as its own instance, so its data stays apart from
; dev/standalone runs: %LOCALAPPDATA%\sturddle-view-<Instance> (platformdirs).
#define Instance "desktop"
#define InstanceArgs "--instance " + Instance
#define DataDirName "sturddle-view-" + Instance
#define KeyService "sturddle-view"
; Must match build_exe.py's BUNDLED_ENGINES_DIRNAME.
#define EnginesDir "engines"
#define EnginesScript "installer_engines.ps1"
#define RegistryFile "engines.json"
; key_store.py's account name prefix.
#define KeyAccountPrefix "ai_api_key_"

[Setup]
; Fixed forever: identifies the app across upgrades and uninstall.
AppId={{FF6444A5-EB1C-4A66-AED1-467822A11405}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install under %LOCALAPPDATA%\Programs; no admin prompt.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Drop the previous version's bundle so stale modules can't linger. Older
; bundled engines stay: their registry entries keep working.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#EnginesScript}"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "{#InstanceArgs}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "{#InstanceArgs}"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "{#InstanceArgs}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  CRED_TYPE_GENERIC = 1;
  // Mirrors the app's key-based AI providers.
  KeyProviders = 'anthropic,gemini';

var
  RemoveData: Boolean;

function CredDeleteW(TargetName: String; CredType: DWORD; Flags: DWORD): Boolean;
  external 'CredDeleteW@advapi32.dll stdcall';

procedure RegisterBundledEngines;
var
  Params: String;
  ResultCode: Integer;
begin
  ExtractTemporaryFile('{#EnginesScript}');
  Params := ExpandConstant('-NoProfile -ExecutionPolicy Bypass -File "{tmp}\{#EnginesScript}"' +
    ' -EnginesDir "{app}\{#EnginesDir}" -Names "{#BundledEngines}" -RegistryPath "{localappdata}\{#DataDirName}\{#RegistryFile}"');
  if not Exec('powershell.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    or (ResultCode <> 0) then
    SuppressibleMsgBox('Could not register the bundled engines. Add them in the app from '
      + ExpandConstant('{app}\{#EnginesDir}'), mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and ('{#BundledEngines}' <> '') then
    RegisterBundledEngines;
end;

function AskDefaultNo(const Question: String): Boolean;
begin
  Result := MsgBox(Question, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

// keyring stores a service's first key under the bare service name and the
// rest as "<account>@<service>"; delete every form.
procedure DeleteApiKeys;
var
  Providers: TStringList;
  I: Integer;
begin
  CredDeleteW('{#KeyService}', CRED_TYPE_GENERIC, 0);
  Providers := TStringList.Create;
  try
    Providers.CommaText := KeyProviders;
    for I := 0 to Providers.Count - 1 do
      CredDeleteW('{#KeyAccountPrefix}' + Providers[I] + '@{#KeyService}', CRED_TYPE_GENERIC, 0);
  finally
    Providers.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if UninstallSilent then
    exit;
  if CurUninstallStep = usUninstall then
  begin
    RemoveData := AskDefaultNo('Also remove settings, tournaments and logs?');
    if AskDefaultNo('Also remove saved AI API keys? Other app instances share them.') then
      DeleteApiKeys;
  end;
  if (CurUninstallStep = usPostUninstall) and RemoveData then
    DelTree(ExpandConstant('{localappdata}\{#DataDirName}'), True, True, True);
end;

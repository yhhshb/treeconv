; Inno Setup script for the Windows installer.  Built by packaging/build.py, which passes:
;   /DAppVersion=x.y.z  /DSourceDir=<dist\treeconv>  /DOutputDir=<dist\release>  /DOutputName=<file name>
;
; Installs per-user by default (no admin prompt); "Install for all users" is offered in the wizard.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\treeconv"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\release"
#endif
#ifndef OutputName
  #define OutputName "treeconv-setup"
#endif

[Setup]
AppId={{1F0A1037-567B-493C-93B8-B9D61315019D}
AppName=treeconv
AppVersion={#AppVersion}
AppVerName=treeconv {#AppVersion}
AppPublisher=Yoshihiro Shibuya
AppPublisherURL=https://github.com/yhhshb/treeconv
AppSupportURL=https://github.com/yhhshb/treeconv/issues
DefaultDirName={autopf}\treeconv
DefaultGroupName=treeconv
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\treeconv-gui.exe
UninstallDisplayName=treeconv
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "addtopath"; Description: "Add treeconv to PATH (use ""treeconv"" from a terminal)"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\treeconv"; Filename: "{app}\treeconv-gui.exe"
Name: "{autodesktop}\treeconv"; Filename: "{app}\treeconv-gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\treeconv-gui.exe"; Description: "{cm:LaunchProgram,treeconv}"; Flags: nowait postinstall skipifsilent

[Code]
function EnvRoot: Integer;
begin
  if IsAdminInstallMode then Result := HKEY_LOCAL_MACHINE else Result := HKEY_CURRENT_USER;
end;

function EnvKey: String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

procedure AddToPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then Paths := '';
  if Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0 then Exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then Paths := Paths + ';';
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then Exit;
  Paths := ';' + Paths + ';';
  if StringChangeEx(Paths, ';' + Dir + ';', ';', True) = 0 then Exit;
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;

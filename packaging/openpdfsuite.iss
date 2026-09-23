; openPDF suite — Inno Setup installer script (M7).
; Build (after PyInstaller produces build\dist\OpenPDFSuite):
;   "C:\Users\<user>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\openpdfsuite.iss
; Output: build\installer\OpenPDFSuiteSetup-0.1.0.exe

#define SuiteName "openPDF suite"
#define SuiteVersion "0.1.0"
#define SuitePublisher "openPDFsuite"
#define SuiteExeName "OpenPDFSuite.exe"

[Setup]
AppId={{7C4F2A98-1B6E-4C8D-9A25-84E3D10FB6C2}
AppName={#SuiteName}
AppVersion={#SuiteVersion}
AppVerName={#SuiteName} {#SuiteVersion}
AppPublisher={#SuitePublisher}
DefaultDirName={autopf}\OpenPDFSuite
DefaultGroupName={#SuiteName}
DisableProgramGroupPage=yes
OutputDir=..\build\installer
OutputBaseFilename=OpenPDFSuiteSetup-{#SuiteVersion}
SetupIconFile=resources\openpdfsuite.ico
UninstallDisplayIcon={app}\{#SuiteExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
; Windows 10/11 only
MinVersion=10.0
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
LicenseFile=resources\LICENSE.rtf

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\dist\OpenPDFSuite\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion

[Icons]
Name: "{group}\{#SuiteName}"; Filename: "{app}\{#SuiteExeName}"
Name: "{group}\{cm:UninstallProgram,{#SuiteName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#SuiteName}"; Filename: "{app}\{#SuiteExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#SuiteExeName}"; Description: "{cm:LaunchProgram,{#SuiteName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; user data lives under %APPDATA%\OpenPDFSuite and is never removed automatically (§11:
; unresolved recovery data must not be deleted); only app files go here.
Type: filesandordirs; Name: "{app}"

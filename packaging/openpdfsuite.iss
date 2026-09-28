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
; auto-update (M12): the updater spawns this installer and quits, so a running
; copy is expected during install — close it gracefully
CloseApplications=yes
PrivilegesRequiredOverridesAllowed=dialog
; the file-association registry entries are deliberately per-user (HKCU):
; on a personal machine the installing user is the one opening PDFs
UsedUserAreasWarning=no
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
Name: "pdf_default"; Description: "Offer openPDF suite when opening PDF files (right-click → Open with)"; \
    GroupDescription: "File associations:"; Flags: unchecked
Name: "pdf_default_now"; Description: "…and try to make openPDF suite the default PDF viewer now"; \
    GroupDescription: "File associations:"; Flags: unchecked

; File association (M12): register the app so PDFs can be opened with it.
; The ProgID + Capabilities make it appear in right-click "Open with" and in
; Windows Settings → Default apps WITHOUT stealing the existing default
; (Adobe/Edge keep it unless the user opts in). On Windows 10/11 the actual
; default is guarded by Windows (UserChoice) — the pdf_default_now task
; writes the legacy registration as a best effort; if Windows keeps the old
; viewer, set it once via right-click → Open with → Choose another app.
[Registry]
Root: HKCU; Subkey: "Software\Classes\OpenPDFSuite.pdf"; ValueType: string; \
    ValueName: ""; ValueData: "openPDF suite PDF Document"; \
    Flags: uninsdeletekey; Tasks: pdf_default
Root: HKCU; Subkey: "Software\Classes\OpenPDFSuite.pdf\shell\open\command"; \
    ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#SuiteExeName}"" ""%1"""; \
    Flags: uninsdeletekey; Tasks: pdf_default
Root: HKCU; Subkey: "Software\Classes\OpenPDFSuite.pdf\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#SuiteExeName},0"; \
    Flags: uninsdeletekey; Tasks: pdf_default
Root: HKCU; Subkey: "Software\OpenPDFSuite\Capabilities"; \
    ValueType: string; ValueName: "ApplicationDescription"; \
    ValueData: "Professional local-first PDF text editor"; \
    Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\OpenPDFSuite\Capabilities"; \
    ValueType: string; ValueName: "ApplicationIcon"; \
    ValueData: "{app}\{#SuiteExeName},0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\OpenPDFSuite\Capabilities\FileAssociations"; \
    ValueType: string; ValueName: ".pdf"; ValueData: "OpenPDFSuite.pdf"; \
    Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\RegisteredApplications"; \
    ValueType: string; ValueName: "{#SuiteName}"; \
    ValueData: "Software\OpenPDFSuite\Capabilities"; \
    Flags: uninsdeletevalue
; add to the per-extension Open-with list (does not change the default)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\OpenWithProgids"; \
    ValueType: string; ValueName: "OpenPDFSuite.pdf"; ValueData: ""; \
    Flags: uninsdeletevalue; Tasks: pdf_default
; best-effort legacy default (Windows 10/11 may keep the current viewer and
; point the user to Settings → Default apps instead)
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; \
    ValueType: string; ValueName: "OpenPDFSuite.pdf"; ValueData: ""; \
    Flags: uninsdeletevalue; Tasks: pdf_default_now

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
Filename: "ms-settings:defaultapps"; \
    Description: "Set openPDF suite as the default PDF viewer (opens Windows Settings)"; \
    Flags: shellexec nowait skipifsilent; Tasks: pdf_default_now

[UninstallDelete]
; user data lives under %APPDATA%\OpenPDFSuite and is never removed automatically (§11:
; unresolved recovery data must not be deleted); only app files go here.
Type: filesandordirs; Name: "{app}"

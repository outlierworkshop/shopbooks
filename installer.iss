; Inno Setup script for the ShopBooks Windows installer.
; Wraps the PyInstaller onedir output (dist\ShopBooks\) into dist\ShopBooks-Setup.exe:
; a normal double-click installer (per-user by default, no admin needed) with Start-Menu and
; optional Desktop shortcuts and an uninstaller. Compiled in CI by build-windows.yml (ISCC.exe).
;
; The user's books live in %USERPROFILE%\ShopBooks (books.db + docs\ + backups\) and are NEVER
; written or removed by this installer/uninstaller — it only manages the program files in {app}.
;
; Signing: intentionally unsigned for now (SmartScreen shows "More info -> Run anyway", the
; Windows counterpart of the Mac ad-hoc right-click->Open). To sign later, add a [Setup]
; SignTool= directive and pass the tool via ISCC /S, or sign the PyInstaller exe upstream.

#define MyAppName "ShopBooks"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Outlier Workshop"
#define MyAppURL "https://shopbooks.co/"
#define MyAppExeName "ShopBooks.exe"

[Setup]
; A stable, unique AppId (GUID) so upgrades/uninstall track the same app across versions.
AppId={{7C3B5E2A-9F1D-4C6E-B0A8-2D6F4E8A1B90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install by default -> no UAC prompt for non-admin users. {autopf} resolves to
; Program Files when elevated, else %LocalAppData%\Programs.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=ShopBooks-Setup
SetupIconFile=build\ShopBooks.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; The entire PyInstaller onedir (ShopBooks.exe + _internal\ with bundled Python & data).
Source: "dist\ShopBooks\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch ShopBooks when the installer finishes.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

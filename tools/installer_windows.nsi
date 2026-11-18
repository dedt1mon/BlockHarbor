; Build with build_installer.py and NSIS 3.13 or newer.
Unicode true
Target amd64-unicode
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
CRCCheck force
ManifestDPIAware true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "FileFunc.nsh"

!define APP_REG "Software\BlockHarbor"
!define UNINSTALL_REG "Software\Microsoft\Windows\CurrentVersion\Uninstall\BlockHarbor"

Name "BlockHarbor"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\BlockHarbor"
InstallDirRegKey HKCU "${APP_REG}" "InstallLocation"
BrandingText "BlockHarbor"
Icon "${ICON_FILE}"
UninstallIcon "${ICON_FILE}"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1049 "ProductName" "BlockHarbor"
VIAddVersionKey /LANG=1049 "FileDescription" "Установщик BlockHarbor"
VIAddVersionKey /LANG=1049 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "BlockHarbor"

Var DesktopShortcut
Var DesktopCheckbox
Var TaskbarPinHelp
Var TaskbarPinCheckbox
Var StartPinHelp
Var StartPinCheckbox
Var PinActions
Var SetupMutex
Var PendingFile
Var Elevated
Var AutoUpdate
Var RemoveData
Var RemoveAppRadio
Var RemoveDataRadio
Var RegisteredUninstall
Var UninstallSummary

!define MUI_ICON "${ICON_FILE}"
!define MUI_UNICON "${ICON_FILE}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Установка BlockHarbor"
!define MUI_WELCOMEPAGE_TEXT "Мастер установит BlockHarbor для текущего пользователя Windows и добавит его в меню «Пуск».$\r$\n$\r$\nПеред обновлением закрой Minecraft и прежний BlockHarbor. Сборки и миры хранятся в твоём профиле Windows."
!define MUI_DIRECTORYPAGE_TEXT_TOP "Выбери папку для приложения BlockHarbor. Сборки, моды и миры находятся в данных пользователя."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Запустить BlockHarbor"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchBlockHarbor
!define MUI_FINISHPAGE_TITLE "BlockHarbor установлен"
!define MUI_FINISHPAGE_TEXT "Открой BlockHarbor через меню «Пуск» или ярлык на рабочем столе.$\r$\n$\r$\nПри первом запуске лаунчер подготовит свои компоненты. После выбора сборки и нажатия «Играть» он загрузит подходящую Java и файлы Minecraft."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom ShortcutPage ShortcutLeave
!insertmacro MUI_PAGE_INSTFILES
Page custom PinHelpPage
!insertmacro MUI_PAGE_FINISH
UninstPage custom un.RemoveDataPage un.RemoveDataLeave
!define MUI_UNCONFIRMPAGE_TEXT_TOP "$UninstallSummary"
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

!macro SetupLock PREFIX
Function ${PREFIX}AcquireSetupLock
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\BlockHarbor.Installer") p .r0 ?e'
    Pop $1
    StrCpy $SetupMutex $0
    ${If} $0 == 0
    ${OrIf} $1 == 183
        MessageBox MB_OK|MB_ICONEXCLAMATION "Установка или удаление BlockHarbor уже выполняется. Дождись завершения этого окна." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
FunctionEnd
!macroend
!insertmacro SetupLock ""
!insertmacro SetupLock "un."

!macro RequireClosed PREFIX
Function ${PREFIX}RequireClosed
    retry:
    IfFileExists "$INSTDIR\BlockHarbor.exe" 0 check_sessions
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\BlockHarbor.exe", i 0x80000000, i 0, p 0, i 3, i 0, p 0) p .r0'
    System::Call 'kernel32::CloseHandle(p r0) i .r1'
    StrCmp $1 0 busy
    check_sessions:
    !if "${PREFIX}" == ""
        ; Portable releases use the same data directory. Catch their live owner
        ; handles as well, so the finish page cannot launch a competing upgrade.
        FindFirst $2 $3 "$LOCALAPPDATA\BlockHarbor\activation-*"
        loop:
        StrCmp $3 "" sessions_done
        IfFileExists "$LOCALAPPDATA\BlockHarbor\$3\.session\owner.lock" 0 next_session
        System::Call 'kernel32::CreateFileW(w "$LOCALAPPDATA\BlockHarbor\$3\.session\owner.lock", i 0x80000000, i 0, p 0, i 3, i 0, p 0) p .r0'
        System::Call 'kernel32::CloseHandle(p r0) i .r1'
        ${If} $1 == 0
            FindClose $2
            Goto busy
        ${EndIf}
        next_session:
        FindNext $2 $3
        Goto loop
        sessions_done:
        FindClose $2
    !endif
    Return
    busy:
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "BlockHarbor сейчас запущен или его файл используется. Закрой лаунчер и Minecraft, затем нажми «Повторить». Если окно уже закрыто, дождись завершения фонового процесса." /SD IDCANCEL IDRETRY retry
    SetErrorLevel 2
    Abort
FunctionEnd
!macroend
!insertmacro RequireClosed ""
!insertmacro RequireClosed "un."

Function .onInit
    SetShellVarContext current
    SetRegView 64
    Call AcquireSetupLock
    StrCpy $AutoUpdate 0
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/AUTOUPDATE" $1
    ${IfNot} ${Errors}
        StrCpy $AutoUpdate 1
        SetSilent silent
        IfFileExists "$INSTDIR\BlockHarbor.exe" automatic_target_ready
        SetErrorLevel 2
        Abort
        automatic_target_ready:
    ${EndIf}
    StrCpy $DesktopShortcut 1
    StrCpy $TaskbarPinHelp 0
    StrCpy $StartPinHelp 0
    ClearErrors
    ReadRegDWORD $0 HKCU "${APP_REG}" "DesktopShortcut"
    ${IfNot} ${Errors}
        StrCpy $DesktopShortcut $0
    ${EndIf}
    ; A manually elevated shell must not start the game with elevated rights.
    StrCpy $Elevated 1
    System::Call 'kernel32::GetCurrentProcess() p .r0'
    System::Call 'advapi32::OpenProcessToken(p r0, i 0x0008, *p .r1) i .r2'
    ${If} $2 != 0
        System::Call 'advapi32::GetTokenInformation(p r1, i 20, *i .r2, i 4, *i .r3) i .r0'
        ${If} $0 != 0
            StrCpy $Elevated $2
        ${EndIf}
        System::Call 'kernel32::CloseHandle(p r1)'
    ${EndIf}
FunctionEnd

Function ShortcutPage
    !insertmacro MUI_HEADER_TEXT "Ярлыки и закрепление" "BlockHarbor появится в списке приложений меню «Пуск»."
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}
    ${NSD_CreateCheckbox} 0 10u 100% 16u "Создать ярлык на рабочем столе"
    Pop $DesktopCheckbox
    ${NSD_SetState} $DesktopCheckbox $DesktopShortcut
    ${NSD_CreateCheckbox} 0 39u 100% 16u "Помочь закрепить на панели задач"
    Pop $TaskbarPinCheckbox
    ${NSD_SetState} $TaskbarPinCheckbox $TaskbarPinHelp
    ${NSD_CreateCheckbox} 0 65u 100% 16u "Помочь закрепить в меню «Пуск» (начальный экран)"
    Pop $StartPinCheckbox
    ${NSD_SetState} $StartPinCheckbox $StartPinHelp
    ${NSD_CreateLabel} 0 96u 100% 35u "Если выбрать помощь, после установки появятся короткая инструкция и кнопка открытия ярлыка. Само закрепление выбирается в меню Windows."
    Pop $0
    nsDialogs::Show
FunctionEnd

Function ShortcutLeave
    ${NSD_GetState} $DesktopCheckbox $DesktopShortcut
    ${NSD_GetState} $TaskbarPinCheckbox $TaskbarPinHelp
    ${NSD_GetState} $StartPinCheckbox $StartPinHelp
FunctionEnd

; Microsoft does not support calling the taskbar pin-request API from an
; installer. These opt-in helpers open the installed shortcut and explain the
; user's own Windows commands; they never alter taskbar/Start pin state.
; https://learn.microsoft.com/en-us/windows/apps/develop/windows-integration/pin-to-taskbar
Function PinHelpPage
    ${If} $TaskbarPinHelp != 1
    ${AndIf} $StartPinHelp != 1
        Abort
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "Закрепление в Windows" "BlockHarbor установлен. Выбери закрепление в меню Windows."
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}
    ${NSD_CreateLabel} 0 2u 100% 30u "Нажми клавишу Win, введи BlockHarbor и щёлкни по найденному приложению правой кнопкой мыши. Затем выбери:"
    Pop $0
    StrCpy $PinActions ""
    ${If} $TaskbarPinHelp == 1
        StrCpy $PinActions "• «Закрепить на панели задач»"
    ${EndIf}
    ${If} $StartPinHelp == 1
        ${If} $PinActions != ""
            StrCpy $PinActions "$PinActions$\r$\n$\r$\n"
        ${EndIf}
        StrCpy $PinActions "$PinActions• «Закрепить на начальном экране» — меню «Пуск»"
    ${EndIf}
    ${NSD_CreateLabel} 0 40u 100% 42u "$PinActions"
    Pop $0
    ${NSD_CreateButton} 0 88u 100% 18u "Показать ярлык BlockHarbor в Проводнике"
    Pop $0
    ${NSD_OnClick} $0 OpenInstalledShortcut
    ${NSD_CreateLabel} 0 114u 100% 26u "После закрепления вернись сюда и нажми «Далее». Этот шаг можно пропустить и завершить установку."
    Pop $0
    ; Installation is already complete. Leave only the forward route to Finish.
    GetDlgItem $0 $HWNDPARENT 2
    EnableWindow $0 0
    nsDialogs::Show
FunctionEnd

Function OpenInstalledShortcut
    Pop $0
    IfFileExists "$SMPROGRAMS\BlockHarbor\BlockHarbor.lnk" 0 shortcut_missing
    ClearErrors
    ExecShell "open" "$WINDIR\explorer.exe" '/select,"$SMPROGRAMS\BlockHarbor\BlockHarbor.lnk"' SW_SHOWNORMAL
    ${IfNot} ${Errors}
        Return
    ${EndIf}
    shortcut_missing:
    MessageBox MB_OK|MB_ICONINFORMATION "Не удалось показать ярлык. Нажми Win, введи BlockHarbor и выбери закрепление в контекстном меню найденного приложения." /SD IDOK
FunctionEnd

Function LaunchBlockHarbor
    ; In automatic mode the helper owns the one restart after setup has exited.
    ${If} $AutoUpdate == 1
        Return
    ${EndIf}
    ${If} $Elevated == 0
        SetOutPath "$INSTDIR"
        Exec '"$INSTDIR\BlockHarbor.exe"'
    ${Else}
        MessageBox MB_OK|MB_ICONINFORMATION "Установка завершена. Открой BlockHarbor обычным щелчком по ярлыку в меню «Пуск», чтобы игра работала с твоим Steam." /SD IDOK
    ${EndIf}
FunctionEnd

Function .onInstFailed
    ${If} $PendingFile != ""
        Delete "$PendingFile"
    ${EndIf}
FunctionEnd

Section "BlockHarbor" SEC_APP
    SectionIn RO
    Call RequireClosed
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=BlockHarbor.exe "${LAUNCHER_EXE}"
    ClearErrors
    CreateDirectory "$INSTDIR"
    StrCpy $PendingFile "$INSTDIR\BlockHarbor.pending.exe"
    CopyFiles /SILENT "$PLUGINSDIR\BlockHarbor.exe" "$PendingFile"
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось записать приложение в выбранную папку. Проверь свободное место и доступ к папке, затем повтори установку." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    commit:
    ; Same-volume replacement keeps the old executable intact if Windows refuses.
    System::Call 'kernel32::MoveFileExW(w "$PendingFile", w "$INSTDIR\BlockHarbor.exe", i 9) i .r0'
    ${If} $0 == 0
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Не удалось обновить файл BlockHarbor. Закрой запущенное приложение и проверь доступ к папке установки." /SD IDCANCEL IDRETRY commit
        SetErrorLevel 2
        Abort
    ${EndIf}
    StrCpy $PendingFile ""
    ClearErrors
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось создать программу удаления. Повтори установку в доступную для записи папку." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}

    SetOutPath "$INSTDIR"
    ClearErrors
    ${If} $AutoUpdate != 1
        ReadRegDWORD $4 HKCU "${APP_REG}" "DesktopShortcut"
        ClearErrors
        CreateDirectory "$SMPROGRAMS\BlockHarbor"
        CreateShortcut "$SMPROGRAMS\BlockHarbor\BlockHarbor.lnk" "$INSTDIR\BlockHarbor.exe" "" "$INSTDIR\BlockHarbor.exe" 0
        CreateShortcut "$SMPROGRAMS\BlockHarbor\Удалить BlockHarbor.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
        ${If} $DesktopShortcut == 1
            CreateShortcut "$DESKTOP\BlockHarbor.lnk" "$INSTDIR\BlockHarbor.exe" "" "$INSTDIR\BlockHarbor.exe" 0
        ${Else}
            ${If} $4 == 1
                Delete "$DESKTOP\BlockHarbor.lnk"
            ${EndIf}
        ${EndIf}
        WriteRegDWORD HKCU "${APP_REG}" "DesktopShortcut" $DesktopShortcut
    ${EndIf}

    WriteRegStr HKCU "${APP_REG}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayName" "BlockHarbor"
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "${UNINSTALL_REG}" "Publisher" "BlockHarbor"
    WriteRegStr HKCU "${UNINSTALL_REG}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayIcon" '"$INSTDIR\BlockHarbor.exe",0'
    WriteRegStr HKCU "${UNINSTALL_REG}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_REG}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "NoRepair" 1
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "EstimatedSize" ${APP_SIZE}
    !if "${PROJECT_URL}" != ""
        WriteRegStr HKCU "${UNINSTALL_REG}" "URLInfoAbout" "${PROJECT_URL}"
    !endif
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось завершить создание ярлыков или регистрацию приложения. Повтори установку под своей учётной записью Windows." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    SetErrorLevel 0
SectionEnd

Function un.onInit
    SetShellVarContext current
    SetRegView 64
    Call un.AcquireSetupLock
    ; There is deliberately no command-line switch for deleting personal data.
    ; Silent removal (including any upgrade tooling) always preserves it.
    StrCpy $RemoveData 0
    Call un.UpdateUninstallSummary
FunctionEnd

Function un.CheckRegisteredInstall
    ; The helper repeats this check against canonical directory handles. This
    ; early check also keeps a copied/old uninstaller from offering full cleanup.
    StrCpy $RegisteredUninstall 0
    ClearErrors
    ReadRegStr $0 HKCU "${APP_REG}" "InstallLocation"
    ${If} ${Errors}
    ${OrIf} $0 == ""
        Return
    ${EndIf}
    ReadRegStr $1 HKCU "${UNINSTALL_REG}" "InstallLocation"
    ${If} ${Errors}
    ${OrIf} $1 == ""
        Return
    ${EndIf}
    GetFullPathName $2 "$INSTDIR"
    GetFullPathName $0 "$0"
    GetFullPathName $1 "$1"
    ${If} ${Errors}
        Return
    ${EndIf}
    StrCmp $0 $2 0 not_registered
    StrCmp $1 $2 0 not_registered
    StrCpy $RegisteredUninstall 1
    not_registered:
FunctionEnd

Function un.UpdateUninstallSummary
    ${If} $RemoveData == 1
        StrCpy $UninstallSummary "Будут удалены приложение BlockHarbor и ВСЕ локальные данные из %LOCALAPPDATA%\BlockHarbor.$\r$\n$\r$\nСборки, миры, резервные копии, скриншоты, скины, настройки, кэши, компоненты и локальные данные входа Prism. Восстановить удалённые данные будет нельзя.$\r$\n$\r$\nНажми «Удалить», чтобы продолжить."
    ${Else}
        StrCpy $UninstallSummary "Будут удалены приложение BlockHarbor и его ярлыки.$\r$\n$\r$\nСборки, миры, резервные копии, скриншоты, скины, настройки и загруженные компоненты останутся в %LOCALAPPDATA%\BlockHarbor.$\r$\n$\r$\nНажми «Удалить», чтобы продолжить."
    ${EndIf}
FunctionEnd

Function un.RemoveDataPage
    IfSilent 0 show_options
    StrCpy $RemoveData 0
    Abort
    show_options:
    !insertmacro MUI_HEADER_TEXT "Удаление BlockHarbor" "Выбери, что удалить. По умолчанию данные сохраняются."
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        StrCpy $RemoveData 0
        Call un.UpdateUninstallSummary
        Abort
    ${EndIf}
    ${NSD_CreateFirstRadioButton} 0 0 100% 18u "Удалить только приложение"
    Pop $RemoveAppRadio
    ${NSD_CreateLabel} 12u 21u 94% 24u "Сборки, миры, настройки и загруженные компоненты останутся на компьютере."
    Pop $0
    ${NSD_CreateAdditionalRadioButton} 0 48u 100% 18u "Удалить приложение и все локальные данные"
    Pop $RemoveDataRadio
    ${NSD_CreateLabel} 12u 69u 94% 36u "Безвозвратно: сборки, миры, резервные копии, скриншоты, скины, настройки, кэши, компоненты и локальные данные входа Prism."
    Pop $0
    Call un.CheckRegisteredInstall
    ${If} $RegisteredUninstall == 1
        ${NSD_CreateLabel} 0 112u 100% 28u "Папка данных: %LOCALAPPDATA%\BlockHarbor.$\r$\nУчётные записи Microsoft и Steam сохранятся."
        Pop $0
    ${Else}
        StrCpy $RemoveData 0
        EnableWindow $RemoveDataRadio 0
        ${NSD_CreateLabel} 0 112u 100% 28u "Полная очистка недоступна: эта папка не совпадает с зарегистрированной установкой BlockHarbor."
        Pop $0
    ${EndIf}
    ${If} $RemoveData == 1
        ${NSD_Check} $RemoveDataRadio
    ${Else}
        ${NSD_Check} $RemoveAppRadio
    ${EndIf}
    nsDialogs::Show
FunctionEnd

Function un.RemoveDataLeave
    ${NSD_GetState} $RemoveDataRadio $RemoveData
    Call un.UpdateUninstallSummary
FunctionEnd

Function un.RemoveLocalData
    ; Validate again immediately before extraction/launch, not just on the page.
    Call un.CheckRegisteredInstall
    ${If} $RegisteredUninstall != 1
        MessageBox MB_OK|MB_ICONSTOP "Удаление остановлено: папка приложения не совпадает с текущей зарегистрированной установкой. Запусти удаление BlockHarbor через «Параметры → Приложения» Windows." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    ClearErrors
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    ${If} ${Errors}
    ${OrIf} $PLUGINSDIR == ""
        MessageBox MB_OK|MB_ICONSTOP "Не удалось подготовить временную папку для очистки данных. Удаление остановлено. Проверь свободное место и доступ к временной папке Windows." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    ; This executable and the uninstaller run outside the data being removed.
    ; INSTDIR itself may be inside the data root, so do not use it as the cwd.
    ClearErrors
    File /oname=BlockHarborDataCleanup.exe "${CLEANUP_EXE}"
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось извлечь компонент очистки данных. Удаление остановлено. Проверь свободное место и повтори попытку." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    ClearErrors
    ExecWait '"$PLUGINSDIR\BlockHarborDataCleanup.exe" --remove-data --install-dir "$INSTDIR"' $0
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось запустить компонент очистки данных. Удаление остановлено; ярлыки и регистрация приложения сохранены." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    ${If} $0 != 0
        ; The helper has already displayed the specific error. Do not report a
        ; successful uninstall or unregister an installation after partial work.
        MessageBox MB_OK|MB_ICONSTOP "Очистка данных не завершена. Удаление остановлено; ярлыки и регистрация приложения сохранены. Устрани причину из предыдущего сообщения и повтори попытку." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
FunctionEnd

Section "Uninstall"
    IfSilent 0 preserve_selection
    StrCpy $RemoveData 0
    preserve_selection:
    Call un.RequireClosed
    ${If} $RemoveData == 1
        Call un.RemoveLocalData
    ${EndIf}
    IfFileExists "$INSTDIR\BlockHarbor.exe" 0 app_removed
    ClearErrors
    Delete "$INSTDIR\BlockHarbor.exe"
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "Не удалось удалить файл BlockHarbor. Закрой приложение и повтори удаление." /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    app_removed:
    ReadRegStr $0 HKCU "${APP_REG}" "InstallLocation"
    ${If} $0 == $INSTDIR
        ReadRegDWORD $1 HKCU "${APP_REG}" "DesktopShortcut"
        ${If} $1 == 1
            Delete "$DESKTOP\BlockHarbor.lnk"
        ${EndIf}
        Delete "$SMPROGRAMS\BlockHarbor\BlockHarbor.lnk"
        Delete "$SMPROGRAMS\BlockHarbor\Удалить BlockHarbor.lnk"
        RMDir "$SMPROGRAMS\BlockHarbor"
        DeleteRegKey HKCU "${UNINSTALL_REG}"
        DeleteRegKey HKCU "${APP_REG}"
    ${EndIf}
    Delete "$INSTDIR\Uninstall.exe"
    ; The helper may already have removed this folder if it was inside the data
    ; root. For application-only removal, never recurse into unrelated files.
    RMDir "$INSTDIR"
    SetErrorLevel 0
SectionEnd

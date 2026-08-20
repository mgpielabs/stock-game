' 작업 스케줄러 래퍼: 로그온 시 콘솔 창 없이 pm2 resurrect 실행
' - 창 스타일 0 = SW_HIDE (node.exe 콘솔 창 완전 숨김)
' - False = 대기 안 함 (pm2는 데몬을 띄우고 바로 종료, 기다릴 필요 없음)
Option Explicit
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run Chr(34) & "C:\nvm4w\nodejs\node.exe" & Chr(34) & _
             " " & Chr(34) & "C:\Users\kkkhe\AppData\Local\nvm\v22.22.2\node_modules\pm2\bin\pm2" & Chr(34) & _
             " resurrect", _
             0, False
Set WshShell = Nothing

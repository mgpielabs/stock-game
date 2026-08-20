' 작업 스케줄러 래퍼: 콘솔 창 없이 daily_pipeline.py 실행
' - 창 스타일 0 = SW_HIDE (python.exe 콘솔 창 완전 숨김)
' - 자식 프로세스는 daily_pipeline.py 내부에서 CREATE_NO_WINDOW 처리
' - True = 작업이 끝날 때까지 대기 (스케줄러가 완료를 올바르게 인식)
Option Explicit
Dim WshShell
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\01coding\stock-game\backend"
WshShell.Run Chr(34) & "c:\01coding\stock-game\backend\.venv\Scripts\python.exe" & Chr(34) & _
             " " & Chr(34) & "c:\01coding\stock-game\backend\daily_pipeline.py" & Chr(34), _
             0, True
Set WshShell = Nothing

module.exports = {
  apps: [
    {
      name: 'stock-backend',
      script: 'c:/01coding/stock-game/backend/.venv/Scripts/python.exe',
      args: '-m uvicorn main:app --host 0.0.0.0 --port 8001',
      cwd: 'c:/01coding/stock-game/backend/server',
      interpreter: 'none',
      autorestart: true,
      watch: false,
      env: {
        PYTHONIOENCODING: 'utf-8',
      },
    },
    {
      name: 'paper-trade-closer',
      script: 'c:/01coding/stock-game/backend/.venv/Scripts/python.exe',
      args: 'c:/01coding/stock-game/backend/scripts/close_expired_cron.py',
      cwd: 'c:/01coding/stock-game',
      interpreter: 'none',
      cron_restart: '0 16 * * 1-5',   // 평일 오후 4시 (장 마감 후)
      autorestart: false,
      watch: false,
      env: {
        PYTHONIOENCODING: 'utf-8',
      },
    },
  ],
}

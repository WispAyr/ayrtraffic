module.exports = {
  apps: [
    {
      name: 'ayrtraffic',
      script: '/opt/homebrew/bin/python3',
      args: '-m uvicorn server:app --host 0.0.0.0 --port 3870 --workers 1',
      cwd: '/Users/noc/operations/ayrtraffic',
      interpreter: 'none',
      env: {
        NODE_ENV: 'production',
        PYTHONUNBUFFERED: '1',
      },
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: '/Users/noc/operations/ayrtraffic/logs/error.log',
      out_file: '/Users/noc/operations/ayrtraffic/logs/out.log',
    },
  ],
};

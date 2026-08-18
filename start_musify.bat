start cmd /k "cd /d C:\Users\bhavy\OneDrive\Desktop\Backup for worst 2\backend && python -m uvicorn main:app --reload --host 0.0.0.0"
start cmd /k "cd /d C:\Users\bhavy\OneDrive\Desktop\Backup for worst 2\frontend && python -m http.server 3000"
timeout /t 5
start http://localhost:3000
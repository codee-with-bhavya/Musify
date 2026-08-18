start cmd /k "cd /d C:\Users\bhavy\OneDrive\Desktop\git musify\backend && python -m uvicorn main:app --reload --port 8000"
start cmd /k "cd /d C:\Users\bhavy\OneDrive\Desktop\git musify\frontend && python -m http.server 3000"
timeout /t 5
start http://localhost:3000
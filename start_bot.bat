@echo off
cd /d C:\Users\Administrator\AI_Brain_Correlation_Hedging

call .venv\Scripts\activate.bat

uvicorn main:app --host 0.0.0.0 --port 8000
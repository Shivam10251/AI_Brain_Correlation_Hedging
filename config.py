import os
from dotenv import load_dotenv

load_dotenv()

# Trading symbols
SYMBOLS = [
    "EURUSDm",
    "GBPUSDm",
    "BTCUSDm",
    "XAUUSDm",
]

# Risk management
SL_PERCENT = 0.002   # 0.20%
TP_PERCENT = 0.004   # 0.40%

API_KEY = os.getenv("API_KEY")

# DeepSeek API
DEEPSEEK_API_KEY = "API_KEY"


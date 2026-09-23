# utils/time.py

from datetime import datetime

def get_time():
    """获取当前时间"""
    return datetime.now().strftime("%Y-%m-%d %A %H:%M")

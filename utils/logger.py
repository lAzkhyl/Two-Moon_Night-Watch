# =====
# MODULE: utils/logger.py
# =====
# Architecture Overview:
# Centralizes Python logging configuration for the enterprise bot.
# Sets up console formatting and daily-rotating file logs to ensure
# production traceback errors are never lost to the void.
# =====
import logging
import logging.handlers
import os

def setup_enterprise_logging():
    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)
    
    # Root Logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Avoid duplicate handlers if called multiple times
    if logger.hasHandlers():
        return
        
    # Console Handler (Formatting with potential color mapping)
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s', 
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    
    # File Handler (Rotating daily, keep 14 days)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename='logs/gamenight.log',
        when='midnight',
        interval=1,
        backupCount=14,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)-25s | %(module)s:%(lineno)d | %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
        
    # Silence extremely noisy internal libraries
    logging.getLogger('discord.gateway').setLevel(logging.WARNING)
    logging.getLogger('asyncpg').setLevel(logging.WARNING)
